"""G2/R2 acceptance tests: EvidenceLedger, batch_ready fields, checkpoint + re-plan injection.

Maps 1:1 to docs/tech-design.md §2.9 (R2 acceptances 1-5).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.agent import AgentLoop
from repair_agent.config import CheckSpec, Config
from repair_agent.domain import (
    ActionKind, Budget, Finding, RepairTask, RunRecord, Severity, Stage, canonical_json,
)
from repair_agent.memory import EvidenceLedger, build_ledger
from repair_agent.models import ModelDecision, ModelProtocolError, ScriptedModel
from repair_agent.orchestrator import RepairOrchestrator
from repair_agent.runtime.store import RunStore
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.executor import ToolExecutor

PASS_EXIT0 = (sys.executable, "-c", "import sys; sys.exit(0)")


@contextmanager
def temp_dir(testcase: unittest.TestCase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    yield Path(temporary.name)


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
    (repo / "src" / "sample.c").write_bytes(b"int value = OLD;\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


def issue() -> Finding:
    return Finding("finding-1", "R001", Severity.LOW, "src/sample.c", 1, "replace old value", symbol="sample", module="src", lifecycle_domain="module")


def task_for(repo: Path, base: str, run_id: str = "run-1") -> RepairTask:
    return RepairTask(task_id="task-1", run_id=run_id, repo=str(repo), base_commit=base, issues=(issue(),), budget=Budget())


def failed_edit_call() -> dict:
    # 固定 call_id:跨 worker 的字节级确定性比较不允许 uuid 默认值进入载荷。
    return {"kind": "tool_call", "tool_call": {"name": "edit_file", "call_id": "edit-1", "arguments": {"path": "src/sample.c", "expected_hash": "wrong-hash", "old_text": "OLD", "new_text": "NEW"}}}


def finding_payload(repo: Path, base: str, run_id: str) -> dict:
    return {
        "task_id": "task-1", "run_id": run_id, "repo": str(repo), "base_commit": base,
        "findings": [{"id": "finding-1", "rule": "R001", "severity": "low", "file": "src/sample.c", "line": 1, "message": "replace old value", "symbol": "sample", "module": "src", "lifecycle_domain": "module"}],
    }


def orchestrator_for(root: Path, repo: Path, base: str, store: RunStore, decisions: list[dict]) -> tuple[RepairOrchestrator, _StateCapturingModel]:
    model = _StateCapturingModel(decisions)
    config = Config(source_repo=str(repo), workspace_parent=str(root / "worktrees"), artifact_root=str(root / "runs"), skill_root=str(root / "skills"), memory_root=str(root / "memory"))
    orchestrator = RepairOrchestrator(config, store=store, model_factory=lambda task, worker: model)
    return orchestrator, model


class _StateCapturingModel(ScriptedModel):
    """Scripted decisions plus a deep copy of every state payload the model sees."""

    def __init__(self, decisions: list[dict]) -> None:
        super().__init__(decisions)
        self.states: list[dict] = []

    def decide(self, task, state, observations, tools):
        self.states.append(json.loads(json.dumps(dict(state), default=str)))
        return super().decide(task, state, observations, tools)


class EvidenceLedgerDerivationTests(unittest.TestCase):
    """R2-1: deterministic derivation from observations only; worker-id frames excluded."""

    def test_ledger_payload_is_byte_identical_across_worker_ids(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            decisions = [
                failed_edit_call(),
                {"kind": "tool_call", "tool_call": {"name": "run_checks", "arguments": {"names": ["pass_check"]}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}, "hypothesis": "hash mismatch", "next_questions": ["q1"], "attempt_summary": "edit rejected"},
            ]
            payloads = []
            for worker_id in ("worker-a", "worker-b"):
                executor = ToolExecutor(WorkspaceState(repo, base), check_specs=(CheckSpec("pass_check", PASS_EXIT0),))
                loop = AgentLoop(task_for(repo, base), worker_id=worker_id, model=ScriptedModel([dict(item) for item in decisions]), executor=executor)
                result = loop.run("batch-1", task_for(repo, base).issues)
                self.assertTrue(result.review_required, msg=str(result.reason))
                self.assertIsNotNone(result.ledger)
                payloads.append(canonical_json(result.ledger))
            self.assertEqual(payloads[0], payloads[1])
            ledger = json.loads(payloads[0])
            self.assertEqual(ledger["checks"]["pass_check"]["last_verdict"], "PASS")
            self.assertEqual(ledger["checks"]["pass_check"]["runs"], 1)
            self.assertEqual(len(ledger["failed_attempts"]), 1)
            self.assertEqual(ledger["failed_attempts"][0]["path"], "src/sample.c")
            self.assertEqual(ledger["failed_attempts"][0]["status"], "VERSION_CHANGED")
            self.assertEqual(ledger["file_evidence"]["src/sample.c"]["last_tool"], "edit_file")
            self.assertEqual(ledger["hypothesis"], "hash mismatch")
            self.assertNotIn("task_id", ledger)
            self.assertNotIn("worker_id", ledger)

    def test_ledger_is_not_written_from_model_claims(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            # a review_required decision never touches set_model_fields (batch_ready only)
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=ScriptedModel([{"kind": "review_required", "hypothesis": "claimed-but-ignored"}]), executor=executor)
            result = loop.run("batch-1", task_for(repo, base).issues)
            self.assertIsNotNone(result.ledger)
            self.assertEqual(result.ledger["hypothesis"], "")

    def test_file_evidence_caps_at_fifty_keeping_first_seen_order(self) -> None:
        ledger = EvidenceLedger(task_id="t", worker_id="w")
        from repair_agent.domain import Observation, ToolStatus

        for index in range(60):
            ledger.record(Observation(tool_call_id=f"c{index}", tool="read_file", status=ToolStatus.OK, content={"path": f"f{index}", "text": "x"}, source_paths=(f"f{index}",)))
        payload = ledger.to_payload()
        self.assertEqual(len(payload["file_evidence"]), 50)
        self.assertEqual(list(payload["file_evidence"]), [f"f{index}" for index in range(10, 60)])

    def test_build_ledger_replays_observation_sequence(self) -> None:
        from repair_agent.domain import Observation, ToolStatus

        observations = (Observation(tool_call_id="c1", tool="read_file", status=ToolStatus.OK, content={"path": "f1", "text": "x"}, source_paths=("f1",)),)
        direct = EvidenceLedger(task_id="t", worker_id="w")
        for observation in observations:
            direct.record(observation)
        rebuilt = build_ledger("t", "w", observations)
        self.assertEqual(rebuilt.to_payload(), direct.to_payload())


class BatchReadyModelFieldTests(unittest.TestCase):
    """R2-2/R2-3: optional batch_ready fields, protocol violations, redaction and bounds."""

    def test_fields_parse_on_batch_ready_with_defaults_and_protocol_errors(self) -> None:
        decision = ModelDecision.from_mapping({"kind": "batch_ready", "action_map": {}})
        self.assertEqual((decision.hypothesis, decision.next_questions, decision.attempt_summary), ("", (), ""))
        decision = ModelDecision.from_mapping({"kind": "batch_ready", "action_map": {}, "hypothesis": "h", "next_questions": ["q1", "q2"], "attempt_summary": "s"})
        self.assertEqual((decision.hypothesis, decision.next_questions, decision.attempt_summary), ("h", ("q1", "q2"), "s"))
        decision = ModelDecision.from_mapping({"kind": "batch_ready", "action_map": {}, "hypothesis": None})
        self.assertEqual(decision.hypothesis, "")
        with self.assertRaises(ModelProtocolError):
            ModelDecision.from_mapping({"kind": "batch_ready", "action_map": {}, "hypothesis": 123})
        with self.assertRaises(ModelProtocolError):
            ModelDecision.from_mapping({"kind": "batch_ready", "action_map": {}, "next_questions": ["ok", 5]})
        with self.assertRaises(ModelProtocolError):
            ModelDecision.from_mapping({"kind": "batch_ready", "action_map": {}, "attempt_summary": []})
        # the fields ride batch_ready only
        decision = ModelDecision.from_mapping({"kind": "review_required"})
        self.assertEqual((decision.hypothesis, decision.next_questions, decision.attempt_summary), ("", (), ""))

    def test_set_model_fields_redacts_and_bounds(self) -> None:
        ledger = EvidenceLedger(task_id="t", worker_id="w")
        ledger.set_model_fields(
            "token=abcsecret " * 200,
            ["password=topsecret " * 100, "q2", "q3", "q4", "q5", "q6"],
            "token=zzzsecret " * 200,
        )
        payload = ledger.to_payload()
        serialized = canonical_json(payload)
        self.assertNotIn("abcsecret", serialized)
        self.assertNotIn("topsecret", serialized)
        self.assertNotIn("zzzsecret", serialized)
        self.assertIn("<redacted>", payload["hypothesis"])
        self.assertLessEqual(len(payload["hypothesis"]), 1_000 + len("…[truncated]"))
        self.assertLessEqual(len(payload["attempt_summary"]), 1_000 + len("…[truncated]"))
        self.assertEqual(len(payload["next_questions"]), 5)
        self.assertTrue(all(len(item) <= 300 + len("…[truncated]") for item in payload["next_questions"]))

    def test_checkpoint_carries_bounded_redacted_model_fields(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            decisions = [
                failed_edit_call(),
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}, "hypothesis": "token=abcsecret " * 120, "attempt_summary": "token=zzzsecret " * 120},
            ]
            orchestrator, _ = orchestrator_for(root, repo, base, RunStore(root / "runs"), decisions)
            orchestrator.run(finding_payload(repo, base, "run-1"))
            checkpoint = orchestrator.store.latest_worker_ledger("run-1")
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint["kind"], "worker")
            ledger = checkpoint["evidence_ledger"]
            serialized = canonical_json(ledger)
            self.assertNotIn("abcsecret", serialized)
            self.assertNotIn("zzzsecret", serialized)
            self.assertLessEqual(len(ledger["hypothesis"]), 1_000 + len("…[truncated]"))
            orchestrator.store.close()


class PriorLedgerInjectionTests(unittest.TestCase):
    """R2-4a/R2-4b: read-only injection unit and orchestrator wiring."""

    def test_injected_prior_evidence_reaches_state_read_only(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            injected = {
                "from_run_id": "run-0",
                "ledger": {
                    "file_evidence": {"src/sample.c": {"last_hash": "prior-hash", "last_tool": "read_file", "last_revision": 1}},
                    "checks": {},
                    "failed_attempts": [],
                    "hypothesis": "prior hypothesis",
                    "next_questions": [],
                    "attempt_summary": "",
                },
            }
            snapshot = json.loads(json.dumps(injected))
            model = _StateCapturingModel([{"kind": "review_required"}])
            executor = ToolExecutor(WorkspaceState(repo, base))
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=model, executor=executor, prior_attempt_evidence=injected)
            loop.run("batch-1", task_for(repo, base).issues)
            state = model.states[0]
            self.assertEqual(state["prior_attempt_evidence"]["from_run_id"], "run-0")
            self.assertEqual(state["prior_attempt_evidence"]["ledger"]["file_evidence"]["src/sample.c"]["last_hash"], "prior-hash")
            # mutating the new ledger (failed edit recorded) never alters the injected copy
            self.assertEqual(injected, snapshot)

    def test_replan_injects_prior_worker_ledger_and_records_markers(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            store = RunStore(root / "runs")
            first_decisions = [
                failed_edit_call(),
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}, "hypothesis": "prior-hypothesis-marker"},
            ]
            first, _ = orchestrator_for(root, repo, base, store, first_decisions)
            first.run(finding_payload(repo, base, "run-1"))
            checkpoint = first.store.latest_worker_ledger("run-1")
            self.assertIsNotNone(checkpoint)

            second_decisions = [{"kind": "review_required"}]
            second, model = orchestrator_for(root, repo, base, store, second_decisions)
            second.run(finding_payload(repo, base, "run-2"), replan_of_run_id="run-1")
            run_payload = store.get_run("run-2")
            self.assertTrue(run_payload["prior_ledger_loaded"])
            self.assertEqual(run_payload["prior_ledger_source_run_id"], "run-1")
            self.assertEqual(model.states[0]["prior_attempt_evidence"]["from_run_id"], "run-1")
            self.assertEqual(model.states[0]["prior_attempt_evidence"]["ledger"]["hypothesis"], "prior-hypothesis-marker")

            # key absent ⇒ today's behavior: cold start with an explicit marker
            third, model3 = orchestrator_for(root, repo, base, store, [{"kind": "review_required"}])
            third.run(finding_payload(repo, base, "run-3"))
            run3 = store.get_run("run-3")
            self.assertFalse(run3["prior_ledger_loaded"])
            self.assertEqual(run3["prior_ledger_reason"], "not_provided")
            self.assertNotIn("prior_attempt_evidence", model3.states[0])

            # prior run missing ⇒ explicit marker, no injection
            fourth, model4 = orchestrator_for(root, repo, base, store, [{"kind": "review_required"}])
            fourth.run(finding_payload(repo, base, "run-4"), replan_of_run_id="missing-run")
            run4 = store.get_run("run-4")
            self.assertFalse(run4["prior_ledger_loaded"])
            self.assertEqual(run4["prior_ledger_reason"], "prior_run_not_found")
            self.assertNotIn("prior_attempt_evidence", model4.states[0])

            # prior run without worker checkpoints ⇒ cold start, reason recorded
            store.create_run(RunRecord("run-empty", "task-1", Stage.REVIEW_REQUIRED, "1", "scripted", {}), {"task": {}})
            fifth, model5 = orchestrator_for(root, repo, base, store, [{"kind": "review_required"}])
            fifth.run(finding_payload(repo, base, "run-5"), replan_of_run_id="run-empty")
            run5 = store.get_run("run-5")
            self.assertFalse(run5["prior_ledger_loaded"])
            self.assertEqual(run5["prior_ledger_reason"], "no_worker_checkpoint")
            self.assertNotIn("prior_attempt_evidence", model5.states[0])

            # unreadable ledger payload ⇒ failure recorded, never silently swallowed
            store.save_checkpoint("run-empty", "checkpoint-worker-bad", Stage.BATCH_REVIEW, [], {"kind": "worker", "evidence_ledger": "corrupt"})
            sixth, model6 = orchestrator_for(root, repo, base, store, [{"kind": "review_required"}])
            sixth.run(finding_payload(repo, base, "run-6"), replan_of_run_id="run-empty")
            run6 = store.get_run("run-6")
            self.assertFalse(run6["prior_ledger_loaded"])
            self.assertEqual(run6["prior_ledger_reason"], "ledger_unreadable")
            self.assertNotIn("prior_attempt_evidence", model6.states[0])
            store.close()

    def test_resume_reports_replan_of_run_id(self) -> None:
        with temp_dir(self) as root:
            store = RunStore(root / "runs")
            store.create_run(
                RunRecord(run_id="run-1", task_id="task-1", stage=Stage.REVIEW_REQUIRED, config_version="1", model_id="scripted", budget_used={}),
                {"task": {"budget": {}}, "budget_used": {"token_usage_known": True}},
            )
            orchestrator = RepairOrchestrator(Config(artifact_root=str(root / "artifacts")), store=store)
            recovered = orchestrator.resume("run-1")
            self.assertEqual(recovered["replan_of_run_id"], "run-1")
            store.close()


if __name__ == "__main__":
    unittest.main()
