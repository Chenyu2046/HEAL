"""Fixture-based context efficiency and retrieval quality benchmark (方案 §18).

SCOPE NOTE (报告头部口径): this is a fixture-based benchmark over deterministic
synthetic C/C++ repositories. The real 800-warning dataset experiments and the
three-arm A/B comparison of 方案 §19 are **notCovered** — they require the
warning dataset and are intentionally out of scope for this harness.

Everything is deterministic: fixtures are static strings, the decision sequence
is fixed, no random source is used. The script is standard-library only and
lives outside the packaged `src/` tree on purpose.

CLI:
    py -3.13 scripts/benchmark.py context   # cache on/off efficiency run
    py -3.13 scripts/benchmark.py recall    # File Recall@3/@5 + Symbol Recall@5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest.mock
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.agent import AgentLoop
from repair_agent.context import ContextCache
from repair_agent.domain import Budget, Finding, RepairTask, Severity
from repair_agent.models import ScriptedModel
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.executor import ToolExecutor
from repair_agent.tools.source import SearchRankingContext

_SCOPE_NOTE = (
    "fixture-based benchmark over deterministic synthetic C/C++ repositories; "
    "the real 800-warning dataset experiments and the three-arm A/B of 方案 §19 "
    "are notCovered (the dataset is required for those)"
)

_METRIC_DEFINITIONS = {
    "physical_evidence_reads": (
        "in-process file content reads (Path.read_text/read_bytes) performed by the tools layer during "
        "the repair loop; version-check hashing (refresh/mark_read) is excluded, edit_file's single "
        "hash-verification read is included and identical in both arms"
    ),
    "repeated_physical_reads": (
        "方案 §18 'Duplicate Physical File Reads / Task': physical_evidence_reads minus distinct paths"
    ),
    "logical_cache_hits": (
        "logical tool calls served from ContextCache, counted separately per §18 (0 when the cache is off)"
    ),
}


# ---------------------------------------------------------------- context fixture

_MANAGER_CPP = """#include "plugin.h"

namespace audio {

AudioManager::AudioManager() : plugin_(nullptr) {
}

void AudioManager::init(Plugin* plugin) {
    plugin_ = plugin;
}

Plugin* AudioManager::getPlugin() {
    return plugin_;
}

void AudioManager::process() {
    // warning target: unchecked dereference of plugin_
    process_packet(plugin_);
}

void AudioManager::shutdown() {
    plugin_ = nullptr;
}

}
"""

_PLUGIN_H = """#ifndef AUDIO_PLUGIN_H
#define AUDIO_PLUGIN_H

namespace audio {

class Plugin {
public:
    void load();
    void unload();
    int frames() const;

private:
    int frames_;
};

}

#endif
"""

_PLUGIN_CPP = """#include "plugin.h"

namespace audio {

void Plugin::load() {
    frames_ = 64;
}

void Plugin::unload() {
    frames_ = 0;
}

int Plugin::frames() const {
    return frames_;
}

}
"""

_ENGINE_CPP = """#include "manager.h"

namespace core {

void Engine::start() {
    AudioManager manager;
    manager.init(manager.getPlugin());
    manager.process();
}

}
"""

_LOG_CPP = """#include <cstddef>

namespace util {

void log_write(const char* message, size_t length) {
    (void)message;
    (void)length;
}

}
"""


def build_context_fixture(root: Path) -> tuple[Path, str, tuple[Finding, ...], list[dict]]:
    """Create the synthetic repair repo; returns (repo, base_commit, issues, decisions).

    完全静态:同样的输入目录产生逐字节相同的文件,edit 的 expected_hash 因此可预计算。
    """
    repo = root / "repo"
    (repo / "src" / "audio").mkdir(parents=True)
    (repo / "src" / "core").mkdir(parents=True)
    (repo / "src" / "util").mkdir(parents=True)
    # 全部用 write_bytes 写 LF 字节,避免 Windows 文本模式换行翻译破坏确定性 hash。
    (repo / ".gitignore").write_bytes(b"build/\n")
    (repo / "src" / "audio" / "manager.cpp").write_bytes(_MANAGER_CPP.encode("utf-8"))
    (repo / "src" / "audio" / "plugin.h").write_bytes(_PLUGIN_H.encode("utf-8"))
    (repo / "src" / "audio" / "plugin.cpp").write_bytes(_PLUGIN_CPP.encode("utf-8"))
    (repo / "src" / "core" / "engine.cpp").write_bytes(_ENGINE_CPP.encode("utf-8"))
    (repo / "src" / "util" / "log.cpp").write_bytes(_LOG_CPP.encode("utf-8"))
    (repo / "src" / "audio" / "manager.h").write_bytes(
        "namespace audio {\nclass Plugin;\nclass AudioManager {\npublic:\n    AudioManager();\n    void init(Plugin* plugin);\n    Plugin* getPlugin();\n    void process();\n    void shutdown();\nprivate:\n    Plugin* plugin_;\n};\n}\n".encode("utf-8")
    )
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "HEAL benchmark")
    _git(repo, "config", "user.email", "heal-bench@localhost")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "base", "--quiet")
    base_commit = _git(repo, "rev-parse", "HEAD")
    issues = (
        Finding(
            "finding-1", "NULL_PLUGIN_DEREF", Severity.HIGH, "src/audio/manager.cpp", 21,
            "plugin_ may be null when process_packet is called",
            symbol="AudioManager", module="audio",
        ),
    )
    expected_hash = hashlib.sha256(_MANAGER_CPP.encode("utf-8")).hexdigest()
    decisions = [
        {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "getPlugin"}}},
        {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/audio/manager.cpp", "start_line": 1, "end_line": 30}}},
        # 重复逻辑读取:cache on 时应命中 FileContextCache,不再产生物理读取。
        {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/audio/manager.cpp", "start_line": 1, "end_line": 30}}},
        {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": "src/audio/plugin.cpp"}}},
        {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/audio/plugin.cpp", "start_line": 1, "end_line": 30}}},
        {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/audio/plugin.cpp", "start_line": 1, "end_line": 30}}},
        # 重复搜索:cache on 时应命中 SearchResultCache。
        {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "getPlugin"}}},
        {"kind": "tool_call", "tool_call": {"name": "edit_file", "arguments": {
            "path": "src/audio/manager.cpp", "expected_hash": expected_hash,
            "old_text": "    process_packet(plugin_);",
            "new_text": "    if (plugin_ != nullptr) {\n        process_packet(plugin_);\n    }",
        }}},
        {"kind": "batch_ready", "action_map": {"finding-1": "FIX_CANDIDATE"}},
    ]
    return repo, base_commit, issues, decisions


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class _EvidenceReadCounter:
    """Count physical file-content reads (Path.read_text/read_bytes) inside the repair loop.

    口径见 _METRIC_DEFINITIONS:版本校验 hash(refresh/mark_read 走 open())不算;
    edit_file 的 hash 校验读算一次(两臂相同)。
    """

    def __init__(self) -> None:
        self.reads: list[str] = []

    def __enter__(self) -> "_EvidenceReadCounter":
        real_read_text = Path.read_text
        real_read_bytes = Path.read_bytes
        counter = self

        def counting_read_text(path, *args, **kwargs):
            counter.reads.append(str(path))
            return real_read_text(path, *args, **kwargs)

        def counting_read_bytes(path, *args, **kwargs):
            counter.reads.append(str(path))
            return real_read_bytes(path, *args, **kwargs)

        self._patchers = (
            unittest.mock.patch.object(Path, "read_text", counting_read_text),
            unittest.mock.patch.object(Path, "read_bytes", counting_read_bytes),
        )
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, *exc_info) -> None:
        for patcher in self._patchers:
            patcher.stop()

    def summary(self) -> dict[str, int]:
        distinct = len(set(self.reads))
        return {
            "physical_evidence_reads": len(self.reads),
            "distinct_files_read": distinct,
            "repeated_physical_reads": len(self.reads) - distinct,
        }


def run_context_arm(root: Path, *, cache_enabled: bool) -> dict[str, Any]:
    """Drive one scripted end-to-end repair and collect context efficiency metrics."""
    repo, base_commit, issues, decisions = build_context_fixture(root)
    cache = ContextCache() if cache_enabled else None
    workspace = WorkspaceState(repo, base_commit)
    # search_backend 固定为 python:rg 子进程内的证据读取不可观测,且保证跨机器确定性。
    executor = ToolExecutor(workspace, cache=cache, search_backend="python")
    task = RepairTask(
        "bench-task", "bench-run", str(repo), base_commit, issues,
        budget=Budget(max_model_calls=20, max_tool_calls=50),
    )
    loop = AgentLoop(task, worker_id="bench-worker", model=ScriptedModel(decisions), executor=executor)
    counter = _EvidenceReadCounter()
    started = time.perf_counter()
    with counter:
        result = loop.run("bench-batch", issues)
    wall_seconds = time.perf_counter() - started
    tool_calls_by_name: dict[str, int] = {}
    for observation in result.observations:
        tool_calls_by_name[observation.tool] = tool_calls_by_name.get(observation.tool, 0) + 1
    return {
        "cache_enabled": cache_enabled,
        "model_calls": result.usage.model_calls,
        "tool_calls_by_name": dict(sorted(tool_calls_by_name.items())),
        "physical_evidence_reads": counter.summary()["physical_evidence_reads"],
        "distinct_files_read": counter.summary()["distinct_files_read"],
        "repeated_physical_reads": counter.summary()["repeated_physical_reads"],
        "logical_cache_hits": sum(v for k, v in cache.stats().items() if k.endswith("_hits")) if cache is not None else 0,
        "cache_stats": cache.stats() if cache is not None else None,
        "wall_seconds": round(wall_seconds, 3),
        "repair_completed": result.proposal is not None,
        "review_required": result.review_required,
        "review_reason": result.reason,
    }


def build_context_report() -> dict[str, Any]:
    runs: dict[str, Any] = {}
    for label, enabled in (("cache_off", False), ("cache_on", True)):
        with tempfile.TemporaryDirectory() as temporary:
            runs[label] = run_context_arm(Path(temporary), cache_enabled=enabled)
    off, on = runs["cache_off"], runs["cache_on"]
    return {
        "scope": _SCOPE_NOTE,
        "benchmark": "context",
        "metric_definitions": _METRIC_DEFINITIONS,
        "runs": runs,
        "comparison": {
            "physical_reads_off": off["physical_evidence_reads"],
            "physical_reads_on": on["physical_evidence_reads"],
            "repeated_reads_off": off["repeated_physical_reads"],
            "repeated_reads_on": on["repeated_physical_reads"],
            "logical_cache_hits_on": on["logical_cache_hits"],
            "model_calls_equal": off["model_calls"] == on["model_calls"],
            "both_repairs_completed": off["repair_completed"] and on["repair_completed"],
        },
    }


# ---------------------------------------------------------------- recall fixture

_RECALL_FILES = {
    "src/audio/manager.cpp": (
        '#include "manager.h"\n\nnamespace audio {\n\n'
        "Plugin* AudioManager::getPlugin() {\n    return plugin_;\n}\n\n"
        "void AudioManager::process() {\n    Plugin* plugin = getPlugin();\n    if (plugin) {\n        plugin->load();\n    }\n}\n\n}\n"
    ),
    "src/audio/plugin.cpp": (
        '#include "plugin.h"\n\nnamespace audio {\n\nvoid Plugin::load() {\n    frames_ = 64;\n}\n\n'
        "void Plugin::unload() {\n    frames_ = 0;\n}\n\n}\n"
    ),
    "src/core/engine.cpp": (
        '#include "engine.h"\n\nnamespace core {\n\nvoid Engine::start() {\n    // start drives the audio pipeline\n    scheduler_tick();\n}\n\n}\n'
    ),
    "src/net/transport.cpp": (
        "#include <cstddef>\n\nnamespace net {\n\n"
        "void packet_send(const unsigned char* data, size_t length) {\n    (void)data;\n    (void)length;\n}\n\n"
        "void packet_flush() {\n    packet_send(nullptr, 0);\n}\n\n}\n"
    ),
    "src/util/logger.cpp": (
        "#include <cstddef>\n\nnamespace util {\n\n"
        "void log_write(const char* message, size_t length) {\n    (void)message;\n    (void)length;\n}\n\n}\n"
    ),
}


@dataclass(frozen=True)
class RecallCase:
    query: str
    expected_file: str
    expected_symbol: str
    ranking_files: tuple[str, ...] = ()
    ranking_symbols: tuple[str, ...] = ()


RECALL_CASES: tuple[RecallCase, ...] = (
    RecallCase("getPlugin", "src/audio/manager.cpp", "getPlugin", ranking_files=("src/audio/manager.cpp",)),
    RecallCase("packet_send", "src/net/transport.cpp", "packet_send"),
    RecallCase("log_write", "src/util/logger.cpp", "log_write"),
    RecallCase("unload", "src/audio/plugin.cpp", "unload"),
    RecallCase("AudioManager", "src/audio/manager.cpp", "process", ranking_symbols=("AudioManager",)),
)


def build_recall_fixture(root: Path) -> Path:
    """Synthetic multi-file repo with planted symbols; deterministic, no randomness."""
    repo = root / "repo"
    for relative, content in _RECALL_FILES.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # 与 build_context_fixture 同一确定性纪律:write_bytes 写 LF 字节,
        # 避免 Windows 文本模式把 \n 翻译成 \r\n 破坏跨平台逐字节一致性。
        target.write_bytes(content.encode("utf-8"))
    return repo


def compute_recall_stats(per_case: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case hit flags into 方案 §18 recall metrics (pure function)."""
    cases = len(per_case)
    if cases == 0:
        return {"cases": 0, "file_recall_at_3": 0.0, "file_recall_at_5": 0.0, "symbol_recall_at_5": 0.0, "per_case": []}
    return {
        "cases": cases,
        "file_recall_at_3": sum(1 for item in per_case if item["file_hit_at_3"]) / cases,
        "file_recall_at_5": sum(1 for item in per_case if item["file_hit_at_5"]) / cases,
        "symbol_recall_at_5": sum(1 for item in per_case if item["symbol_hit_at_5"]) / cases,
        "per_case": per_case,
    }


def run_recall_case(repo: Path, base_commit: str, case: RecallCase) -> dict[str, Any]:
    workspace = WorkspaceState(repo, base_commit)
    ranking = SearchRankingContext(files=case.ranking_files, symbols=case.ranking_symbols)
    executor = ToolExecutor(workspace, ranking_context=ranking, search_backend="python")
    search = executor.execute(type("Call", (), {"name": "search_code", "arguments": {"query": case.query}, "call_id": "recall"})())
    if search.status.value != "OK":
        raise RuntimeError(f"recall search failed for {case.query!r}: {search.error}")
    ranked_files = [item["path"] for item in search.content["files"]]
    symbols = executor.execute(type("Call", (), {"name": "list_symbols", "arguments": {"path": case.expected_file}, "call_id": "symbols"})())
    decl_names = [item["name"] for item in symbols.content["symbols"]] if symbols.status.value == "OK" else []
    # Symbol Recall@5 口径:list_symbols 返回按行序的声明,取前 5 个作为候选集。
    symbol_hit = any(name == case.expected_symbol or name.endswith("::" + case.expected_symbol) for name in decl_names[:5])
    return {
        "query": case.query,
        "expected_file": case.expected_file,
        "expected_symbol": case.expected_symbol,
        "ranked_files": ranked_files,
        "file_hit_at_3": case.expected_file in ranked_files[:3],
        "file_hit_at_5": case.expected_file in ranked_files[:5],
        "file_rank": ranked_files.index(case.expected_file) + 1 if case.expected_file in ranked_files else None,
        "symbol_hit_at_5": symbol_hit,
        "symbol_candidates": decl_names[:5],
    }


def run_recall_benchmark(root: Path) -> dict[str, Any]:
    """File Recall@3/@5 over aggregated search results; Symbol Recall@5 over list_symbols candidates."""
    repo = build_recall_fixture(root)
    per_case = [run_recall_case(repo, "HEAD", case) for case in RECALL_CASES]
    stats = compute_recall_stats(per_case)
    return {
        "scope": _SCOPE_NOTE,
        "benchmark": "recall",
        "metric_definitions": {
            "file_recall_at_k": "方案 §18: expected file within the first K aggregated search-result files",
            "symbol_recall_at_5": "方案 §18: expected function within the first 5 list_symbols declarations",
            "determinism": "static fixture and queries; no random source",
        },
        **stats,
    }


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixture-based context/recall benchmark (方案 §18); see module docstring for the notCovered scope.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("context", help="cache on/off efficiency over a scripted synthetic repair")
    sub.add_parser("recall", help="File Recall@3/@5 and Symbol Recall@5 over planted symbols")
    args = parser.parse_args(argv)
    if args.command == "context":
        report = build_context_report()
    else:
        with tempfile.TemporaryDirectory() as temporary:
            report = run_recall_benchmark(Path(temporary))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
