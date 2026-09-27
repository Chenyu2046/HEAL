"""Phase 3 experience loop: structured retrieval scoring, lexical confidence, ExperienceWriter consolidation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.adapters.ci import CIRequest
from repair_agent.adapters.gerrit import SubmissionResponse
from repair_agent.config import Config
from repair_agent.domain import ActionKind, Stage, SubmissionStatus, ValidationClass, canonical_json
from repair_agent.experience import ExperienceWriter
from repair_agent.memory import Episode, EpisodeStore
from repair_agent.models import ScriptedModel
from repair_agent.orchestrator import RepairOrchestrator
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
    (repo / "src").mkdir()
    (repo / "src" / "sample.c").write_bytes(b"int value = OLD;\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


def finding_payload(repo: Path, base: str, run_id: str = "run-1") -> dict:
    return {
        "task_id": "task-1", "run_id": run_id, "repo": str(repo), "base_commit": base,
        "findings": [{"id": "finding-1", "rule": "R001", "severity": "low", "file": "src/sample.c", "line": 1, "message": "replace old value", "symbol": "sample", "module": "src", "lifecycle_domain": "module"}],
    }


def repair_decisions() -> list[dict]:
    original = b"int value = OLD;\n"
    return [
        {"kind": "tool_call", "tool_call": {"name": "edit_file", "arguments": {"path": "src/sample.c", "expected_hash": hashlib.sha256(original).hexdigest(), "old_text": "OLD", "new_text": "NEW"}}},
        {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
    ]


class FakeGerrit:
    def submit(self, intent, candidate):
        return SubmissionResponse(SubmissionStatus.SUBMITTED, "accepted", "change-1", "1", candidate.candidate_commit)

    def query(self, intent):
        return SubmissionResponse(SubmissionStatus.SUBMITTED, "found", "change-1", "1", intent.candidate_commit)


class FakeCI:
    def __init__(self) -> None:
        self.requests: dict[str, CIRequest] = {}

    def submit(self, candidate, intent, *, idempotency_key: str):
        existing = self.requests.get(idempotency_key)
        if existing is not None:
            return existing
        request = CIRequest(True, "ci-run-1", "accepted", "fake-ci")
        self.requests[idempotency_key] = request
        return request


def deliver_ci(orchestrator: RepairOrchestrator, candidate, **payload):
    run = orchestrator.store.get_run(candidate.run_id)
    return orchestrator.receive_ci(candidate.candidate_id, dispatch_id=run["ci_dispatch_id"], **payload)


class StructuredScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = EpisodeStore.for_experience(Path(temporary.name))

    def episode(self, episode_id: str, **overrides) -> Episode:
        values = {
            "episode_id": episode_id, "repo": "repo", "module": "src", "rule": "R001",
            "source_commit": "c-other", "memory_type": "fix", "keywords": ("null",),
            "summary": "s", "provenance": ("p",), "human_review": "APPROVED", "ci_result": "VALIDATION_PASS",
        }
        values.update(overrides)
        return Episode(**values)

    def store_all(self, *episodes: Episode) -> None:
        for episode in episodes:
            self.store.store_experience(episode)

    def test_weights_order_and_top3(self) -> None:
        self.store_all(
            self.episode("a-full", trigger_rule="R001", trigger_symbol="plugin_", validated=True),
            self.episode("b-rule-module", trigger_rule="R001", trigger_symbol="other", validated=True),
            self.episode("c-rule-symbol", trigger_rule="R001", trigger_symbol="plugin_", keywords=(), validated=False),
            self.episode("d-wildcard", rule="*", trigger_rule="R001", trigger_symbol="plugin_", validated=True),
            self.episode("e-other-rule", rule="R002", validated=True),
        )
        result = self.store.retrieve(repo="repo", module="src", rule="R001", keywords=("null",), source_commit="", symbol="plugin_")
        # a: rule4+module3+symbol3+keyword1+validated1=12; c: rule4+module3+symbol3=10;
        # b: rule4+module3+keyword1+validated1=9; d: module3+symbol3+keyword1+validated1=8(rule 为通配不加分);
        # e 被 rule 硬过滤。Top 3 = a, c, b。
        self.assertEqual([item.episode_id for item in result], ["a-full", "c-rule-symbol", "b-rule-module"])

    def test_strict_source_commit_filter_removed_and_bonus_applies(self) -> None:
        self.store_all(
            self.episode("exact-commit", source_commit="c-current", validated=True),
            self.episode("other-commit", source_commit="c-ancient", validated=True),
        )
        result = self.store.retrieve(repo="repo", module="src", rule="R001", keywords=(), source_commit="c-current")
        # 两条都返回(不再硬过滤);完全相等的 commit 拿 +2 加分排前。
        self.assertEqual([item.episode_id for item in result], ["exact-commit", "other-commit"])
        self.assertEqual(len(result), 2)

    def test_old_schema_episode_is_readable(self) -> None:
        old_json = {
            "episode_id": "legacy", "repo": "repo", "module": "src", "rule": "R001",
            "source_commit": "c1", "memory_type": "fix", "keywords": ["null"], "summary": "s",
            "provenance": ["p"], "human_review": "APPROVED", "ci_result": "VALIDATION_PASS",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        (self.store.experience_root / "legacy.json").write_text(json.dumps(old_json), encoding="utf-8")
        result = self.store.retrieve(repo="repo", module="src", rule="R001", keywords=("null",), source_commit="c1")
        self.assertEqual(len(result), 1)
        episode = result[0]
        self.assertEqual(episode.episode_id, "legacy")
        self.assertFalse(episode.validated)
        self.assertEqual(episode.confidence, "")
        self.assertEqual(episode.trigger_symbol, "")
        self.assertEqual(episode.schema_version, "1")

    def test_episode_round_trip_keeps_sequence_fields_as_tuples(self) -> None:
        # canonical_json 把 tuple 写成 JSON 数组;Episode(**json) 读回必须仍是
        # 注解声明的 tuple,而不是 list。
        episode = self.episode("round-trip", changed_files=("src/a.c", "src/b.c"))
        restored = Episode(**json.loads(canonical_json(episode)))
        self.assertEqual(restored, episode)
        self.assertIsInstance(restored.keywords, tuple)
        self.assertIsInstance(restored.provenance, tuple)
        self.assertIsInstance(restored.changed_files, tuple)
        self.assertEqual(restored.keywords, ("null",))
        self.assertEqual(restored.provenance, ("p",))
        self.assertEqual(restored.changed_files, ("src/a.c", "src/b.c"))

    def test_legacy_flat_json_sequence_fields_read_back_as_tuples(self) -> None:
        # 旧扁平 JSON 的序列字段是 list;读回应 tuple 化,保持类型稳定。
        legacy = {
            "episode_id": "legacy-flat", "repo": "repo", "module": "src", "rule": "R001",
            "source_commit": "c1", "memory_type": "fix", "keywords": ["null"], "summary": "s",
            "provenance": ["p"], "human_review": "APPROVED", "ci_result": "VALIDATION_PASS",
        }
        restored = Episode(**legacy)
        self.assertIsInstance(restored.keywords, tuple)
        self.assertIsInstance(restored.provenance, tuple)
        self.assertEqual(restored.keywords, ("null",))
        self.assertEqual(restored.provenance, ("p",))

    def test_confidence_three_tiers_and_score_bonus(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace_root = Path(temporary.name)
        (workspace_root / "src").mkdir()
        (workspace_root / "src" / "manager.cpp").write_text("class AudioManager {\n    void plugin_reset() {}\n};\nvoid free_fn() {}\n", encoding="utf-8")
        (workspace_root / "src" / "no_symbols.c").write_text("int x = 1;\n", encoding="utf-8")
        workspace = WorkspaceState(workspace_root, "HEAD")
        self.store_all(
            self.episode("high", trigger_symbol="plugin_reset", changed_files=("src/manager.cpp",), validated=True),
            self.episode("stale-file", trigger_symbol="whatever", changed_files=("src/gone.cpp",), validated=True),
            self.episode("stale-symbol", trigger_symbol="not_there", changed_files=("src/manager.cpp",), validated=True),
            self.episode("medium-no-input", validated=True),
            self.episode("medium-no-decls", trigger_symbol="x", changed_files=("src/no_symbols.c",), validated=True),
        )
        result = self.store.retrieve(repo="repo", module="src", rule="R001", keywords=(), source_commit="", workspace=workspace, limit=5)
        confidence = {item.episode_id: item.confidence for item in result}
        self.assertEqual(confidence["high"], "HIGH")
        self.assertEqual(confidence["stale-file"], "STALE")
        self.assertEqual(confidence["stale-symbol"], "STALE")
        self.assertEqual(confidence["medium-no-input"], "MEDIUM")
        self.assertEqual(confidence["medium-no-decls"], "MEDIUM")
        # HIGH 小幅加成 +1:high(4+1) 与 stale-file/stale-symbol(4) 同基分时排最前。
        self.assertEqual(result[0].episode_id, "high")

    def test_protected_file_probe_is_stale_not_error(self) -> None:
        workspace_root = Path(tempfile.mkdtemp())
        (workspace_root / "vendor").mkdir()
        (workspace_root / "vendor" / "v.c").write_text("void plugin_reset() {}\n", encoding="utf-8")
        workspace = WorkspaceState(workspace_root, "HEAD")
        self.store_all(self.episode("guarded", trigger_symbol="plugin_reset", changed_files=("vendor/v.c",), validated=True))
        result = self.store.retrieve(repo="repo", module="src", rule="R001", keywords=(), source_commit="", workspace=workspace)
        self.assertEqual(result[0].confidence, "STALE")


class ExperienceWriterEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo, self.base = make_repo(self.root)

    def orchestrator(self, decisions: list[dict]) -> RepairOrchestrator:
        config = Config(
            source_repo=str(self.repo), workspace_parent=str(self.root / "worktrees"),
            artifact_root=str(self.root / "runs"), skill_root=str(self.root / "skills"),
            memory_root=str(self.root / "memory"), max_workers=1,
        )
        orchestrator = RepairOrchestrator(config, model_factory=lambda task, worker: ScriptedModel(decisions), gerrit=FakeGerrit(), ci=FakeCI())
        self.addCleanup(orchestrator.store.close)
        return orchestrator

    def experience_files(self, orchestrator: RepairOrchestrator) -> list[dict]:
        experience_root = self.root / "memory" / "experience"
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(experience_root.glob("*.json"))]

    def test_verified_run_writes_honest_experience_episode(self) -> None:
        orchestrator = self.orchestrator(repair_decisions())
        result = orchestrator.run(finding_payload(self.repo, self.base))
        self.assertEqual(result.stage, Stage.HUMAN_REVIEW, msg=str(result.review_reasons))
        candidate = result.candidate
        orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
        orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
        final = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="r1", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"build": "PASS", "ut": "PASS", "scan": "PASS"}, final=True)
        self.assertEqual(final.classification, ValidationClass.VALIDATION_PASS)
        self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)

        episodes = self.experience_files(orchestrator)
        self.assertEqual(len(episodes), 1)
        episode = episodes[0]
        run_payload = orchestrator.store.get_run(candidate.run_id)
        fingerprint = run_payload["task"]["issues"][0]["finding_fingerprint"]
        self.assertEqual(episode["trigger_rule"], "R001")
        self.assertEqual(episode["trigger_symbol"], "sample")
        self.assertEqual(episode["trigger_module"], "src")
        self.assertEqual(episode["warning_signature"], fingerprint)
        self.assertEqual(episode["changed_files"], ["src/sample.c"])
        self.assertEqual(episode["fix_summary"], "FIX_CANDIDATE")
        self.assertEqual(episode["human_review"], "APPROVED")
        self.assertEqual(episode["ci_result"], "VALIDATION_PASS")
        self.assertTrue(episode["validated"])
        self.assertEqual((episode["build_result"], episode["ut_result"], episode["scan_result"]), ("PASS", "PASS", "PASS"))
        self.assertEqual(episode["root_cause"], "")
        self.assertIn("patch scope: 1 file(s)", episode["evidence_summary"])
        self.assertIn("checks: build=PASS", episode["evidence_summary"])
        self.assertEqual(episode["schema_version"], "2")
        self.assertIn(f"run:{candidate.run_id}", episode["provenance"])
        self.assertIn(f"candidate:{candidate.candidate_id}", episode["provenance"])
        self.assertIn("finding:finding-1", episode["provenance"])
        # episode id 由 (candidate, finding) 决定,可重复写不产生重复条目。
        self.assertTrue(episode["episode_id"].startswith("exp-"))
        self.assertEqual(len({item["episode_id"] for item in episodes}), 1)

    def test_writer_failure_does_not_break_verification(self) -> None:
        orchestrator = self.orchestrator(repair_decisions())
        result = orchestrator.run(finding_payload(self.repo, self.base))
        candidate = result.candidate
        orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
        orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
        recorded: list[dict] = []
        real_record_trace = orchestrator.store.record_trace

        def spy(run_id, payload):
            recorded.append(payload)
            return real_record_trace(run_id, payload)

        with patch.object(orchestrator.store, "record_trace", spy), patch.object(ExperienceWriter, "record_verified_run", side_effect=RuntimeError("boom")):
            final = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="r1", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"build": "PASS", "ut": "PASS", "scan": "PASS"}, final=True)
        self.assertEqual(final.classification, ValidationClass.VALIDATION_PASS)
        self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)
        self.assertEqual(self.experience_files(orchestrator), [])
        self.assertTrue(any(item.get("kind") == "experience_write_failed" for item in recorded))

    def test_resume_recovery_also_consolidates_experience(self) -> None:
        orchestrator = self.orchestrator(repair_decisions())
        result = orchestrator.run(finding_payload(self.repo, self.base))
        candidate = result.candidate
        orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
        orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
        # 只累积 CI accumulator,不走 receive_ci,让 VERIFIED 发生在 resume 恢复路径。
        orchestrator.store.merge_ci_checks(
            candidate_id=candidate.candidate_id, ci_run_id="ci-run-1", revision="r-final",
            actual_tested_commit=candidate.candidate_commit,
            checks={"build": "PASS", "ut": "PASS", "scan": "PASS"},
            required_checks=("build", "ut", "scan"), final=True, config_id="cfg", backend="fake-ci",
        )
        recovered = orchestrator.resume(candidate.run_id)
        self.assertEqual(recovered["run"]["stage"], Stage.VERIFIED.value)
        episodes = self.experience_files(orchestrator)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["trigger_symbol"], "sample")
        self.assertTrue(episodes[0]["validated"])


class MemoryRetrieveToolTests(unittest.TestCase):
    def test_symbol_argument_and_confidence_in_output(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "src").mkdir()
        (root / "src" / "manager.cpp").write_text("void sample() {}\n", encoding="utf-8")
        workspace = WorkspaceState(root, "HEAD")
        store = EpisodeStore.for_experience(root / "memory")
        store.store_experience(Episode(
            "exp-1", "repo", "src", "R001", "c1", "fix", ("sample",), "s", ("p",), "APPROVED", "VALIDATION_PASS",
            trigger_rule="R001", trigger_symbol="sample", changed_files=("src/manager.cpp",), validated=True, schema_version="2",
        ))
        executor = ToolExecutor(workspace, episode_store=store)
        description = executor.registry.get("memory_retrieve").description
        self.assertIn("confidence", description)
        observation = executor.execute(type("Call", (), {"name": "memory_retrieve", "arguments": {"repo": "repo", "rule": "R001", "symbol": "sample"}, "call_id": "m1"})())
        self.assertEqual(observation.status.value, "OK")
        self.assertEqual(len(observation.content), 1)
        self.assertEqual(observation.content[0]["confidence"], "HIGH")
        self.assertEqual(observation.content[0]["trigger_symbol"], "sample")

    def test_writer_direct_call_requires_matched_identity(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo, base = make_repo(root)
        config = Config(
            source_repo=str(repo), workspace_parent=str(root / "worktrees"),
            artifact_root=str(root / "runs"), skill_root=str(root / "skills"),
            memory_root=str(root / "memory"), max_workers=1,
        )
        orchestrator = RepairOrchestrator(config, model_factory=lambda task, worker: ScriptedModel(repair_decisions()), gerrit=FakeGerrit(), ci=FakeCI())
        self.addCleanup(orchestrator.store.close)
        run_result = orchestrator.run(finding_payload(repo, base))
        candidate = run_result.candidate
        writer = ExperienceWriter(EpisodeStore.for_experience(root / "memory"))
        # 未 APPROVED / 无 VALIDATION_PASS:返回空,不是错误。
        self.assertEqual(writer.record_verified_run(orchestrator.store, run_id=candidate.run_id, candidate_id=candidate.candidate_id), ())
        with self.assertRaises(ValueError):
            writer.record_verified_run(orchestrator.store, run_id=candidate.run_id, candidate_id="cand-missing")


if __name__ == "__main__":
    unittest.main()
