from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import repair_agent.tools.source as source_module
from repair_agent.agent import AgentLoop
from repair_agent.config import Config, ToolLimits, load_config
from repair_agent.context import ContextCache, FileContextCache, SearchContextEntry, SearchResultCache
from repair_agent.domain import Budget, Finding, RepairTask, Severity, ToolStatus
from repair_agent.models import ScriptedModel, ToolCall
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.executor import ToolExecutor


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "HEAL test")
    git(repo, "config", "user.email", "heal-test@localhost")
    git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_bytes(b"build/\n")
    (repo / "src").mkdir()
    (repo / "src" / "a.c").write_bytes(b"int first = OLD;\nint second = OLD;\n")
    (repo / "src" / "b.c").write_bytes(b"int other = 2;\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


@contextmanager
def counted_evidence_reads():
    """Count SourceTools-level evidence reads.

    refresh()/mark_read() 的重新 hash 走 runtime.workspace 自己的 file_hash
    引用,属既有版本校验语义,不计入;这里只统计 read_file 的内容 hash 与
    search_code 的逐文件读取。
    """
    counter = {"file_hash": 0, "read_text": 0}
    real_file_hash = source_module.file_hash
    real_read_text = Path.read_text

    def counting_hash(path):
        counter["file_hash"] += 1
        return real_file_hash(path)

    def counting_read_text(path, *args, **kwargs):
        counter["read_text"] += 1
        return real_read_text(path, *args, **kwargs)

    with patch.object(source_module, "file_hash", counting_hash), patch.object(Path, "read_text", counting_read_text):
        yield counter


def read_file_call(path: str, **extra) -> ToolCall:
    return ToolCall("read_file", {"path": path, **extra})


def search_call(query: str, **extra) -> ToolCall:
    return ToolCall("search_code", {"query": query, **extra})


class ContextCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo, self.base = make_repo(Path(temporary.name))

    def executor(self, *, cache: ContextCache | None = None, limits: ToolLimits | None = None, workspace: WorkspaceState | None = None, search_backend: str = "auto") -> tuple[ToolExecutor, WorkspaceState]:
        workspace = workspace or WorkspaceState(self.repo, self.base)
        return ToolExecutor(workspace, limits=limits, cache=cache, search_backend=search_backend), workspace

    def test_read_file_hit_does_not_reread_content(self) -> None:
        executor, _ = self.executor(cache=ContextCache())
        with counted_evidence_reads() as counter:
            first = executor.execute(read_file_call("src/a.c"))
            after_first = counter["file_hash"]
            second = executor.execute(read_file_call("src/a.c"))
            after_second = counter["file_hash"]
            slice_read = executor.execute(read_file_call("src/a.c", start_line=2, end_line=2))
            after_slice = counter["file_hash"]
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual(after_first, 1)
        self.assertEqual((second.status, second.content, second.file_hashes, second.complete), (first.status, first.content, first.file_hashes, first.complete))
        self.assertEqual(after_second, 1)
        self.assertEqual(slice_read.status, ToolStatus.OK)
        self.assertEqual(slice_read.content["text"], "int second = OLD;\n")
        self.assertEqual(slice_read.content["content_hash"], first.content["content_hash"])
        self.assertEqual(after_slice, 1)

    def test_edit_file_invalidates_only_edited_file(self) -> None:
        executor, _ = self.executor(cache=ContextCache())
        original = (self.repo / "src" / "a.c").read_bytes()
        with counted_evidence_reads() as counter:
            read_a = executor.execute(read_file_call("src/a.c"))
            read_b = executor.execute(read_file_call("src/b.c"))
            self.assertEqual(counter["file_hash"], 2)
            edit = executor.execute(ToolCall("edit_file", {
                "path": "src/a.c", "expected_hash": hashlib.sha256(original).hexdigest(),
                "old_text": "int first = OLD;", "new_text": "int first = NEW;",
            }))
            self.assertEqual(edit.status, ToolStatus.OK)
            reread_a = executor.execute(read_file_call("src/a.c"))
            after_reread = counter["file_hash"]
            reread_b = executor.execute(read_file_call("src/b.c"))
            after_all = counter["file_hash"]
        self.assertEqual(after_reread, 3)
        self.assertEqual(after_all, 3)
        self.assertEqual(reread_a.status, ToolStatus.OK)
        self.assertEqual(reread_a.content["text"], "int first = NEW;\nint second = OLD;\n")
        self.assertNotEqual(reread_a.content["content_hash"], read_a.content["content_hash"])
        self.assertEqual(reread_b.status, ToolStatus.OK)
        self.assertEqual((reread_b.content, reread_b.file_hashes), (read_b.content, read_b.file_hashes))

    def test_search_hit_and_invalidation(self) -> None:
        # Phase-1 evidence-read counting is defined against the Python scanner
        # (rg 后端不经过 Path.read_text);形状断言适配 Phase 2 的按文件聚合输出。
        executor, _ = self.executor(cache=ContextCache(), search_backend="python")
        with counted_evidence_reads() as counter:
            first = executor.execute(search_call("OLD"))
            scans_after_first = counter["read_text"]
            second = executor.execute(search_call("OLD"))
            scans_after_hit = counter["read_text"]
            scoped = executor.execute(search_call("OLD", paths=["src"]))
            scans_after_scope = counter["read_text"]
            original = (self.repo / "src" / "a.c").read_bytes()
            edit = executor.execute(ToolCall("edit_file", {
                "path": "src/a.c", "expected_hash": hashlib.sha256(original).hexdigest(),
                "old_text": "int first = OLD;", "new_text": "int first = NEW;",
            }))
            self.assertEqual(edit.status, ToolStatus.OK)
            after_edit = executor.execute(search_call("OLD"))
            scans_after_edit = counter["read_text"]
            replayed = executor.execute(search_call("OLD"))
            scans_after_replay = counter["read_text"]
            empty_first = executor.execute(search_call("ZZZ"))
            scans_after_empty = counter["read_text"]
            empty_rescan = executor.execute(search_call("ZZZ"))
            scans_after_rescan = counter["read_text"]
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual(first.content["total_hits"], 2)
        self.assertEqual([item["path"] for item in first.content["files"]], ["src/a.c"])
        self.assertEqual(first.content["files"][0]["hits"], 2)
        self.assertEqual(scans_after_first, 3)
        self.assertEqual((second.status, second.content, second.file_hashes, second.complete), (first.status, first.content, first.file_hashes, first.complete))
        self.assertEqual(scans_after_hit, 3)
        self.assertEqual(scoped.status, ToolStatus.OK)
        self.assertEqual(scans_after_scope, 5)
        self.assertEqual(after_edit.status, ToolStatus.OK)
        self.assertEqual(after_edit.content["total_hits"], 1)
        self.assertEqual(scans_after_edit, 8)
        self.assertEqual((replayed.status, replayed.content, replayed.file_hashes), (after_edit.status, after_edit.content, after_edit.file_hashes))
        self.assertEqual(scans_after_replay, 8)
        self.assertEqual(empty_first.status, ToolStatus.EMPTY)
        self.assertEqual(scans_after_empty, 11)
        self.assertEqual((empty_rescan.status, empty_rescan.content, empty_rescan.error), (empty_first.status, empty_first.content, empty_first.error))
        self.assertEqual(empty_rescan.complete, True)
        self.assertEqual(scans_after_rescan, 14)

    def test_search_empty_then_edit_introducing_match_is_not_stale(self) -> None:
        executor, _ = self.executor(cache=ContextCache(), search_backend="python")
        with counted_evidence_reads() as counter:
            empty = executor.execute(search_call("GUARD_CHECK"))
            scans_before = counter["read_text"]
            original = (self.repo / "src" / "a.c").read_bytes()
            edit = executor.execute(ToolCall("edit_file", {
                "path": "src/a.c", "expected_hash": hashlib.sha256(original).hexdigest(),
                "old_text": "int first = OLD;", "new_text": "int first = OLD;\n// GUARD_CHECK added",
            }))
            self.assertEqual(edit.status, ToolStatus.OK)
            found = executor.execute(search_call("GUARD_CHECK"))
            scans_after = counter["read_text"]
        self.assertEqual(empty.status, ToolStatus.EMPTY)
        self.assertEqual(found.status, ToolStatus.OK)
        self.assertEqual(found.complete, True)
        self.assertEqual(found.content, {
            "files": [{"path": "src/a.c", "hits": 1, "sample_lines": [{"line": 2, "text": "// GUARD_CHECK added"}]}],
            "total_hits": 1,
        })
        self.assertGreater(scans_after, scans_before)

    def test_read_file_empty_range_replay_matches_direct(self) -> None:
        executor, _ = self.executor(cache=ContextCache())
        with counted_evidence_reads() as counter:
            first = executor.execute(read_file_call("src/a.c", start_line=50, end_line=60))
            after_first = counter["file_hash"]
            second = executor.execute(read_file_call("src/a.c", start_line=50, end_line=60))
            after_second = counter["file_hash"]
        self.assertEqual(first.status, ToolStatus.EMPTY)
        self.assertEqual(first.error, "selected range is empty")
        self.assertEqual((second.status, second.content, second.error, second.complete), (first.status, first.content, first.error, first.complete))
        self.assertEqual(after_first, 1)
        self.assertEqual(after_second, 1)

    def test_search_invalidated_when_touched_file_is_deleted(self) -> None:
        (self.repo / "src" / "b.c").write_bytes(b"int other = OLD;\n")
        executor, _ = self.executor(cache=ContextCache(), search_backend="python")
        with counted_evidence_reads() as counter:
            first = executor.execute(search_call("OLD"))
            scans_after_first = counter["read_text"]
            (self.repo / "src" / "b.c").unlink()
            after_delete = executor.execute(search_call("OLD"))
            scans_after_delete = counter["read_text"]
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual({item["path"] for item in first.content["files"]}, {"src/a.c", "src/b.c"})
        self.assertEqual(scans_after_first, 3)
        self.assertEqual(after_delete.status, ToolStatus.OK)
        self.assertEqual({item["path"] for item in after_delete.content["files"]}, {"src/a.c"})
        self.assertEqual(after_delete.complete, True)
        self.assertEqual(scans_after_delete, 5)

    def test_search_partial_is_never_cached(self) -> None:
        executor, workspace = self.executor(cache=ContextCache(), search_backend="python")
        with counted_evidence_reads() as counter:
            limited_first = executor.execute(search_call("OLD", max_results=1))
            scans_after_first = counter["read_text"]
            limited_second = executor.execute(search_call("OLD", max_results=1))
            scans_after_second = counter["read_text"]
        self.assertEqual(limited_first.status, ToolStatus.PARTIAL)
        self.assertEqual(limited_first.complete, False)
        self.assertEqual(limited_second.status, ToolStatus.PARTIAL)
        self.assertGreater(scans_after_second, scans_after_first)
        source = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, cache=ContextCache(), search_backend="python")
        deadline_partial = source.search_code({"query": "OLD", "_deadline": 0.0})
        self.assertEqual(deadline_partial[0], ToolStatus.PARTIAL)
        self.assertEqual(len(source.cache.searches), 0)

    def test_cache_disabled_matches_current_behavior(self) -> None:
        executor, _ = self.executor(cache=None)
        with counted_evidence_reads() as counter:
            first = executor.execute(read_file_call("src/a.c"))
            after_first = counter["file_hash"]
            second = executor.execute(read_file_call("src/a.c"))
            after_second = counter["file_hash"]
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual(second.status, ToolStatus.OK)
        self.assertEqual(after_first, 1)
        self.assertEqual(after_second, 2)
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps({"source_repo": "x", "context_cache_enabled": False}), encoding="utf-8")
            self.assertFalse(load_config(config_path).context_cache_enabled)
        self.assertTrue(Config().context_cache_enabled)
        self.assertTrue(load_config(None).context_cache_enabled)

    def test_worker_caches_are_isolated(self) -> None:
        cache_a, cache_b = ContextCache(), ContextCache()
        executor_a, _ = self.executor(cache=cache_a)
        executor_b, _ = self.executor(cache=cache_b)
        with counted_evidence_reads() as counter:
            from_a = executor_a.execute(read_file_call("src/a.c"))
            after_a = counter["file_hash"]
            from_b = executor_b.execute(read_file_call("src/a.c"))
            after_b = counter["file_hash"]
            again_a = executor_a.execute(read_file_call("src/a.c"))
            after_again = counter["file_hash"]
        self.assertEqual(after_a, 1)
        self.assertEqual(after_b, 2)
        self.assertEqual(after_again, 2)
        self.assertEqual((from_b.status, from_b.content), (from_a.status, from_a.content))
        self.assertEqual(again_a.status, ToolStatus.OK)
        self.assertEqual(len(cache_a.files), 1)
        self.assertEqual(len(cache_b.files), 1)

    def test_oversized_file_is_never_cached(self) -> None:
        (self.repo / "src" / "big.c").write_bytes(b"x" * 40)
        executor, _ = self.executor(cache=ContextCache(), limits=ToolLimits(max_file_bytes=32))
        cache = executor.cache
        with counted_evidence_reads() as counter:
            first = executor.execute(read_file_call("src/big.c"))
            second = executor.execute(read_file_call("src/big.c"))
            after_big = counter["file_hash"]
            small = executor.execute(read_file_call("src/b.c"))
        self.assertEqual(first.status, ToolStatus.TRUNCATED)
        self.assertTrue(first.error.startswith("file exceeds limit: 40 bytes"))
        self.assertEqual((second.status, second.error), (first.status, first.error))
        self.assertEqual(after_big, 0)
        self.assertEqual(len(cache.files), 1)
        self.assertIsNone(cache.files.get("src/big.c", "x" * 64))
        self.assertEqual(small.status, ToolStatus.OK)
        self.assertEqual(counter["file_hash"], 1)

    def test_cache_regions_evict_by_lru_capacity(self) -> None:
        files = FileContextCache(capacity=2)
        files.put("a", text="1", content_hash="ha", observed_hash="ha")
        files.put("b", text="2", content_hash="hb", observed_hash="hb")
        files.put("c", text="3", content_hash="hc", observed_hash="hc")
        self.assertEqual(len(files), 2)
        self.assertIsNone(files.get("a", "ha"))
        self.assertIsNotNone(files.get("c", "hc"))
        self.assertIsNone(files.get("c", "stale"))
        searches = SearchResultCache(capacity=1)
        searches.put("q1", (".",), 5, SearchContextEntry(ToolStatus.OK, (), (), {}, None))
        searches.put("q2", (".",), 5, SearchContextEntry(ToolStatus.EMPTY, (), (), {}, "no textual matches; this does not prove semantic absence"))
        self.assertEqual(len(searches), 1)
        self.assertIsNone(searches.get("q1", (".",), 5, {}))
        self.assertIsNotNone(searches.get("q2", (".",), 5, {}))


class ContextCacheAgentLoopTests(unittest.TestCase):
    """缓存开启的全链路防回归(Phase 1 终审遗留)。

    ToolExecutor 注入 ContextCache 等价于生产 context_cache_enabled=True 的
    注入路径;ScriptedModel 决策序列驱动 AgentLoop 跑通 read/search/
    list_symbols + edit_file,断言:缓存回放 Observation 与直接读同构
    (status/content/hashes)、edit 后缓存失效读到新内容、全程无需人工复核。
    """

    A_C = b"int first = OLD;\nint second = OLD;\nvoid run_step(void) {\n    step(OLD);\n}\n"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo = Path(temporary.name) / "repo"
        repo.mkdir()
        git(repo, "init", "--quiet")
        git(repo, "config", "user.name", "HEAL test")
        git(repo, "config", "user.email", "heal-test@localhost")
        git(repo, "config", "core.autocrlf", "false")
        (repo / "src").mkdir()
        (repo / "src" / "a.c").write_bytes(self.A_C)
        git(repo, "add", "--all")
        git(repo, "commit", "-m", "base", "--quiet")
        self.repo = repo
        self.base = git(repo, "rev-parse", "HEAD")

    def test_cache_on_loop_replays_isomorphic_reads_and_invalidates_after_edit(self) -> None:
        path = "src/a.c"
        decisions = [
            {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": path}}},
            {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": path}}},
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
            {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": path}}},
            {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": path}}},
            {"kind": "tool_call", "tool_call": {"name": "edit_file", "arguments": {
                "path": path,
                "expected_hash": hashlib.sha256(self.A_C).hexdigest(),
                "old_text": "int first = OLD;",
                "new_text": "int first = NEW;",
            }}},
            {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": path}}},
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
            {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": path}}},
            {"kind": "batch_ready", "action_map": {"finding-1": "FIX_CANDIDATE"}},
        ]
        cache = ContextCache()
        executor = ToolExecutor(WorkspaceState(self.repo, self.base), cache=cache, search_backend="python")
        task = RepairTask(
            "task", "run", str(self.repo), self.base,
            (Finding("finding-1", "R001", Severity.LOW, path, 1, "replace old value"),),
            budget=Budget(),
        )
        result = AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=executor).run("batch", task.issues)

        # 全程无需人工复核:所有观察 complete 且无阻塞状态,提案正常产出。
        self.assertFalse(result.review_required, msg=str(result.reason))
        self.assertIsNotNone(result.proposal)
        self.assertEqual(result.proposal.changed_files, (path,))

        observations = result.observations
        # 每对 (直接读, 缓存回放) 同构:status/content/hashes/complete 一致。
        for direct, replayed in ((observations[0], observations[1]), (observations[2], observations[3]), (observations[4], observations[5])):
            self.assertEqual(
                (replayed.status, replayed.content, replayed.file_hashes, replayed.complete),
                (direct.status, direct.content, direct.file_hashes, direct.complete),
            )
            self.assertEqual(replayed.status, ToolStatus.OK)

        # edit 成功;edit 后按路径校验自然失效:读到新内容、新 hash。
        self.assertEqual(observations[6].status, ToolStatus.OK)
        self.assertIn("int first = NEW;", observations[7].content["text"])
        self.assertNotIn("int first = OLD;\n", observations[7].content["text"])
        self.assertNotEqual(observations[7].content["content_hash"], observations[0].content["content_hash"])
        self.assertEqual(observations[7].file_hashes, {path: observations[7].content["content_hash"]})
        # 搜索重扫反映编辑后的现实:OLD 只剩 line 2 与 line 4 两处命中。
        self.assertEqual(observations[8].status, ToolStatus.OK)
        self.assertEqual(observations[8].content["total_hits"], 2)
        # 符号重扫仍可用(缓存失效后走物理读取)。
        self.assertEqual(observations[9].status, ToolStatus.OK)

        # 回放确实来自缓存:三个区各有逻辑命中,而不是全靠重扫。
        stats = cache.stats()
        self.assertGreaterEqual(stats["file_hits"], 1)
        self.assertGreaterEqual(stats["search_hits"], 1)
        self.assertGreaterEqual(stats["symbol_hits"], 1)


if __name__ == "__main__":
    unittest.main()
