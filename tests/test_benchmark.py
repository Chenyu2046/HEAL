"""Phase 4 benchmark harness core logic: fixture determinism, cache-efficiency counts, recall stats."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import benchmark  # noqa: E402
from repair_agent.context import ContextCache, SearchContextEntry  # noqa: E402
from repair_agent.domain import ToolStatus  # noqa: E402


class ContextCacheStatsTests(unittest.TestCase):
    def test_stats_counters_are_pure_additions(self) -> None:
        cache = ContextCache()
        self.assertEqual(cache.stats(), {"file_hits": 0, "file_misses": 0, "search_hits": 0, "search_misses": 0, "symbol_hits": 0, "symbol_misses": 0})
        cache.files.put("a", text="1", content_hash="ha", observed_hash="ha")
        self.assertIsNotNone(cache.files.get("a", "ha"))
        self.assertIsNone(cache.files.get("a", "stale"))
        cache.searches.put("q", (".",), 5, SearchContextEntry(ToolStatus.OK, (), (), {"a": "h1"}, None))
        self.assertIsNotNone(cache.searches.get("q", (".",), 5, {"a": "h1"}))
        self.assertIsNone(cache.searches.get("q", (".",), 5, {"a": "h2"}))
        self.assertEqual(cache.stats(), {"file_hits": 1, "file_misses": 1, "search_hits": 1, "search_misses": 1, "symbol_hits": 0, "symbol_misses": 0})


class ContextBenchmarkTests(unittest.TestCase):
    def test_fixture_is_deterministic_and_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            repo_a, base_a, issues_a, decisions_a = benchmark.build_context_fixture(Path(first))
            repo_b, base_b, issues_b, decisions_b = benchmark.build_context_fixture(Path(second))
            hashes_a = {path.relative_to(repo_a).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(repo_a.rglob("*")) if path.is_file() and ".git" not in path.parts}
            hashes_b = {path.relative_to(repo_b).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(repo_b.rglob("*")) if path.is_file() and ".git" not in path.parts}
        # 工作树文件逐字节确定(git commit 含时间戳,base_commit 的 SHA 不跨构建相等)。
        self.assertEqual(hashes_a, hashes_b)
        self.assertEqual(len(base_a), 40)
        self.assertEqual(len(base_b), 40)
        self.assertEqual(decisions_a, decisions_b)
        self.assertEqual(issues_a, issues_b)
        # 决策序列里的 edit expected_hash 与 fixture 文件逐字节对应。
        edit = next(item for item in decisions_a if item["kind"] == "tool_call" and item["tool_call"]["name"] == "edit_file")
        self.assertEqual(edit["tool_call"]["arguments"]["expected_hash"], hashes_a["src/audio/manager.cpp"])

    def test_cache_on_reduces_physical_reads(self) -> None:
        with tempfile.TemporaryDirectory() as off_dir, tempfile.TemporaryDirectory() as on_dir:
            off = benchmark.run_context_arm(Path(off_dir), cache_enabled=False)
            on = benchmark.run_context_arm(Path(on_dir), cache_enabled=True)
        # 决策序列相同 → 模型调用数相同;两次都必须产出修复提案。
        self.assertEqual(on["model_calls"], off["model_calls"])
        self.assertTrue(off["repair_completed"], msg=str(off["review_reason"]))
        self.assertTrue(on["repair_completed"], msg=str(on["review_reason"]))
        self.assertFalse(on["review_required"])
        # 方案 §18:cache on 时物理证据读取显著少于 off;重复物理读取被消除到常数级
        # (ON 剩余的重复 = edit hash 校验读 + list_symbols/read_file 两个缓存层的
        # 各一次内容读取;OFF 则每次逻辑调用都物理重读)。
        self.assertLess(on["physical_evidence_reads"], off["physical_evidence_reads"])
        self.assertLess(on["repeated_physical_reads"], off["repeated_physical_reads"])
        self.assertLessEqual(on["repeated_physical_reads"], 4)
        self.assertGreaterEqual(off["repeated_physical_reads"], 10)
        # 命中的逻辑调用单独计数:cache on 有命中,off 为 0。
        self.assertGreater(on["logical_cache_hits"], 0)
        self.assertEqual(off["logical_cache_hits"], 0)
        self.assertIsNone(off["cache_stats"])
        self.assertGreater(on["cache_stats"]["file_hits"], 0)
        self.assertGreater(on["cache_stats"]["search_hits"], 0)

    def test_context_report_shape_and_scope_note(self) -> None:
        report = benchmark.build_context_report()
        self.assertEqual(report["benchmark"], "context")
        self.assertIn("notCovered", report["scope"])
        self.assertIn("800-warning", report["scope"])
        self.assertIn("metric_definitions", report)
        comparison = report["comparison"]
        self.assertTrue(comparison["model_calls_equal"])
        self.assertTrue(comparison["both_repairs_completed"])
        self.assertLess(comparison["physical_reads_on"], comparison["physical_reads_off"])
        self.assertLess(comparison["repeated_reads_on"], comparison["repeated_reads_off"])


class RecallBenchmarkTests(unittest.TestCase):
    def test_compute_recall_stats_is_pure_aggregation(self) -> None:
        stats = benchmark.compute_recall_stats([
            {"file_hit_at_3": True, "file_hit_at_5": True, "symbol_hit_at_5": True},
            {"file_hit_at_3": False, "file_hit_at_5": True, "symbol_hit_at_5": False},
            {"file_hit_at_3": False, "file_hit_at_5": False, "symbol_hit_at_5": True},
        ])
        self.assertEqual(stats["cases"], 3)
        self.assertAlmostEqual(stats["file_recall_at_3"], 1 / 3)
        self.assertAlmostEqual(stats["file_recall_at_5"], 2 / 3)
        self.assertAlmostEqual(stats["symbol_recall_at_5"], 2 / 3)
        self.assertEqual(benchmark.compute_recall_stats([])["cases"], 0)

    def test_recall_benchmark_is_deterministic_and_correct(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_report = benchmark.run_recall_benchmark(Path(first))
            second_report = benchmark.run_recall_benchmark(Path(second))
        self.assertEqual(first_report, second_report)
        self.assertEqual(first_report["benchmark"], "recall")
        self.assertIn("notCovered", first_report["scope"])
        self.assertEqual(first_report["cases"], len(benchmark.RECALL_CASES))
        # 植入符号的确定性 fixture:全部 case 必须命中,否则 harness 本身坏了。
        self.assertEqual(first_report["file_recall_at_3"], 1.0)
        self.assertEqual(first_report["file_recall_at_5"], 1.0)
        self.assertEqual(first_report["symbol_recall_at_5"], 1.0)
        packet = next(item for item in first_report["per_case"] if item["query"] == "packet_send")
        self.assertEqual(packet["expected_file"], "src/net/transport.cpp")
        self.assertEqual(packet["file_rank"], 1)
        self.assertIn("packet_send", packet["symbol_candidates"])


class DedupBenchmarkTests(unittest.TestCase):
    """R3-4: dedup comparison harness — estimate decreases, repair outcomes identical."""

    def test_compute_dedup_comparison_is_pure_aggregation(self) -> None:
        arms = {
            "dedup_off": {"context_token_estimate_bytes": 1000, "repair_completed": True, "review_required": False, "model_calls": 9},
            "dedup_on": {"context_token_estimate_bytes": 800, "repair_completed": True, "review_required": False, "model_calls": 9},
        }
        comparison = benchmark.compute_dedup_comparison(arms)
        self.assertEqual(comparison["estimate_off"], 1000)
        self.assertEqual(comparison["estimate_on"], 800)
        self.assertTrue(comparison["estimate_decreases"])
        self.assertTrue(comparison["repair_outcomes_identical"])
        self.assertTrue(comparison["model_calls_equal"])
        worse = benchmark.compute_dedup_comparison({
            "dedup_off": arms["dedup_off"],
            "dedup_on": {**arms["dedup_on"], "context_token_estimate_bytes": 1200},
        })
        self.assertFalse(worse["estimate_decreases"])

    def test_dedup_arms_decrease_estimate_with_identical_outcomes(self) -> None:
        report = benchmark.build_dedup_report()
        self.assertEqual(report["benchmark"], "dedup")
        self.assertIn("context_token_estimate_basis", report["metric_definitions"])
        self.assertIn("notCovered", report["scope"])
        arms = report["runs"]
        self.assertEqual(set(arms), {"dedup_off", "dedup_on"})
        self.assertFalse(arms["dedup_off"]["dedup_enabled"])
        self.assertTrue(arms["dedup_on"]["dedup_enabled"])
        comparison = report["comparison"]
        self.assertTrue(comparison["estimate_decreases"], msg=str(comparison))
        self.assertTrue(comparison["repair_outcomes_identical"])
        self.assertTrue(comparison["model_calls_equal"])
        self.assertTrue(arms["dedup_off"]["repair_completed"])
        self.assertTrue(arms["dedup_on"]["repair_completed"])


if __name__ == "__main__":
    unittest.main()
