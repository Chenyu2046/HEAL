"""Task, batch, and small provenance-bound episodic memory layers."""

from __future__ import annotations

import json
import os
import tempfile
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from .domain import ActionKind, Observation, RiskClass, canonical_json, sha256_text, utc_now
from .runtime.workspace import WorkspaceError

if TYPE_CHECKING:  # 仅类型标注;运行时不需要,避免引入更重的依赖面
    from .runtime.workspace import WorkspaceState

# 置信度探测读取源文件的上限,对齐 ToolLimits.max_file_bytes 默认值。
_EPISODE_PROBE_MAX_BYTES = 256_000


@dataclass
class TaskStateMemory:
    task_id: str
    worker_id: str
    current_hypothesis: str = ""
    observations: list[Observation] = field(default_factory=list)
    action_map: dict[str, ActionKind] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def add(self, observation: Observation) -> None:
        self.observations.append(observation)
        if observation.error:
            self.evidence.append(observation.error)


@dataclass(frozen=True)
class BatchCacheEntry:
    key: str
    workspace_revision: int
    file_hashes: Mapping[str, str]
    value: Any
    created_at: str = field(default_factory=utc_now)


class BatchCache:
    def __init__(self) -> None:
        self._entries: dict[str, BatchCacheEntry] = {}

    def put(self, key: str, workspace_revision: int, file_hashes: Mapping[str, str], value: Any) -> None:
        self._entries[key] = BatchCacheEntry(key, workspace_revision, dict(file_hashes), value)

    def get(self, key: str, workspace_revision: int, file_hashes: Mapping[str, str]) -> Any | None:
        entry = self._entries.get(key)
        if entry is None or entry.workspace_revision != workspace_revision or dict(entry.file_hashes) != dict(file_hashes):
            return None
        return entry.value

    def invalidate(self, paths: set[str] | None = None) -> None:
        if paths is None:
            self._entries.clear()
            return
        self._entries = {
            key: value for key, value in self._entries.items() if not paths.intersection(value.file_hashes)
        }


@dataclass(frozen=True)
class Episode:
    episode_id: str
    repo: str
    module: str
    rule: str
    source_commit: str
    memory_type: str
    keywords: tuple[str, ...]
    summary: str
    provenance: tuple[str, ...]
    human_review: str
    ci_result: str
    created_at: str = field(default_factory=utc_now)
    # --- Phase 3(方案 §11/§13/§14)结构化字段:全部带默认值,旧 JSON(上面 11 个
    # 字段)仍可直接 Episode(**json) 读取;追加在 created_at 之后以保持旧的位置
    # 参数构造兼容。 ---
    trigger_rule: str = ""
    trigger_module: str = ""
    trigger_symbol: str = ""
    warning_signature: str = ""
    root_cause: str = ""
    evidence_summary: str = ""
    fix_summary: str = ""
    changed_files: tuple[str, ...] = ()
    validation_feedback: str = ""
    build_result: str = ""
    ut_result: str = ""
    scan_result: str = ""
    validated: bool = False
    # confidence 是 retrieve() 的输出字段:按当前 workspace 做词法探测后经
    # dataclasses.replace 填充(HIGH/MEDIUM/STALE),落盘时恒为空字符串。
    confidence: str = ""
    schema_version: str = "1"

    def __post_init__(self) -> None:
        # canonical_json 把 tuple 字段写成 JSON 数组,Episode(**json) 读回会是
        # list,与 frozen 注解不符;这里把序列字段统一 tuple 化,保证
        # round-trip 与旧扁平 JSON 读回类型稳定。list 之外的非 tuple 入参在
        # 此 fail-closed(retrieve 的加载路径会捕获并跳过该条)。
        for name in ("keywords", "provenance", "changed_files"):
            value = getattr(self, name)
            if isinstance(value, tuple):
                continue
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))
                continue
            raise TypeError(f"Episode.{name} must be a tuple or list, got {type(value).__name__}")


class EpisodeStore:
    """JSON episode files keep the initial store inspectable and dependency-free."""

    def __init__(self, root: str | Path, worker_id: str | None = None, task_id: str | None = None, *, experience_only: bool = False) -> None:
        base = Path(root).resolve()
        self.root = base / "tasks" / (task_id or "_unbound") / (worker_id or "default")
        self.experience_root = base / "experience"
        if not experience_only:
            self.root.mkdir(parents=True, exist_ok=True)
        self.experience_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_experience(cls, root: str | Path) -> "EpisodeStore":
        """共享经验库绑定(方案 §12):不创建 task/worker 沙箱目录,只保证 experience_root 可用。"""
        return cls(root, experience_only=True)

    def store_candidate(self, episode: Episode) -> Path:
        if episode.human_review not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ValueError("human_review must be explicit")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", episode.episode_id):
            raise ValueError("episode_id must be a simple file name")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", episode.episode_id):
            raise ValueError("episode_id must be a simple file name")
        payload = canonical_json(episode)
        path = self.root / f"{episode.episode_id}.json"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def store_experience(self, episode: Episode) -> Path:
        if episode.human_review != "APPROVED" or episode.ci_result not in {"VALIDATION_PASS", "PASS"}:
            raise ValueError("shared experience requires human approval and a passing validation result")
        path = self.experience_root / f"{episode.episode_id}.json"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", episode.episode_id):
            raise ValueError("episode_id must be a simple file name")
        payload = canonical_json(episode)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.experience_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def retrieve(
        self,
        *,
        repo: str,
        module: str | None,
        rule: str | None,
        keywords: tuple[str, ...],
        source_commit: str,
        symbol: str | None = None,
        warning_signature: str | None = None,
        workspace: "WorkspaceState | None" = None,
        limit: int = 3,
        deadline: float | None = None,
    ) -> tuple[Episode, ...]:
        """方案 §13 的结构化评分检索(无向量搜索):

        score = same_rule*4 + same_module*3 + same_symbol*3 + warning_match*2
                + keyword_hit*1 + validated*1
        source_commit 完全相等按 §14 改为 +2 加分项(不再硬过滤);repo 仍是
        provenance 硬过滤,module/rule 通配过滤保留。workspace 提供时按 §14 做
        词法级置信度探测(见 _probe_symbol_confidence),HIGH 额外 +1。取 Top
        ``limit``(默认 3)。
        """
        wanted = {word.lower() for word in keywords if word}
        paths = sorted(set(self.root.glob("*.json")) | set(self.experience_root.glob("*.json")))
        scored: list[tuple[int, str, Episode]] = []
        for path in paths:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("memory retrieval stopped at the workspace deadline")
            try:
                if path.stat().st_size > 256_000:
                    continue
                episode = Episode(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("memory retrieval stopped at the workspace deadline")
            if episode.repo != repo or (module and episode.module not in {module, "*"}) or (rule and episode.rule not in {rule, "*"}):
                continue
            confidence = ""
            if workspace is not None:
                confidence = _probe_symbol_confidence(
                    workspace,
                    file=episode.changed_files[0] if episode.changed_files else None,
                    symbol=episode.trigger_symbol or None,
                )
                episode = replace(episode, confidence=confidence)
            score = 0
            if rule and episode.rule == rule:
                score += 4  # same_rule
            if module and episode.module == module:
                score += 3  # same_module
            if symbol and episode.trigger_symbol and episode.trigger_symbol == symbol:
                score += 3  # same_symbol
            if warning_signature and episode.warning_signature and episode.warning_signature == warning_signature:
                score += 2  # warning_match
            score += len(wanted.intersection(word.lower() for word in episode.keywords))
            if episode.validated:
                score += 1  # validated
            if source_commit and episode.source_commit == source_commit:
                score += 2  # §14:source_commit 完全相等是加分项,不是过滤器
            if confidence == "HIGH":
                score += 1  # 置信度小幅加成
            scored.append((score, episode.created_at, episode))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in scored[: max(0, limit)])


def _probe_symbol_confidence(workspace: "WorkspaceState", *, file: str | None, symbol: str | None) -> str:
    """词法级置信度判断(方案 §14),不是语义分析。

    HIGH = 当前文件仍被符号扫描器扫到该符号(精确名或 Class::name 后缀);
    STALE = 文件不存在(含受保护/逃逸路径)或扫描结果中符号已消失;
    MEDIUM = 无 file/symbol、文件超限、读取失败或扫描不到任何声明——扫描器
    对宏/typedef 有漏报天花板,此时诚实地说"无法判断"而不是猜 HIGH/STALE。
    与方案 §14 的偏差声明:§14 定义四档 HIGH/MEDIUM/LOW/STALE,本实现三档——
    LOW 并入 STALE,MEDIUM 重定义为"扫描器无法判断",整体方向更保守(fail-closed:
    不确定时不给 HIGH)。
    """
    if not file or not symbol:
        return "MEDIUM"
    try:
        path = workspace.resolve(file)
    except WorkspaceError:
        return "STALE"
    try:
        if not path.is_file():
            return "STALE"
        if path.stat().st_size > _EPISODE_PROBE_MAX_BYTES:
            return "MEDIUM"
        # 函数级导入:tools/__init__ 经 executor 反向依赖本模块,顶层导入会成环。
        from .tools.symbols import scan_symbols

        decls = scan_symbols(path.read_bytes().decode("utf-8", errors="replace"))
    except OSError:
        return "MEDIUM"
    if not decls:
        return "MEDIUM"
    if any(decl.name == symbol or decl.name.endswith("::" + symbol) for decl in decls):
        return "HIGH"
    return "STALE"


def episode_candidate_id(repo: str, rule: str, summary: str) -> str:
    return sha256_text(f"{repo}:{rule}:{summary}")[:24]
