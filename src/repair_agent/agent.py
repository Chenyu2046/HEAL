"""Single Repair Agent loop with bounded budgets and optional read-only chunks."""

from __future__ import annotations

import json
import time
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .domain import (
    ActionKind,
    BatchProposal,
    Budget,
    Finding,
    Issue,
    Observation,
    RepairTask,
    RiskClass,
    ToolStatus,
    canonical_json,
    issue_id,
    redact_text,
    sha256_text,
    to_primitive,
)
from .config import ToolLimits
from .memory import EvidenceLedger
from .models import ModelAdapter, ModelDeadlineExceeded, ModelDecision, ModelError, ModelProtocolError, ToolCall
from .skills import SkillContextError, SkillRouter
from .retry import RetryPolicy
from .tools.chunking import ActionChunk, ChunkAction, ChunkExecutor
from .tools.executor import ToolExecutor
from .validation.scope import PatchScopeGuard


TraceCallback = Callable[[Observation], None]
UsageCallback = Callable[["AgentUsage"], None]

# Chunk execution blocks on budget reasons before any action runs; those specific
# reasons are surfaced, while every other chunk failure keeps the durable generic reason.
_CHUNK_BUDGET_REASONS = frozenset({
    "tool call budget exhausted during action chunk",
    "wall-clock budget exhausted during action chunk",
    "search round budget exhausted",
    "symbol expansion budget exhausted",
    "context file budget exhausted",
})

# R6:model_reason 只进诊断元数据,经单一咽喉点 redact+截断,永不进入决策路径。
MAX_MODEL_REASON_CHARS = 1_000


def _cap_model_reason(value: str) -> str:
    if len(value) <= MAX_MODEL_REASON_CHARS:
        return value
    return value[:MAX_MODEL_REASON_CHARS] + "…[truncated]"


def _safe_review_reason(reason: str) -> str:
    """Keep durable review reasons useful without copying arbitrary response/error text."""
    budgets = {
        "model call budget exhausted", "tool call budget exhausted", "token budget exhausted",
        "model token usage unavailable; stopped to preserve budget",
        "edit attempt budget exhausted", "wall-clock budget exhausted",
        "tool call budget exhausted during action chunk", "wall-clock budget exhausted during action chunk",
        "model request attempt budget exhausted",
        "search round budget exhausted", "symbol expansion budget exhausted", "context file budget exhausted",
        "check run budget exhausted",
        "skill context limit exceeded",
        "skill context deadline exceeded",
    }
    parts = [item.strip() for item in reason.split(";")]
    if parts and all(item in budgets for item in parts):
        return "; ".join(parts)
    if reason.startswith("tool observation incomplete: "):
        detail = reason.removeprefix("tool observation incomplete: ")
        if all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}/[A-Z_]{1,32}", item.strip()) for item in detail.split(",")):
            return reason
        return "tool observation incomplete; human review required"
    if reason.startswith("MODEL_HTTP:"):
        return "MODEL_HTTP: model request failed"
    if reason.startswith("MODEL_TRANSPORT:"):
        return "MODEL_TRANSPORT: model request failed"
    if reason.startswith("NOT_CONFIGURED:"):
        return "NOT_CONFIGURED: model provider is unavailable"
    if reason.startswith("unexpected model failure:"):
        error_type = reason.partition(":")[2].strip()
        if error_type.isidentifier():
            return f"unexpected model failure: {error_type}"
    if reason.startswith("patch scope requires review"):
        return "patch scope requires review"
    if reason.startswith("invalid action chunk"):
        return "invalid action chunk"
    if reason in {
        "invalid model protocol", "model requested human review", "unsupported model decision",
        "action chunk incomplete", "action chunk rejected because chunking is disabled",
        "worker requires review", "could not obtain a complete real Git diff",
    }:
        return reason
    return "worker requires review; inspect outcome metadata"


def _check_summary_note(observations: Sequence[Observation]) -> str | None:
    """One bounded note with per-check last verdicts (tech-design §1.8); display metadata only."""
    verdicts: dict[str, Mapping[str, Any]] = {}
    for item in observations:
        if item.tool != "run_checks" or not isinstance(item.content, Mapping):
            continue
        entries = item.content.get("checks")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("name"):
                verdicts[str(entry["name"])] = entry  # last verdict wins
    if not verdicts:
        return None
    parts = []
    for name in sorted(verdicts)[:8]:
        entry = verdicts[name]
        verdict = str(entry.get("verdict") or "")
        returncode = entry.get("returncode")
        error = str(entry.get("error") or "").strip()
        if verdict in {"PASS", "FAIL"} and isinstance(returncode, int):
            parts.append(f"{name}={verdict}(exit {returncode})")
        else:
            parts.append(f"{name}={verdict or 'INFRA_FAIL'}({error or 'unknown'})")
    return "In-loop checks: " + ", ".join(parts)


@dataclass
class AgentUsage:
    model_calls: int = 0
    model_attempts: int = 0
    model_retries: int = 0
    tool_calls: int = 0
    tokens: int = 0
    token_usage_known: bool = True
    edit_attempts: int = 0
    chunk_actions: int = 0
    changed_files: int = 0
    diff_lines: int = 0
    search_rounds: int = 0
    symbol_expansions: int = 0
    context_files: int = 0
    check_runs: int = 0
    elapsed_seconds: float = 0.0
    started_at: float = 0.0
    retry_events: list[dict[str, Any]] = field(default_factory=list)

    def within(self, budget: Budget) -> bool:
        return (
            self.model_calls < budget.max_model_calls
            and self.model_attempts < budget.max_model_calls
            and self.tool_calls < budget.max_tool_calls
            and self.token_usage_known
            and self.tokens < budget.max_tokens
            and self.edit_attempts < budget.max_edit_attempts
            and self.search_rounds < budget.max_search_rounds
            and self.symbol_expansions < budget.max_symbol_expansions
            and self.context_files < budget.max_context_files
            and self.check_runs < budget.max_check_runs
            and time.monotonic() - self.started_at < budget.max_wall_seconds
        )


@dataclass(frozen=True)
class AgentResult:
    batch_id: str
    worker_id: str
    proposal: BatchProposal | None
    review_required: bool
    reason: str | None
    usage: AgentUsage
    observations: tuple[Observation, ...]
    # R2 契约变更一次性声明(§2.4):ledger 为确定性证据载荷;model_reason 属
    # G3/R6,在此占位避免二次破坏性契约修改,本组恒为 None。
    ledger: Mapping[str, Any] | None = None
    model_reason: str | None = None


class AgentLoop:
    def __init__(
        self,
        task: RepairTask,
        *,
        worker_id: str,
        model: ModelAdapter,
        executor: ToolExecutor,
        skill_router: SkillRouter | None = None,
        chunking_enabled: bool = False,
        retry_policy: RetryPolicy | None = None,
        tool_limits: ToolLimits | None = None,
        max_recent_observations: int = 10,
        max_observation_chars: int = 4_000,
        max_skill_context_chars: int = 12_000,
        max_skill_scan_bytes: int = 512_000,
        trace_callback: TraceCallback | None = None,
        usage_callback: UsageCallback | None = None,
        budget_started_at: float | None = None,
        evidence_ledger_enabled: bool = True,
        dedup_recent_observations: bool = False,
        prior_attempt_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.task = task
        self.worker_id = worker_id
        self.model = model
        self.executor = executor
        self.skill_router = skill_router
        self.chunking_enabled = chunking_enabled
        self.retry_policy = retry_policy or RetryPolicy(0)
        self.tool_limits = tool_limits or ToolLimits()
        self.max_recent_observations = max_recent_observations
        self.max_observation_chars = max_observation_chars
        self.max_skill_context_chars = max_skill_context_chars
        self.max_skill_scan_bytes = max_skill_scan_bytes
        self.scope_guard = PatchScopeGuard(self.tool_limits)
        self.proposal_error: str | None = None
        self._blocking_tool_failures: list[tuple[str, str]] = []
        self.trace_callback = trace_callback
        self.usage_callback = usage_callback
        self.budget_started_at = budget_started_at
        self.chunk_executor = ChunkExecutor()
        self.evidence_ledger_enabled = evidence_ledger_enabled
        self.dedup_recent_observations = dedup_recent_observations
        self.prior_attempt_evidence = dict(prior_attempt_evidence) if prior_attempt_evidence is not None else None
        self._ledger: EvidenceLedger | None = None

    def run(self, batch_id: str, issues: Sequence[Issue]) -> AgentResult:
        self._blocking_tool_failures = []
        self._ledger = None
        usage = AgentUsage(started_at=self.budget_started_at or time.monotonic())
        self._persist_usage(usage)
        observations: list[Observation] = []
        if len({issue_id(issue) for issue in issues}) != len(issues):
            return self._review(batch_id, usage, observations, "worker requires review")
        try:
            skill_injection = self._skills(issues, deadline=usage.started_at + self.task.budget.max_wall_seconds)
        except SkillContextError as exc:
            reason = "skill context deadline exceeded" if "deadline" in str(exc) else "skill context limit exceeded"
            return self._review(batch_id, usage, observations, reason)
        ledger = EvidenceLedger(task_id=self.task.task_id, worker_id=self.worker_id) if self.evidence_ledger_enabled else None
        self._ledger = ledger
        state: dict[str, Any] = {
            "worker_id": self.worker_id,
            "batch_id": batch_id,
            "workspace_revision": self.executor.workspace.revision,
            "skill_injection": skill_injection,
            "issue_ids": [issue_id(issue) for issue in issues],
        }
        if ledger is not None:
            state["evidence_ledger"] = ledger.to_payload()
        if self.prior_attempt_evidence is not None:
            # deep copy via canonical_json round-trip: the new ledger never writes into the injected copy (§2.4)
            state["prior_attempt_evidence"] = json.loads(canonical_json(self.prior_attempt_evidence))

        while usage.within(self.task.budget):
            usage.model_calls += 1
            self._persist_usage(usage)
            try:
                decision = self.retry_policy.run(
                    lambda: self.model.decide_with_deadline(
                        self.task, state, self._prompt_observations(observations), self.executor.registry.schemas(),
                        deadline=usage.started_at + self.task.budget.max_wall_seconds,
                        token_limit=self.task.budget.max_tokens - usage.tokens,
                    ),
                    on_attempt=lambda: self._record_attempt(usage),
                    on_retry=lambda error, delay: self._record_retry(usage, error, delay),
                    before_attempt=lambda: "wall-clock budget exhausted before model attempt" if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds else None,
                    can_wait=lambda delay: (
                        usage.model_attempts < self.task.budget.max_model_calls
                        and time.monotonic() - usage.started_at + delay < self.task.budget.max_wall_seconds
                    ),
                )
                if not decision.usage.reported:
                    usage.token_usage_known = False
                    self._persist_usage(usage)
                    return self._review(batch_id, usage, observations, "model token usage unavailable; stopped to preserve budget")
                usage.tokens += decision.usage.total_tokens
                usage.token_usage_known = True
                self._persist_usage(usage)
            except ModelDeadlineExceeded:
                usage.token_usage_known = False
                self._persist_usage(usage)
                return self._review(batch_id, usage, observations, "wall-clock budget exhausted")
            except ModelProtocolError as exc:
                usage.token_usage_known = False
                self._persist_usage(usage)
                return self._review(batch_id, usage, observations, "invalid model protocol")
            except ModelError as exc:
                usage.token_usage_known = not exc.usage_unknown
                self._persist_usage(usage)
                if exc.category == "TOKEN_BUDGET":
                    return self._review(batch_id, usage, observations, "token budget exhausted")
                if exc.category == "BUDGET_EXHAUSTED":
                    return self._review(batch_id, usage, observations, "wall-clock budget exhausted")
                if exc.retryable and usage.model_attempts >= self.task.budget.max_model_calls:
                    return self._review(batch_id, usage, observations, "model request attempt budget exhausted")
                category = exc.category if exc.category in {"NOT_CONFIGURED", "UNSUPPORTED", "TOKEN_BUDGET", "MODEL_HTTP", "MODEL_TRANSPORT", "MODEL_PROTOCOL"} else "MODEL_ERROR"
                return self._review(batch_id, usage, observations, f"{category}: model request failed")
            except Exception as exc:
                usage.token_usage_known = False
                self._persist_usage(usage)
                return self._review(batch_id, usage, observations, f"unexpected model failure: {type(exc).__name__}")

            if usage.tokens > self.task.budget.max_tokens:
                return self._review(batch_id, usage, observations, "token budget exhausted")

            if decision.kind == "tool_call" and decision.tool_call is not None:
                requested_check_runs = 0
                if decision.tool_call.name == "run_checks":
                    raw_names = decision.tool_call.arguments.get("names") if isinstance(decision.tool_call.arguments, Mapping) else None
                    requested_check_runs = len(raw_names) if isinstance(raw_names, list) else 0
                blocked = self._tool_budget_error(usage, decision.tool_call.name, requested_check_runs=requested_check_runs)
                if blocked:
                    return self._review(batch_id, usage, observations, blocked)
                usage.tool_calls += 1
                if decision.tool_call.name == "edit_file":
                    usage.edit_attempts += 1
                if decision.tool_call.name == "search_code":
                    usage.search_rounds += 1
                if decision.tool_call.name == "list_symbols":
                    usage.symbol_expansions += 1
                self._persist_usage(usage)
                observation = self.executor.execute(decision.tool_call, expected_workspace_revision=self.executor.workspace.revision, deadline=usage.started_at + self.task.budget.max_wall_seconds)
                self._record(observation, observations, ledger, usage)
                if ledger is not None:
                    state["evidence_ledger"] = ledger.to_payload()
                if decision.tool_call.name == "run_checks" and isinstance(observation.content, Mapping) and isinstance(observation.content.get("checks"), list):
                    # executed checks burn budget; pre-execution rejections carry no "checks" list and burn none
                    usage.check_runs += len(observation.content["checks"])
                    self._persist_usage(usage)
                state["workspace_revision"] = self.executor.workspace.revision
                continue

            if decision.kind == "action_chunk":
                if not self.chunking_enabled:
                    return self._review(batch_id, usage, observations, "action chunk rejected because chunking is disabled")
                try:
                    chunk = self._parse_chunk(decision.action_chunk)
                except (TypeError, ValueError) as exc:
                    return self._review(batch_id, usage, observations, "invalid action chunk")
                remaining = self.task.budget.max_tool_calls - usage.tool_calls
                action_limit = min(self.task.budget.max_chunk_actions, remaining)
                result = self.chunk_executor.execute(
                    chunk, self.executor,
                    expected_workspace_revision=self.executor.workspace.revision,
                    max_actions=action_limit,
                    before_action=lambda action: self._chunk_budget_error(usage, action),
                    deadline=usage.started_at + self.task.budget.max_wall_seconds,
                )
                usage.chunk_actions += len(result.completed)
                self._persist_usage(usage)
                for observation in (*result.completed, *result.not_executed):
                    self._record(observation, observations, ledger, usage)
                if ledger is not None:
                    state["evidence_ledger"] = ledger.to_payload()
                state["workspace_revision"] = self.executor.workspace.revision
                if not result.accepted:
                    reason = result.reason if result.reason in _CHUNK_BUDGET_REASONS else "action chunk incomplete"
                    return self._review(batch_id, usage, observations, reason)
                continue

            if decision.kind == "batch_ready":
                if ledger is not None:
                    # batch_ready-only ingestion point; claims are redacted/bounded inside the ledger
                    ledger.set_model_fields(decision.hypothesis, decision.next_questions, decision.attempt_summary)
                if self._blocking_tool_failures:
                    details = ", ".join(f"{name}/{status}" for name, status in self._blocking_tool_failures[:8])
                    return self._review(batch_id, usage, observations, f"tool observation incomplete: {details}")
                blocked = self._tool_budget_error(usage, "git_diff")
                if blocked:
                    return self._review(batch_id, usage, observations, blocked)
                proposal = self._proposal(batch_id, issues, decision, usage, observations, ledger)
                if proposal is None:
                    return self._review(batch_id, usage, observations, self.proposal_error or "could not obtain a complete real Git diff", model_reason=decision.reason)
                return AgentResult(batch_id, self.worker_id, proposal, False, None, usage, tuple(observations), ledger=ledger.to_payload() if ledger is not None else None)

            if decision.kind == "review_required":
                # durable reason stays fixed; the model's own words ride model_reason only
                return self._review(batch_id, usage, observations, "model requested human review", model_reason=decision.reason)

            return self._review(batch_id, usage, observations, "unsupported model decision")

        reasons = []
        if usage.model_calls >= self.task.budget.max_model_calls:
            reasons.append("model call budget exhausted")
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            reasons.append("tool call budget exhausted")
        if usage.tokens >= self.task.budget.max_tokens:
            reasons.append("token budget exhausted")
        if not usage.token_usage_known:
            reasons.append("model token usage unavailable; stopped to preserve budget")
        if usage.edit_attempts >= self.task.budget.max_edit_attempts:
            reasons.append("edit attempt budget exhausted")
        if usage.search_rounds >= self.task.budget.max_search_rounds:
            reasons.append("search round budget exhausted")
        if usage.symbol_expansions >= self.task.budget.max_symbol_expansions:
            reasons.append("symbol expansion budget exhausted")
        if usage.context_files >= self.task.budget.max_context_files:
            reasons.append("context file budget exhausted")
        if usage.check_runs >= self.task.budget.max_check_runs:
            reasons.append("check run budget exhausted")
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            reasons.append("wall-clock budget exhausted")
        return self._review(batch_id, usage, observations, "; ".join(reasons) or "budget exhausted")

    def _record(self, observation: Observation, observations: list[Observation], ledger: EvidenceLedger | None, usage: AgentUsage) -> None:
        observations.append(observation)
        if ledger is not None:
            # 确定性证据派生的唯一入口(§2.2):chunk observations 也经由这里进账本。
            ledger.record(observation)
        # context_files 的精确口径:observations 里去重的证据文件数(source_paths
        # 覆盖 read/search/list_symbols/git_diff 触碰过的文件)。执行前 enforcement
        # 用的近似口径见 _tool_budget_error 注释。
        usage.context_files = len({path for item in observations for path in item.source_paths})
        if not observation.complete or observation.status in {
            ToolStatus.ERROR, ToolStatus.PARTIAL, ToolStatus.TRUNCATED, ToolStatus.VERSION_CHANGED,
            ToolStatus.AMBIGUOUS, ToolStatus.UNSUPPORTED, ToolStatus.NOT_EXECUTED,
        }:
            self._blocking_tool_failures.append((observation.tool, observation.status.value))
        if self.trace_callback:
            self.trace_callback(observation)

    def _record_attempt(self, usage: AgentUsage) -> None:
        usage.model_attempts += 1
        # Persist uncertainty before crossing the provider boundary. A crash
        # after remote acceptance but before usage persistence then fails closed.
        usage.token_usage_known = False
        self._persist_usage(usage)

    def _persist_usage(self, usage: AgentUsage) -> None:
        usage.elapsed_seconds = max(0.0, time.monotonic() - usage.started_at) if usage.started_at else 0.0
        if self.usage_callback:
            self.usage_callback(usage)

    def _record_retry(self, usage: AgentUsage, error: ModelError, delay: float) -> None:
        usage.model_retries += 1
        usage.retry_events.append({"category": error.category, "attempt": usage.model_attempts, "delay_seconds": round(delay, 3)})
        self._persist_usage(usage)

    def _tool_budget_error(self, usage: AgentUsage, name: str, requested_check_runs: int = 0) -> str | None:
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            return "tool call budget exhausted"
        if name == "edit_file" and usage.edit_attempts >= self.task.budget.max_edit_attempts:
            return "edit attempt budget exhausted"
        if name == "search_code" and usage.search_rounds >= self.task.budget.max_search_rounds:
            return "search round budget exhausted"
        if name == "list_symbols" and usage.symbol_expansions >= self.task.budget.max_symbol_expansions:
            return "symbol expansion budget exhausted"
        if name == "run_checks" and usage.check_runs + requested_check_runs > self.task.budget.max_check_runs:
            # all-or-nothing: a multi-name call with budget for fewer runs nothing
            return "check run budget exhausted"
        if name in {"search_code", "list_symbols"} and len(self.executor.workspace.observed_hashes) >= self.task.budget.max_context_files:
            # 近似口径:context_files 的精确计数是 observations 去重证据文件数
            # (事后统计),执行前只能用已观察文件数(observed_hashes,含 warning
            # 初始文件)近似;宁可在边界上早停,不做放行假设。
            return "context file budget exhausted"
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            return "wall-clock budget exhausted"
        return None

    def _chunk_budget_error(self, usage: AgentUsage, action: Any = None) -> str | None:
        # run_checks 的 check-runs 预算门在此刻意缺席(设计 §1.7 站点 6):BoundaryDetector
        # 会先以 non-read-only 拒绝任何包含 run_checks 的 chunk(chunking.py),该门不可达。
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            return "tool call budget exhausted during action chunk"
        name = str(getattr(action, "tool", ""))
        if name == "search_code" and usage.search_rounds >= self.task.budget.max_search_rounds:
            return "search round budget exhausted"
        if name == "list_symbols" and usage.symbol_expansions >= self.task.budget.max_symbol_expansions:
            return "symbol expansion budget exhausted"
        if name in {"search_code", "list_symbols"} and len(self.executor.workspace.observed_hashes) >= self.task.budget.max_context_files:
            # 同 _tool_budget_error 的近似口径:用已观察文件数判断 context_files。
            return "context file budget exhausted"
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            return "wall-clock budget exhausted during action chunk"
        usage.tool_calls += 1
        if name == "search_code":
            usage.search_rounds += 1
        if name == "list_symbols":
            usage.symbol_expansions += 1
        self._persist_usage(usage)
        return None

    def _prompt_observations(self, observations: Sequence[Observation]) -> list[dict[str, Any]]:
        cutoff = max(0, len(observations) - self.max_recent_observations)
        older = observations[:cutoff]
        counts: dict[str, int] = {}
        for item in older:
            key = f"{item.tool}:{item.status.value}"
            counts[key] = counts.get(key, 0) + 1
        result: list[dict[str, Any]] = []
        if older:
            errors = [item.error[:self.max_observation_chars] for item in older if item.error][-5:]
            result.append({"historical_summary": {"count": len(older), "tool_status_counts": counts, "source_paths": sorted({path for item in older for path in item.source_paths})[:50], "errors": errors}})
        pinned = [
            {"tool": item.tool, "source_paths": list(item.source_paths), "file_hashes": dict(item.file_hashes)}
            for item in observations if item.status == ToolStatus.OK and item.file_hashes
        ][-8:]
        if pinned:
            result.append({"pinned_evidence": pinned})
        seen_reads: dict[tuple[str, int, int, str], str] = {}  # dedup key -> first occurrence's tool_call_id
        for item in observations[cutoff:]:
            dedup_key = self._read_dedup_key(item) if self.dedup_recent_observations else None
            if dedup_key is not None and dedup_key in seen_reads:
                # later byte-identical read ⇒ self-describing single-hop reference (§2.6)
                content = item.content
                result.append({
                    "tool_call_id": item.tool_call_id,
                    "tool": item.tool,
                    "status": item.status.value,
                    "observation_ref": {
                        "path": content.get("path"),
                        "start_line": content.get("start_line"),
                        "end_line": content.get("end_line"),
                        "content_hash": content.get("content_hash"),
                        "replays_tool_call_id": seen_reads[dedup_key],
                    },
                })
                continue
            if dedup_key is not None:
                seen_reads[dedup_key] = item.tool_call_id
            primitive = to_primitive(item)
            content = primitive.get("content")
            content_text = canonical_json(content) if content is not None else ""
            if len(content_text) > self.max_observation_chars:
                primitive["content"] = content_text[: self.max_observation_chars] + "…[truncated]"
                primitive["complete"] = False
            result.append(primitive)
        return result

    @staticmethod
    def _read_dedup_key(item: Observation) -> tuple[str, int, int, str] | None:
        """Eligibility for in-window dedup: byte-identical read_file observations only (§2.6)."""
        if item.tool != "read_file" or item.status != ToolStatus.OK or item.complete is not True:
            return None
        content = item.content
        if not isinstance(content, Mapping):
            return None
        path = content.get("path")
        start_line = content.get("start_line")
        end_line = content.get("end_line")
        content_hash = content.get("content_hash")
        if not isinstance(path, str) or not path or not isinstance(start_line, int) or not isinstance(end_line, int):
            return None
        if not isinstance(content_hash, str) or not content_hash or not isinstance(content.get("text"), str):
            return None
        return (path, start_line, end_line, content_hash)

    def _review(self, batch_id: str, usage: AgentUsage, observations: list[Observation], reason: str, model_reason: str | None = None) -> AgentResult:
        usage.elapsed_seconds = max(0.0, time.monotonic() - usage.started_at) if usage.started_at else 0.0
        self._persist_usage(usage)
        ledger = self._ledger.to_payload() if self._ledger is not None else None
        redacted_model_reason = _cap_model_reason(redact_text(str(model_reason))) if model_reason is not None else None
        return AgentResult(batch_id, self.worker_id, None, True, _safe_review_reason(reason), usage, tuple(observations), ledger=ledger, model_reason=redacted_model_reason)

    def _skills(self, issues: Sequence[Issue], *, deadline: float | None = None) -> str:
        if self.skill_router is None:
            return "No Skill store configured."
        records = self.skill_router.route_many(
            tuple(issues),
            max_skill_bytes=self.tool_limits.max_file_bytes,
            max_scan_bytes=self.max_skill_scan_bytes,
            max_context_chars=self.max_skill_context_chars,
            deadline=deadline,
        )
        if not records:
            return "No matching versioned skill is configured. Current source evidence remains authoritative."
        return "\n\n".join(f"[Skill {skill.skill_id} v{skill.version} from {skill.source}]\n{skill.content}" for skill in records)

    def _parse_chunk(self, value: Any) -> ActionChunk:
        if not isinstance(value, Mapping):
            raise ValueError("action_chunk must be an object")
        raw_actions = value.get("actions", ())
        if not isinstance(raw_actions, list):
            raise ValueError("action_chunk.actions must be a list")
        actions: list[ChunkAction] = []
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, Mapping) or not raw.get("tool"):
                raise ValueError(f"invalid action at index {index}")
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"arguments must be an object at index {index}")
            actions.append(ChunkAction(str(raw["tool"]), arguments, str(raw.get("action_id", f"action-{index}")), tuple(str(item) for item in raw.get("depends_on", ()))))
        return ActionChunk(str(value.get("chunk_id", "chunk")), tuple(actions))

    def _proposal(self, batch_id: str, issues: Sequence[Issue], decision: ModelDecision, usage: AgentUsage, observations: list[Observation], ledger: EvidenceLedger | None = None) -> BatchProposal | None:
        diff_call = ToolCall("git_diff", {}, f"diff-{batch_id}")
        usage.tool_calls += 1
        self._persist_usage(usage)
        diff_observation = self.executor.execute(diff_call, expected_workspace_revision=self.executor.workspace.revision, deadline=usage.started_at + self.task.budget.max_wall_seconds)
        self._record(diff_observation, observations, ledger, usage)
        # The proposal must never use a model-written diff. Only the Git tool output is authoritative.
        if diff_observation.status not in {ToolStatus.OK, ToolStatus.EMPTY}:
            return None
        payload = diff_observation.content or {}
        diff = str(payload.get("diff", "")) if isinstance(payload, Mapping) else ""
        untracked = list(payload.get("untracked_files", ())) if isinstance(payload, Mapping) else []
        changed_files = set(untracked)
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                changed_files.add(line[6:])
            elif line.startswith("--- a/"):
                changed_files.add(line[6:])
        scope = self.scope_guard.inspect(diff, changed_files)
        usage.changed_files = scope.changed_files
        usage.diff_lines = scope.diff_lines
        self._persist_usage(usage)
        if scope.violations:
            self.proposal_error = "patch scope requires review: " + "; ".join(scope.violations)
            return None
        action_map: dict[str, ActionKind] = {}
        for issue in issues:
            raw = decision.action_map.get(issue_id(issue), ActionKind.UNRESOLVED.value)
            try:
                action = ActionKind(raw)
            except ValueError:
                action = ActionKind.REVIEW_REQUIRED
            if action == ActionKind.SUPPRESSION_CANDIDATE and not any("evidence" in str(item.content).lower() for item in observations):
                action = ActionKind.REVIEW_REQUIRED
            if action == ActionKind.SUPPRESSION_CANDIDATE and "evidence" not in (decision.reason or "").lower():
                action = ActionKind.REVIEW_REQUIRED
            action_map[issue_id(issue)] = action
        if any(action == ActionKind.FIX_CANDIDATE for action in action_map.values()) and not diff and not changed_files:
            for identifier, action in list(action_map.items()):
                if action == ActionKind.FIX_CANDIDATE:
                    action_map[identifier] = ActionKind.REVIEW_REQUIRED
        unresolved = tuple(identifier for identifier, action in action_map.items() if action in {ActionKind.UNRESOLVED, ActionKind.REVIEW_REQUIRED})
        review_notes = (
            "Proposal only; it is not a frozen or verified candidate.",
            f"Patch scope: {scope.changed_files} files, {scope.diff_lines} changed lines ({scope.added_lines} added, {scope.deleted_lines} deleted).",
        )
        check_note = _check_summary_note(observations)
        if check_note:
            review_notes = (*review_notes, check_note)
        return BatchProposal(
            batch_id=batch_id,
            worker_id=self.worker_id,
            base_commit=self.task.base_commit,
            workspace_revision=self.executor.workspace.revision,
            action_map=action_map,
            changed_files=tuple(sorted(changed_files)),
            diff=diff,
            diff_hash=sha256_text(diff),
            risk=max((getattr(issue, "risk", RiskClass.UNKNOWN) for issue in issues), key=lambda item: {RiskClass.UNKNOWN: 0, RiskClass.LOW: 1, RiskClass.MEDIUM: 2, RiskClass.HIGH: 3}[item]),
            unresolved=unresolved,
            complete=not unresolved,
            review_notes=review_notes,
        )
