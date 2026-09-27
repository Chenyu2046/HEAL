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
        self.assertIn("net::packet_send", packet["symbol_candidates"])


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


class RecallR4aFixtureTests(unittest.TestCase):
    """R4a acceptance 4: the recall fixture gains plugin.h and a frames_ member_variable case."""

    def test_frames_member_variable_case_hits_through_namespace_prefixing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = benchmark.run_recall_benchmark(Path(temporary))
        case = next(item for item in report["per_case"] if item["query"] == "frames_")
        self.assertEqual(case["expected_file"], "src/audio/plugin.h")
        self.assertEqual(case["file_rank"], 1)
        self.assertTrue(case["symbol_hit_at_5"])
        self.assertTrue(any(name.endswith("::frames_") for name in case["symbol_candidates"]))


class SuiteBenchmarkTests(unittest.TestCase):
    """R5a acceptance 1/2/4: three arms, deterministic metrics, pure aggregation."""

    def test_compute_suite_comparison_is_pure_aggregation(self) -> None:
        arms = {
            "baseline": {"context_token_estimate_bytes": 900, "physical_evidence_reads": 8, "repeated_physical_reads": 3, "model_calls": 9, "repair_completed": True, "review_required": False},
            "cache": {"context_token_estimate_bytes": 900, "physical_evidence_reads": 5, "repeated_physical_reads": 0, "model_calls": 9, "repair_completed": True, "review_required": False},
            "ledger": {"context_token_estimate_bytes": 620, "physical_evidence_reads": 5, "repeated_physical_reads": 0, "model_calls": 9, "repair_completed": True, "review_required": False},
        }
        comparison = benchmark.compute_suite_comparison(arms)
        self.assertEqual(comparison["estimate_baseline"], 900)
        self.assertEqual(comparison["estimate_cache"], 900)
        self.assertEqual(comparison["estimate_ledger"], 620)
        self.assertFalse(comparison["estimate_decreases_cache_vs_baseline"])
        # ContextCache's mechanism contribution is physical reads (zero-read replay of
        # identical content), not prompt bytes — see compute_suite_comparison docstring
        self.assertTrue(comparison["physical_reads_decrease_cache_vs_baseline"])
        self.assertTrue(comparison["repeated_reads_decrease_cache_vs_baseline"])
        self.assertTrue(comparison["model_calls_equal"])
        self.assertTrue(comparison["both_repairs_completed"])
        self.assertIn("delta_cache_vs_baseline", comparison)
        self.assertIn("delta_ledger_vs_cache", comparison)
        worse = benchmark.compute_suite_comparison({
            "baseline": arms["baseline"],
            "cache": {**arms["cache"], "physical_evidence_reads": 9},
            "ledger": arms["ledger"],
        })
        self.assertFalse(worse["physical_reads_decrease_cache_vs_baseline"])

    def test_suite_arms_are_deterministic_and_complete(self) -> None:
        first = benchmark.build_suite_report()
        second = benchmark.build_suite_report()
        self.assertEqual(first["benchmark"], "suite")
        self.assertEqual(set(first["arms"]), {"baseline", "cache", "ledger"})
        self.assertIn("context_token_estimate_basis", first["metric_definitions"])
        self.assertIn("notCovered", first["scope"])
        self.assertIn("§6.9", first["arm_definitions"]["scope_note"])
        for label, arm in first["arms"].items():
            self.assertIn("model_calls", arm)
            self.assertIn("tool_calls_by_name", arm)
            # R1 landed: the suite sequence contains one run_checks self-check naming one check
            self.assertEqual(arm["check_runs"], 1, msg=label)
            self.assertIn("physical_evidence_reads", arm)
            self.assertIn("distinct_files_read", arm)
            self.assertIn("repeated_physical_reads", arm)
            self.assertIn("logical_cache_hits", arm)
            self.assertIn("cache_stats", arm)
            self.assertIn("wall_seconds", arm)
            self.assertIn("context_token_estimate_bytes", arm)
            self.assertTrue(arm["repair_completed"], msg=label)
            self.assertFalse(arm["review_required"], msg=label)
        self.assertEqual(first["arms"]["baseline"]["cache_enabled"], False)
        self.assertEqual(first["arms"]["baseline"]["ledger_enabled"], False)
        self.assertEqual(first["arms"]["cache"]["cache_enabled"], True)
        self.assertEqual(first["arms"]["cache"]["ledger_enabled"], False)
        self.assertEqual(first["arms"]["ledger"]["cache_enabled"], True)
        self.assertEqual(first["arms"]["ledger"]["ledger_enabled"], True)
        # determinism (R5a-1, asserted over the aggregation): structure and counts are
        # run-invariant; the byte estimate varies only by observation elapsed_ms widths
        # (time-borne) and the fixed-width temp repo path, documented in docs/BENCHMARK.md
        for label in first["arms"]:
            stable = {"model_calls", "tool_calls_by_name", "check_runs", "physical_evidence_reads",
                      "distinct_files_read", "repeated_physical_reads", "logical_cache_hits",
                      "cache_stats", "repair_completed", "review_required"}
            left = {k: v for k, v in first["arms"][label].items() if k in stable}
            right = {k: v for k, v in second["arms"][label].items() if k in stable}
            self.assertEqual(left, right, msg=label)
            self.assertAlmostEqual(first["arms"][label]["context_token_estimate_bytes"],
                                   second["arms"][label]["context_token_estimate_bytes"],
                                   delta=64, msg=label)
        self.assertEqual(benchmark.compute_suite_comparison(first["arms"]),
                         benchmark.compute_suite_comparison(first["arms"]))
        comparison = first["comparison"]
        # elapsed_ms is normalized out of the estimate, so arm byte counts are deterministic:
        # the cache arm replays identical content (equal estimate) and the ledger arm pays
        # the injection cost in prompt bytes; the cache mechanism signal is physical reads.
        self.assertEqual(comparison["estimate_baseline"], comparison["estimate_cache"], msg=str(comparison))
        self.assertGreater(comparison["estimate_ledger"], comparison["estimate_cache"], msg=str(comparison))
        self.assertTrue(comparison["physical_reads_decrease_cache_vs_baseline"], msg=str(comparison))
        self.assertTrue(comparison["repeated_reads_decrease_cache_vs_baseline"], msg=str(comparison))
        self.assertTrue(comparison["model_calls_equal"])
        self.assertTrue(comparison["both_repairs_completed"])


if __name__ == "__main__":
    unittest.main()
