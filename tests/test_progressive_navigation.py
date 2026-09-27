"""Phase 2 progressive navigation: symbol scanner, list_symbols, rg backend, ranking, and expansion budgets."""

from __future__ import annotations

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
from repair_agent.config import Config, ToolLimits
from repair_agent.context import ContextCache
from repair_agent.domain import Budget, Finding, RepairTask, Severity, Stage, ToolStatus
from repair_agent.models import ScriptedModel
from repair_agent.orchestrator import RepairOrchestrator
from repair_agent.runtime.store import RunRecord, RunStore
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.chunking import ActionChunk, BoundaryDetector, ChunkAction, ChunkExecutor
from repair_agent.tools.executor import ToolExecutor
from repair_agent.tools.source import SearchRankingContext
from repair_agent.tools.symbols import SymbolDecl, scan_symbols


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
    (repo / "src").mkdir()
    (repo / "src" / "a.c").write_text("int first = OLD;\nint second = OLD;\n", encoding="utf-8")
    (repo / "src" / "b.c").write_text("int other = 2;\n", encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


def issue(issue_id: str = "finding-1", **overrides) -> Finding:
    values = {
        "rule_id": "R001", "severity": Severity.LOW, "file": "src/a.c", "line": 1,
        "message": "replace old value", "symbol": "sample", "module": "src", "analysis_trace": (),
    }
    values.update(overrides)
    return Finding(issue_id, values["rule_id"], values["severity"], values["file"], values["line"], values["message"], analysis_trace=values["analysis_trace"], symbol=values["symbol"], module=values["module"])


def call(name: str, arguments: dict) -> object:
    return type("Call", (), {"name": name, "arguments": arguments, "call_id": name})()


class SymbolScannerTests(unittest.TestCase):
    def test_braces_in_comments_and_strings_do_not_break_structure(self) -> None:
        text = (
            "// stray } in line comment\n"
            "/* block } with { brace */\n"
            'static const char *k = "}{";  /* } */\n'
            "struct Audio {\n"
            '    const char *open = "{";\n'
            "    void reset() {}\n"
            "};\n"
        )
        decls = scan_symbols(text)
        self.assertEqual([item.name for item in decls], ["Audio", "Audio::reset"])
        self.assertEqual(decls[0].type, "struct")
        self.assertEqual((decls[0].start_line, decls[0].end_line), (4, 7))
        self.assertEqual((decls[1].start_line, decls[1].end_line), (6, 6))

    def test_out_of_class_member_definition_and_free_functions(self) -> None:
        text = (
            "class Manager {\n"
            "public:\n"
            "    void reset();\n"
            "};\n"
            "void Manager::reset() {\n"
            "    if (x) { step(); }\n"
            "}\n"
            "int free_function(int a) { return a; }\n"
            "namespace audio {\n"
            "int nested_free() { return 1; }\n"
            "}\n"
        )
        decls = {item.name: item for item in scan_symbols(text)}
        self.assertEqual(decls["Manager"].type, "class")
        self.assertEqual((decls["Manager"].start_line, decls["Manager"].end_line), (1, 4))
        self.assertEqual(decls["Manager::reset"].type, "member_function")
        self.assertEqual((decls["Manager::reset"].start_line, decls["Manager::reset"].end_line), (5, 7))
        self.assertEqual(decls["free_function"].type, "function")
        self.assertEqual(decls["nested_free"].type, "function")
        self.assertEqual(decls["audio"].type, "namespace")
        self.assertEqual(decls["Manager::reset"].signature, "void Manager::reset()")

    def test_empty_file_and_comment_only_file_have_no_symbols(self) -> None:
        self.assertEqual(scan_symbols(""), ())
        self.assertEqual(scan_symbols("// only a comment\n/* still {} nothing */\n"), ())

    def test_control_keywords_and_prototypes_are_not_symbols(self) -> None:
        text = (
            "void declared(int a);\n"
            "int use() { if (a) { return 1; } while (b) { work(); } return 0; }\n"
        )
        names = [item.name for item in scan_symbols(text)]
        self.assertEqual(names, ["use"])

    def test_enum_declarations_are_recognized(self) -> None:
        text = (
            "enum color { RED, GREEN };\n"
            "enum class Mode { ON, OFF };\n"
            "enum struct State { IDLE };\n"
            "enum legacy : unsigned char { LOW };\n"
            "int pick(enum color c) { return c; }\n"
        )
        names = [item.name for item in scan_symbols(text)]
        # C 风格 enum 与 enum class/enum struct 都要产出声明;枚举常量不是符号。
        self.assertEqual(names, ["color", "Mode", "State", "legacy", "pick"])
        decls = {item.name: item for item in scan_symbols(text)}
        self.assertEqual(decls["color"].type, "enum")
        self.assertEqual((decls["color"].start_line, decls["color"].end_line), (1, 1))
        self.assertEqual(decls["Mode"].type, "enum")
        self.assertEqual(decls["State"].type, "enum")
        self.assertEqual(decls["legacy"].type, "enum")
        self.assertEqual(decls["pick"].type, "function")

    def test_symbol_decl_is_frozen(self) -> None:
        decl = SymbolDecl("function", "f", "void f()", 1, 1)
        with self.assertRaises(Exception):
            decl.name = "g"  # type: ignore[misc]


class ListSymbolsToolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo, self.base = make_repo(Path(temporary.name))
        (self.repo / "src" / "a.c").write_text(
            "int first = OLD;\nvoid run() {\n    step();\n}\nstatic int helper(void) { return 1; }\n",
            encoding="utf-8",
        )

    def executor(self, *, limits: ToolLimits | None = None, cache: ContextCache | None = None) -> ToolExecutor:
        return ToolExecutor(WorkspaceState(self.repo, self.base), limits=limits, cache=cache, search_backend="python")

    def test_list_symbols_reports_lexical_outline(self) -> None:
        executor = self.executor(cache=ContextCache())
        result = executor.execute(call("list_symbols", {"path": "src/a.c"}))
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.complete, True)
        self.assertEqual([item["name"] for item in result.content["symbols"]], ["run", "helper"])
        self.assertEqual(result.content["symbols"][0]["start_line"], 2)
        self.assertEqual(result.content["symbols"][0]["end_line"], 4)
        self.assertEqual(result.file_hashes, {result.content["path"]: result.content["content_hash"]})

    def test_list_symbols_empty_file_returns_empty_with_heuristic_note(self) -> None:
        (self.repo / "src" / "empty.c").write_text("", encoding="utf-8")
        executor = self.executor(cache=ContextCache())
        result = executor.execute(call("list_symbols", {"path": "src/empty.c"}))
        self.assertEqual(result.status, ToolStatus.EMPTY)
        self.assertEqual(result.complete, True)
        self.assertEqual(result.content["symbols"], [])
        self.assertIn("heuristic lexical structure, not semantic navigation", result.error)

    def test_list_symbols_rejects_protected_and_escaping_paths(self) -> None:
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "v.c").write_text("int x;\n", encoding="utf-8")
        executor = self.executor()
        protected = executor.execute(call("list_symbols", {"path": "vendor/v.c"}))
        self.assertEqual(protected.status, ToolStatus.ERROR)
        escaping = executor.execute(call("list_symbols", {"path": "../outside.c"}))
        self.assertEqual(escaping.status, ToolStatus.ERROR)
        missing = executor.execute(call("list_symbols", {"path": "src/missing.c"}))
        self.assertEqual(missing.status, ToolStatus.ERROR)

    def test_list_symbols_oversized_file_is_truncated(self) -> None:
        (self.repo / "src" / "big.c").write_bytes(b"int v;\n" * 20)
        executor = self.executor(limits=ToolLimits(max_file_bytes=32))
        result = executor.execute(call("list_symbols", {"path": "src/big.c"}))
        self.assertEqual(result.status, ToolStatus.TRUNCATED)
        self.assertEqual(result.complete, False)
        self.assertTrue(result.error.startswith("file exceeds limit: "))

    def test_list_symbols_output_is_char_bounded(self) -> None:
        many = "".join(f"void fn_{index}() {{}}\n" for index in range(40))
        (self.repo / "src" / "many.c").write_text(many, encoding="utf-8")
        executor = self.executor(limits=ToolLimits(max_output_chars=600), cache=ContextCache())
        result = executor.execute(call("list_symbols", {"path": "src/many.c"}))
        self.assertEqual(result.status, ToolStatus.TRUNCATED)
        self.assertEqual(result.complete, False)
        self.assertEqual(result.error, "symbol list output limit reached")
        self.assertGreater(len(result.content["symbols"]), 0)
        self.assertLess(len(result.content["symbols"]), 40)
        self.assertLessEqual(len(json.dumps(result.content)), 600 + 64)

    def test_list_symbols_cache_hit_reads_no_content(self) -> None:
        executor = self.executor(cache=ContextCache())
        counter = {"read_bytes": 0, "file_hash": 0}
        real_file_hash = source_module.file_hash
        real_read_bytes = Path.read_bytes

        def counting_hash(path):
            counter["file_hash"] += 1
            return real_file_hash(path)

        def counting_read_bytes(path, *args, **kwargs):
            counter["read_bytes"] += 1
            return real_read_bytes(path, *args, **kwargs)

        with patch.object(source_module, "file_hash", counting_hash), patch.object(Path, "read_bytes", counting_read_bytes):
            first = executor.execute(call("list_symbols", {"path": "src/a.c"}))
            after_first = dict(counter)
            second = executor.execute(call("list_symbols", {"path": "src/a.c"}))
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual(after_first["file_hash"], 1)
        self.assertEqual(after_first["read_bytes"], 1)
        self.assertEqual((second.status, second.content), (first.status, first.content))
        self.assertEqual(counter["file_hash"], 1)
        self.assertEqual(counter["read_bytes"], 1)

    def test_list_symbols_is_allowed_inside_action_chunk(self) -> None:
        executor = self.executor(cache=ContextCache())
        self.assertIn("list_symbols", BoundaryDetector.READ_ONLY_TOOLS)
        chunk = ActionChunk("nav", (
            ChunkAction("list_symbols", {"path": "src/a.c"}, "symbols"),
            ChunkAction("read_file", {"path": "src/a.c"}, "read"),
        ))
        self.assertIsNone(BoundaryDetector().validate(chunk, executor.registry))
        result = ChunkExecutor().execute(chunk, executor, expected_workspace_revision=0)
        self.assertTrue(result.accepted)
        self.assertEqual([observation.status for observation in result.completed], [ToolStatus.OK, ToolStatus.OK])


class SearchBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo, self.base = make_repo(Path(temporary.name))

    def test_python_backend_fallback_produces_same_aggregate(self) -> None:
        workspace = WorkspaceState(self.repo, self.base)
        forced = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="python")
        status, content, touched, hashes, complete, error = forced.search_code({"query": "OLD"})
        self.assertEqual(status, ToolStatus.OK)
        self.assertTrue(complete)
        self.assertIsNone(error)
        self.assertEqual(content["total_hits"], 2)
        self.assertEqual([item["path"] for item in content["files"]], ["src/a.c"])
        self.assertEqual(content["files"][0]["hits"], 2)
        self.assertEqual([item["line"] for item in content["files"][0]["sample_lines"]], [1, 2])
        self.assertEqual(touched, ("src/a.c",))
        self.assertEqual(len(hashes), 1)

    def test_rg_backend_matches_python_aggregate(self) -> None:
        workspace = WorkspaceState(self.repo, self.base)
        rg_tools = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="rg")
        status, content, touched, hashes, complete, error = rg_tools.search_code({"query": "OLD"})
        self.assertEqual(status, ToolStatus.OK)
        self.assertTrue(complete)
        self.assertEqual(content["total_hits"], 2)
        self.assertEqual(content, {
            "files": [{"path": "src/a.c", "hits": 2, "sample_lines": [
                {"line": 1, "text": "int first = OLD;"},
                {"line": 2, "text": "int second = OLD;"},
            ]}],
            "total_hits": 2,
        })
        self.assertEqual(touched, ("src/a.c",))

    def test_rg_backend_uses_fixed_argv_and_excludes_protected_globs(self) -> None:
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "v.c").write_text("OLD in vendor\n", encoding="utf-8")
        workspace = WorkspaceState(self.repo, self.base)
        rg_tools = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="rg")
        observed: dict[str, object] = {}
        real_run = subprocess.run

        def recording_run(argv, **kwargs):
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            return real_run(argv, **kwargs)

        with patch.object(source_module.subprocess, "run", recording_run):
            status, content, touched, hashes, complete, error = rg_tools.search_code({"query": "OLD"})
        self.assertEqual(status, ToolStatus.OK)
        argv = observed["argv"]
        kwargs = observed["kwargs"]
        self.assertTrue(str(argv[0]).lower().endswith("rg.exe") or str(argv[0]).endswith("rg"))
        self.assertIn("--json", argv)
        self.assertIn("--fixed-strings", argv)
        self.assertIn("--max-filesize", argv)
        self.assertEqual(argv[argv.index("--max-filesize") + 1], "256000")
        self.assertIn("--", argv)
        pattern_position = argv.index("--") + 1
        self.assertEqual(argv[pattern_position], "OLD")
        for protected in workspace.protected_paths:
            self.assertIn(f"!{protected}/**", argv)
        self.assertIn("shell", kwargs)
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("vendor/v.c", touched)
        self.assertEqual([item["path"] for item in content["files"]], ["src/a.c"])

    def test_rg_backend_deadline_stops_before_spawning_ripgrep(self) -> None:
        workspace = WorkspaceState(self.repo, self.base)
        rg_tools = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="rg")
        with patch.object(source_module.subprocess, "run", side_effect=AssertionError("rg must not spawn past the deadline")):
            status, content, touched, hashes, complete, error = rg_tools.search_code({"query": "OLD", "_deadline": 0.0})
        self.assertEqual(status, ToolStatus.PARTIAL)
        self.assertEqual(complete, False)
        self.assertEqual(error, "search stopped at the workspace deadline")

    def test_forced_rg_backend_fails_closed_without_binary(self) -> None:
        workspace = WorkspaceState(self.repo, self.base)
        with patch.object(source_module.shutil, "which", return_value=None):
            with self.assertRaises(ValueError):
                source_module.SourceTools(workspace, max_file_bytes=1, max_output_chars=1, max_search_results=1, search_backend="rg")
            auto = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="auto")
            self.assertIsNone(auto._rg_path)
            status, content, touched, hashes, complete, error = auto.search_code({"query": "OLD"})
        self.assertEqual(status, ToolStatus.OK)
        self.assertEqual(content["total_hits"], 2)

    def test_rg_output_bad_lines_are_skipped(self) -> None:
        workspace = WorkspaceState(self.repo, self.base)
        rg_tools = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="rg")
        good = {"type": "match", "data": {"path": {"text": "src\\a.c"}, "lines": {"text": "int first = OLD;\n"}, "line_number": 1}}
        payload = "\n".join(["{not json", json.dumps({"type": "begin"}), "null", json.dumps(good)]).encode("utf-8")
        hits = rg_tools._parse_rg_output(payload, cap=200)
        self.assertEqual(hits, [("src/a.c", 1, "int first = OLD;")])

    def test_sample_lines_are_capped_at_three(self) -> None:
        (self.repo / "src" / "a.c").write_text("".join(f"int v{index} = OLD;\n" for index in range(5)), encoding="utf-8")
        executor = ToolExecutor(WorkspaceState(self.repo, self.base), search_backend="python")
        result = executor.execute(call("search_code", {"query": "OLD"}))
        self.assertEqual(result.content["total_hits"], 5)
        self.assertEqual(len(result.content["files"][0]["sample_lines"]), 3)
        self.assertEqual([item["line"] for item in result.content["files"][0]["sample_lines"]], [1, 2, 3])

    def test_direct_handler_invalid_max_results_is_bounded_error(self) -> None:
        # ToolExecutor 路径由 schema 校验 integer>=1;直接 handler 调用传非法
        # max_results 必须返回有界 ERROR,而不是抛 ValueError/TypeError。
        workspace = WorkspaceState(self.repo, self.base)
        tools = source_module.SourceTools(workspace, max_file_bytes=256_000, max_output_chars=80_000, max_search_results=200, search_backend="python")
        for bad in ("abc", None, [], {}):
            with self.subTest(bad=bad):
                status, content, touched, hashes, complete, error = tools.search_code({"query": "OLD", "max_results": bad})
                self.assertEqual(status, ToolStatus.ERROR)
                self.assertIsNone(content)
                self.assertEqual(touched, ())
                self.assertFalse(complete)
                self.assertEqual(error, "invalid argument: max_results")
        # 对照:合法 max_results 的直接调用仍按 cap 正常有界返回。
        status, content, touched, hashes, complete, error = tools.search_code({"query": "OLD", "max_results": 1})
        self.assertEqual(status, ToolStatus.PARTIAL)
        self.assertEqual(content["total_hits"], 1)


class SearchRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo, self.base = make_repo(Path(temporary.name))
        (self.repo / "src" / "a.c").write_text("int exact = OLD;\n", encoding="utf-8")
        (self.repo / "src" / "b.c").write_text("int substring = OLDEST;\n", encoding="utf-8")
        (self.repo / "src" / "c.c").write_text("int plain = OLDER; // tracehit marker\n", encoding="utf-8")

    def ranked_search(self, query: str, ranking: SearchRankingContext | None) -> list[str]:
        executor = ToolExecutor(WorkspaceState(self.repo, self.base), ranking_context=ranking, search_backend="python")
        result = executor.execute(call("search_code", {"query": query}))
        self.assertEqual(result.status, ToolStatus.OK, msg=result.error)
        return [item["path"] for item in result.content["files"]]

    def test_exact_identifier_match_ranks_above_substring(self) -> None:
        self.assertEqual(self.ranked_search("OLD", None)[0], "src/a.c")

    def test_same_file_bonus_lifts_substring_hits(self) -> None:
        ranking = SearchRankingContext(files=("src/b.c",))
        order = self.ranked_search("OLD", ranking)
        # b.c 和 c.c 都只是子串命中;SameFile(+3) 把 b.c 抬到 c.c 之上,a.c 仍以精确匹配(+4)居首。
        self.assertEqual(order, ["src/a.c", "src/b.c", "src/c.c"])

    def test_trace_token_bonus_applies(self) -> None:
        ranking = SearchRankingContext(trace_tokens=("tracehit",))
        order = self.ranked_search("OLD", ranking)
        # c.c 命中 trace 词元(+1)超过零加分的 b.c。
        self.assertEqual(order, ["src/a.c", "src/c.c", "src/b.c"])

    def test_symbol_bonus_uses_identifier_boundaries(self) -> None:
        (self.repo / "src" / "d.c").write_text("int sample_next = OLD;\n", encoding="utf-8")
        (self.repo / "src" / "e.c").write_text("int sample = OLD;\n", encoding="utf-8")
        ranking = SearchRankingContext(symbols=("sample",))
        executor = ToolExecutor(WorkspaceState(self.repo, self.base), ranking_context=ranking, search_backend="python")
        result = executor.execute(call("search_code", {"query": "OLD"}))
        paths = [item["path"] for item in result.content["files"]]
        # e.c:符号独立标识符命中(+2)叠加精确匹配(+4),居首;d.c 的 sample_next 是
        # 子串,不加符号分,与 a.c 同为精确匹配 4 分按路径序排其后。
        self.assertEqual(paths, ["src/e.c", "src/a.c", "src/d.c", "src/b.c", "src/c.c"])

    def test_ranking_context_derives_tokens_from_issues(self) -> None:
        batch_issue = issue("finding-1", analysis_trace=("plugin_->process() at manager.cpp:128",))
        ranking = SearchRankingContext.from_issues([batch_issue])
        self.assertEqual(ranking.files, ("src/a.c",))
        self.assertEqual(ranking.modules, ("src",))
        self.assertEqual(ranking.symbols, ("sample",))
        self.assertIn("plugin_", ranking.trace_tokens)
        self.assertIn("process", ranking.trace_tokens)

    def test_empty_ranking_context_adds_no_bonus(self) -> None:
        self.assertEqual(SearchRankingContext().files, ())
        self.assertEqual(self.ranked_search("OLD", None), ["src/a.c", "src/b.c", "src/c.c"])


class ExpansionBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo, self.base = make_repo(Path(temporary.name))

    def loop(self, decisions: list[dict], budget: Budget) -> object:
        task = RepairTask("task", "run", str(self.repo), self.base, (issue(),), budget=budget)
        executor = ToolExecutor(WorkspaceState(self.repo, self.base))
        return AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=executor).run("batch", task.issues)

    def test_search_round_budget_enforced_before_execution(self) -> None:
        decisions = [
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
        ]
        result = self.loop(decisions, Budget(max_search_rounds=1))
        self.assertTrue(result.review_required)
        self.assertIsNone(result.proposal)
        self.assertEqual(result.reason, "search round budget exhausted")
        self.assertEqual(result.usage.search_rounds, 1)

    def test_symbol_expansion_budget_enforced_before_execution(self) -> None:
        decisions = [
            {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": "src/a.c"}}},
            {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": "src/a.c"}}},
        ]
        result = self.loop(decisions, Budget(max_symbol_expansions=1))
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, "symbol expansion budget exhausted")
        self.assertEqual(result.usage.symbol_expansions, 1)

    def test_context_file_budget_enforced_with_observed_file_approximation(self) -> None:
        decisions = [
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
            {"kind": "tool_call", "tool_call": {"name": "search_code", "arguments": {"query": "OLD"}}},
        ]
        result = self.loop(decisions, Budget(max_context_files=1))
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, "context file budget exhausted")
        self.assertEqual(result.usage.search_rounds, 1)

    def test_zero_budget_reports_reason_before_any_model_call(self) -> None:
        result = self.loop([], Budget(max_search_rounds=0))
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, "search round budget exhausted")
        self.assertEqual(result.usage.model_calls, 0)

    def test_usage_counts_context_files_from_deduplicated_evidence(self) -> None:
        decisions = [
            {"kind": "tool_call", "tool_call": {"name": "list_symbols", "arguments": {"path": "src/a.c"}}},
            {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/a.c"}}},
            {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/b.c"}}},
            {"kind": "batch_ready", "action_map": {}},
        ]
        result = self.loop(decisions, Budget())
        self.assertEqual(result.usage.search_rounds, 0)
        self.assertEqual(result.usage.symbol_expansions, 1)
        self.assertEqual(result.usage.context_files, 2)

    def test_chunk_actions_count_toward_expansion_budgets(self) -> None:
        # list_symbols 的目标文件必须带符号,否则 EMPTY 会中断 chunk。
        (self.repo / "src" / "nav.c").write_text("void nav_fn() {}\n", encoding="utf-8")
        executor = ToolExecutor(WorkspaceState(self.repo, self.base))
        search_decisions = [{"kind": "action_chunk", "action_chunk": {"chunk_id": "c", "actions": [
            {"action_id": "a", "tool": "search_code", "arguments": {"query": "OLD"}},
            {"action_id": "b", "tool": "search_code", "arguments": {"query": "OLD"}},
        ]}}]
        result = AgentLoop(RepairTask("task", "run", str(self.repo), self.base, (issue(),), budget=Budget(max_search_rounds=1)),
                           worker_id="worker", model=ScriptedModel(search_decisions), executor=executor, chunking_enabled=True).run("batch", [issue()])
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, "search round budget exhausted")
        self.assertEqual(result.usage.search_rounds, 1)
        symbol_decisions = [{"kind": "action_chunk", "action_chunk": {"chunk_id": "c", "actions": [
            {"action_id": "a", "tool": "list_symbols", "arguments": {"path": "src/nav.c"}},
            {"action_id": "b", "tool": "list_symbols", "arguments": {"path": "src/nav.c"}},
        ]}}]
        result = AgentLoop(RepairTask("task", "run", str(self.repo), self.base, (issue(),), budget=Budget(max_symbol_expansions=1)),
                           worker_id="worker", model=ScriptedModel(symbol_decisions), executor=executor, chunking_enabled=True).run("batch", [issue()])
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, "symbol expansion budget exhausted")
        self.assertEqual(result.usage.symbol_expansions, 1)

    def test_slot_budget_allocation_covers_expansion_budgets(self) -> None:
        total = Budget(max_search_rounds=8, max_symbol_expansions=12, max_context_files=24)
        used = {"search_rounds": 3, "symbol_expansions": 2, "context_files": 4}
        batches = tuple(type("Batch", (), {"batch_id": f"b{index}"})() for index in range(2))
        allocated = RepairOrchestrator._allocate_slot_budgets(total, used, batches)
        self.assertEqual(sum(item.max_search_rounds for item in allocated.values()), 5)
        self.assertEqual(sum(item.max_symbol_expansions for item in allocated.values()), 10)
        self.assertEqual(sum(item.max_context_files for item in allocated.values()), 20)

    def test_worker_budget_store_and_resume_remaining_track_expansion_budgets(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = RunStore(root / "runs")
        self.addCleanup(store.close)
        store.create_run(RunRecord("run", "task", Stage.RECEIVED, "1", "model", {}), {})
        aggregate = store.update_worker_budget("run", "worker-1", {"search_rounds": 3, "symbol_expansions": 2, "context_files": 5})
        self.assertEqual(aggregate["search_rounds"], 3)
        self.assertEqual(aggregate["symbol_expansions"], 2)
        self.assertEqual(aggregate["context_files"], 5)
        orchestrator = RepairOrchestrator(Config(artifact_root=str(root / "runs")), store=store)
        recovered = orchestrator.resume("run")
        remaining = recovered["budget_remaining"]
        self.assertEqual(remaining["max_search_rounds"], Budget().max_search_rounds - 3)
        self.assertEqual(remaining["max_symbol_expansions"], Budget().max_symbol_expansions - 2)
        self.assertEqual(remaining["max_context_files"], Budget().max_context_files - 5)


if __name__ == "__main__":
    unittest.main()
