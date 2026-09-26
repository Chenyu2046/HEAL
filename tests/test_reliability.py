from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.agent import AgentLoop, AgentResult, AgentUsage
from repair_agent.adapters.ci import CIRequest
from repair_agent.adapters.gerrit import SubmissionResponse
from repair_agent.config import Config, ToolLimits
from repair_agent.domain import (
    ActionKind, BatchProposal, Budget, Finding, HumanApproval, RepairTask, RiskClass,
    Observation, RunRecord, Severity, Stage, SubmissionIntent, SubmissionStatus, ToolStatus, ValidationClass, canonical_json,
)
from repair_agent.memory import BatchCache, Episode, EpisodeStore
from repair_agent.models import ModelAdapter, ModelDeadlineExceeded, ModelDecision, ModelError, ModelUsage, ScriptedModel
from repair_agent.orchestrator import OrchestratorError, RepairOrchestrator, _worker_result_artifact_payload
from repair_agent.planning import BatchPlanner, WorkingBatch
from repair_agent.retry import RetryPolicy
from repair_agent.runtime.store import RunStore, StoreError
from repair_agent.runtime.trace import sanitize
from repair_agent.runtime.workspace import GitWorktreeManager, WorkspaceError, WorkspaceState, git_tree_oid, tree_hash
from repair_agent.tools.chunking import ActionChunk, ChunkAction, ChunkExecutor
from repair_agent.tools.executor import ToolExecutor
from repair_agent.validation.candidate import CandidateError, CandidateFreezer
from repair_agent.validation.local import CommandSpec, LocalValidator
from repair_agent.validation.results import IndependentValidator


@contextmanager
def temp_workspace(testcase: unittest.TestCase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    yield temporary.name


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


def issue(issue_id: str = "finding-1") -> Finding:
    return Finding(issue_id, "R001", Severity.LOW, "src/sample.c", 1, "replace old value", symbol="sample", module="src", lifecycle_domain="module")


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
    def __init__(self, lose_response: bool = False) -> None:
        self.lose_response = lose_response
        self.intent = None
        self.submit_calls = 0

    def submit(self, intent, candidate):
        self.submit_calls += 1
        self.intent = intent
        response = SubmissionResponse(SubmissionStatus.SUBMITTED, "accepted", "change-1", "1", candidate.candidate_commit)
        return SubmissionResponse(SubmissionStatus.SUBMISSION_UNKNOWN, "response lost", response_known=False) if self.lose_response else response

    def query(self, intent):
        return SubmissionResponse(SubmissionStatus.SUBMITTED, "found", "change-1", "1", intent.candidate_commit)


class FakeCI:
    def __init__(self, *, lose_first_response: bool = False) -> None:
        self.calls = 0
        self.accepted_calls = 0
        self.lose_first_response = lose_first_response
        self.requests: dict[str, CIRequest] = {}
        self.idempotency_keys: list[str] = []

    def submit(self, candidate, intent, *, idempotency_key: str):
        self.calls += 1
        self.idempotency_keys.append(idempotency_key)
        existing = self.requests.get(idempotency_key)
        if existing is not None:
            return existing
        request = CIRequest(True, "ci-run-1", "accepted", "fake-ci")
        self.requests[idempotency_key] = request
        self.accepted_calls += 1
        if self.lose_first_response:
            self.lose_first_response = False
            raise TimeoutError("simulated response lost after enqueue")
        return request


def deliver_ci(orchestrator: RepairOrchestrator, candidate, **payload):
    run = orchestrator.store.get_run(candidate.run_id)
    return orchestrator.receive_ci(candidate.candidate_id, dispatch_id=run["ci_dispatch_id"], **payload)


class FlakyModel(ModelAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, task, state, observations, tools):
        self.calls += 1
        if self.calls == 1:
            raise ModelError("temporary", category="MODEL_TRANSPORT", retryable=True, usage_unknown=False)
        return ModelDecision("review_required", reason="stop")

    def decide_with_deadline(self, task, state, observations, tools, *, deadline, token_limit):
        # Mirrors ScriptedModel.decide_with_deadline: bounded request wrapper
        # around decide() so the fail-closed base implementation never fires.
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()
        if token_limit <= 0:
            raise ModelError("no model token budget remains", category="TOKEN_BUDGET", usage_unknown=False)
        try:
            decision = self.decide(task, state, observations, tools)
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise ModelDeadlineExceeded() from exc
            raise
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()
        if not decision.usage.reported:
            decision = replace(decision, usage=ModelUsage(reported=True))
        return decision


class ReliabilityTests(unittest.TestCase):
    def test_planner_splits_affinity_after_grouping(self) -> None:
        task = RepairTask("task", "run", ".", "deadbeef", tuple(issue(f"f{i}") for i in range(45)))
        batches = BatchPlanner(max_issues=20, max_files=8).plan(task)
        self.assertEqual([len(batch.issues) for batch in batches], [20, 20, 5])
        self.assertEqual({batch.affinity_key for batch in batches}, {batches[0].affinity_key})

    def test_state_machine_accepts_recovery_paths_and_rejects_illegal_edges(self) -> None:
        with temp_workspace(self) as temporary:
            store = RunStore(Path(temporary) / "runs")
            self.addCleanup(store.close)
            paths = (
                (Stage.RECEIVED, Stage.REPAIRING, Stage.BATCH_REVIEW, Stage.INTEGRATING, Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.SUBMITTING, Stage.CI_DISPATCHING, Stage.CI_PENDING, Stage.VERIFIED),
                (Stage.RECEIVED, Stage.REPAIRING, Stage.BATCH_REVIEW, Stage.INTEGRATING, Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.SUBMITTING, Stage.CI_DISPATCHING, Stage.CI_DISPATCH_UNKNOWN, Stage.CI_DISPATCHING, Stage.CI_PENDING),
                (Stage.RECEIVED, Stage.REPAIRING, Stage.BATCH_REVIEW, Stage.INTEGRATING, Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.SUBMITTING, Stage.SUBMISSION_UNKNOWN, Stage.HUMAN_REVIEW),
                (Stage.RECEIVED, Stage.REPAIRING, Stage.BATCH_REVIEW, Stage.INTEGRATING, Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.SUBMITTING, Stage.SUBMISSION_UNKNOWN, Stage.CI_DISPATCHING, Stage.CI_PENDING),
                (Stage.RECEIVED, Stage.REPAIRING, Stage.BATCH_REVIEW, Stage.INTEGRATING, Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.SUBMITTING, Stage.CI_DISPATCHING, Stage.CI_PENDING, Stage.RETRY_INFRA, Stage.CI_PENDING),
                (Stage.RECEIVED, Stage.REPAIRING, Stage.BATCH_REVIEW, Stage.INTEGRATING, Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.SUBMITTING, Stage.CI_DISPATCHING, Stage.CI_PENDING, Stage.REPAIRING),
            )
            for index, path in enumerate(paths):
                run_id = f"run-{index}"
                store.create_run(RunRecord(run_id, "task", path[0], "1", "model", {}), {})
                for stage in path[1:]:
                    store.transition(run_id, stage)
            store.create_run(RunRecord("illegal", "task", Stage.RECEIVED, "1", "model", {}), {})
            with self.assertRaises(StoreError):
                store.transition("illegal", Stage.VERIFIED)

    def test_retry_attempts_consume_model_request_budget(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            task = RepairTask("task", "run", str(repo), base, (issue(),), budget=Budget(max_model_calls=2, max_tool_calls=4))
            model = FlakyModel()
            loop = AgentLoop(task, worker_id="worker", model=model, executor=ToolExecutor(WorkspaceState(repo, base)), retry_policy=RetryPolicy(1, sleeper=lambda _: None, random_value=lambda: 0.5))
            result = loop.run("batch", task.issues)
            self.assertTrue(result.review_required)
            self.assertEqual((result.usage.model_calls, result.usage.model_attempts, result.usage.model_retries), (1, 2, 1))

    def test_model_retry_is_not_sent_after_attempt_budget_is_exhausted(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            task = RepairTask("task", "run", str(repo), base, (issue(),), budget=Budget(max_model_calls=1, max_tool_calls=4))
            model = FlakyModel()
            loop = AgentLoop(task, worker_id="worker", model=model, executor=ToolExecutor(WorkspaceState(repo, base)), retry_policy=RetryPolicy(1, sleeper=lambda _: None, random_value=lambda: 0.5))
            result = loop.run("batch", task.issues)
            self.assertEqual(result.reason, "model request attempt budget exhausted")
            self.assertEqual((result.usage.model_calls, result.usage.model_attempts, result.usage.model_retries), (1, 1, 0))

    def test_failed_ordinary_tool_blocks_batch_proposal(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            task = RepairTask("task", "run", str(repo), base, (issue(),))
            model = ScriptedModel([
                {"kind": "tool_call", "tool_call": {"name": "read_file", "arguments": {"path": "src/missing.c"}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
            ])
            loop = AgentLoop(task, worker_id="worker", model=model, executor=ToolExecutor(WorkspaceState(repo, base)))
            result = loop.run("batch", task.issues)
            self.assertTrue(result.review_required)
            self.assertIsNone(result.proposal)
            self.assertTrue(result.reason.startswith("tool observation incomplete: read_file/ERROR"))

    def test_protected_search_and_recursive_trace_redaction(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            (repo / ".env").write_text("needle=secret\n", encoding="utf-8")
            (repo / "vendor").mkdir()
            (repo / "vendor" / "private.c").write_text("needle vendor\n", encoding="utf-8")
            workspace = WorkspaceState(repo, base)
            with self.assertRaises(WorkspaceError):
                workspace.resolve(".env")
            executor = ToolExecutor(workspace)
            observation = executor.execute(type("Call", (), {"name": "search_code", "arguments": {"query": "needle"}, "call_id": "x"})())
            self.assertEqual(observation.status, ToolStatus.EMPTY)
            safe = sanitize({"payload": {"api_key": "abc123", "tokens": 7}, "text": '{"password":"pw"}'})
            self.assertEqual(safe["payload"]["api_key"], "<redacted>")
            self.assertEqual(safe["payload"]["tokens"], 7)
            self.assertNotIn("pw", safe["text"])

    def test_run_payload_is_sanitized_before_persistence(self) -> None:
        with temp_workspace(self) as temporary:
            store = RunStore(Path(temporary) / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.RECEIVED, "1", "model", {}), {"finding": {"message": "api_key=private-value"}, "access_token": "private-value-2"})
            persisted = store.get_run("run")
            self.assertNotIn("private-value", repr(persisted))
            self.assertNotIn("private-value-2", repr(persisted))

    def test_budget_snapshots_are_cumulative_and_replace_worker_updates(self) -> None:
        with temp_workspace(self) as temporary:
            store = RunStore(Path(temporary) / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.REPAIRING, "1", "model", {}), {"budget_used": {}, "worker_budget_usage": {}})
            first = {"model_calls": 1, "model_attempts": 2, "model_retries": 1, "tool_calls": 3, "tokens": 10, "edit_attempts": 1, "chunk_actions": 0, "changed_files": 1, "diff_lines": 4, "elapsed_seconds": 5.0}
            second = {"model_calls": 1, "model_attempts": 1, "model_retries": 0, "tool_calls": 2, "tokens": 7, "edit_attempts": 0, "chunk_actions": 0, "changed_files": 0, "diff_lines": 0, "elapsed_seconds": 4.0}
            store.update_worker_budget("run", "worker-1", first)
            aggregate = store.update_worker_budget("run", "worker-2", second)
            first["model_attempts"] = 3
            aggregate = store.update_worker_budget("run", "worker-1", first)
            self.assertEqual(aggregate["model_attempts"], 4)
            self.assertEqual(aggregate["tool_calls"], 5)
            self.assertEqual(aggregate["tokens"], 17)
            self.assertEqual(aggregate["wall_seconds"], 5.0)
            batches = tuple(WorkingBatch(f"b{i}", "task", (issue(f"f{i}"),), ("src", f"s{i}", "l"), frozenset(), frozenset(), frozenset(), RiskClass.LOW) for i in range(2))
            slices = RepairOrchestrator._allocate_slot_budgets(Budget(max_model_calls=10, max_tool_calls=10, max_tokens=30, max_edit_attempts=4), aggregate, batches)
            self.assertEqual(sum(item.max_model_calls for item in slices.values()), 6)
            self.assertEqual(sum(item.max_tool_calls for item in slices.values()), 5)
            self.assertEqual(sum(item.max_tokens for item in slices.values()), 13)
            self.assertEqual(sum(item.max_edit_attempts for item in slices.values()), 3)

    def test_batch_cache_is_content_and_revision_bound(self) -> None:
        cache = BatchCache()
        cache.put("symbol", 2, {"src/a.c": "hash-a"}, "value")
        self.assertEqual(cache.get("symbol", 2, {"src/a.c": "hash-a"}), "value")
        self.assertIsNone(cache.get("symbol", 2, {"src/a.c": "hash-b"}))
        self.assertIsNone(cache.get("symbol", 3, {"src/a.c": "hash-a"}))

    def test_candidate_commit_and_git_tree_are_frozen_together(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            (repo / "src" / "sample.c").write_text("int value = NEW;\n", encoding="utf-8")
            store = RunStore(root / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.INTEGRATING, "1", "model", {}), {})
            proposal = BatchProposal("batch", "worker", base, 1, {"finding-1": ActionKind.FIX_CANDIDATE}, ("src/sample.c",), "worker diff", "hash", RiskClass.LOW)
            candidate = CandidateFreezer(store).freeze(run_id="run", workspace=repo, base_commit=base, proposals=(proposal,), finding_ids=("finding-1",)).candidate
            self.assertEqual(git(repo, "rev-parse", f"{candidate.candidate_commit}^{{tree}}"), candidate.git_tree_oid)
            CandidateFreezer(store).assert_current(candidate, repo)
            self.assertEqual(store.reconcile("run"), [])
            orphan = store.artifact_root / "run" / "orphan.bin"
            orphan.write_bytes(b"not registered in SQLite")
            self.assertTrue(any("unregistered artifact file" in issue for issue in store.reconcile("run")))
            build = repo / "build"
            build.mkdir()
            (build / "object.o").write_bytes(b"ignored build output")
            self.assertEqual(tree_hash(repo), candidate.tree_hash)
            self.assertEqual(git_tree_oid(repo), candidate.git_tree_oid)
            (repo / "src" / "sample.c").write_text("int value = CHANGED;\n", encoding="utf-8")
            with self.assertRaises(CandidateError):
                CandidateFreezer(store).assert_current(candidate, repo)

    def test_validation_pass_requires_actual_candidate_commit(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            (repo / "src" / "sample.c").write_text("int value = NEW;\n", encoding="utf-8")
            store = RunStore(root / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.INTEGRATING, "1", "model", {}), {})
            proposal = BatchProposal("batch", "worker", base, 1, {}, ("src/sample.c",), "", "", RiskClass.LOW)
            candidate = CandidateFreezer(store).freeze(run_id="run", workspace=repo, base_commit=base, proposals=(proposal,), finding_ids=()).candidate
            validator = IndependentValidator()
            checks = {"build": "PASS", "ut": "PASS", "scan": "PASS"}
            wrong = validator.classify(candidate=candidate, revision="metadata", actual_tested_commit=base, ci_run_id="ci", config_id="cfg", backend="fake-ci", checks=checks)
            self.assertEqual(wrong.classification, ValidationClass.INCONCLUSIVE)
            right = validator.classify(candidate=candidate, revision="metadata", actual_tested_commit=candidate.candidate_commit, ci_run_id="ci2", config_id="cfg", backend="fake-ci", checks=checks)
            self.assertEqual(right.classification, ValidationClass.VALIDATION_PASS)

    def test_final_ci_result_can_be_refined_after_missing_commit_and_duplicate_is_safe(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            (repo / "src" / "sample.c").write_bytes(b"int value = NEW;\n")
            store = RunStore(root / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.INTEGRATING, "1", "model", {}), {})
            proposal = BatchProposal("batch", "worker", base, 1, {}, ("src/sample.c",), "", "", RiskClass.LOW)
            candidate = CandidateFreezer(store).freeze(run_id="run", workspace=repo, base_commit=base, proposals=(proposal,), finding_ids=()).candidate
            validator = IndependentValidator()
            checks = {"build": "PASS", "ut": "PASS", "scan": "PASS"}
            missing_identity = validator.classify(candidate=candidate, revision="rev", actual_tested_commit=None, ci_run_id="ci", config_id="cfg", backend="ci", checks=checks)
            self.assertEqual(store.save_validation(missing_identity).classification, ValidationClass.INCONCLUSIVE)
            corrected = validator.classify(candidate=candidate, revision="rev", actual_tested_commit=candidate.candidate_commit, ci_run_id="ci", config_id="cfg", backend="ci", checks=checks)
            saved = store.save_validation(corrected)
            self.assertEqual(saved.classification, ValidationClass.VALIDATION_PASS)
            duplicate = store.save_validation(corrected)
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(duplicate.classification, ValidationClass.VALIDATION_PASS)

    def test_action_chunk_over_budget_executes_nothing(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            task = RepairTask("task", "run", str(repo), base, (issue(),), budget=Budget(max_model_calls=2, max_tool_calls=1))
            model = ScriptedModel([{"kind": "action_chunk", "action_chunk": {"chunk_id": "c", "actions": [
                {"action_id": "a", "tool": "read_file", "arguments": {"path": "src/sample.c"}},
                {"action_id": "b", "tool": "read_file", "arguments": {"path": "src/sample.c"}},
            ]}}])
            result = AgentLoop(task, worker_id="worker", model=model, executor=ToolExecutor(WorkspaceState(repo, base)), chunking_enabled=True).run("batch", task.issues)
            self.assertTrue(result.review_required)
            self.assertIsNone(result.proposal)
            self.assertEqual(result.usage.tool_calls, 0)
            self.assertEqual([item.status for item in result.observations], [ToolStatus.NOT_EXECUTED, ToolStatus.NOT_EXECUTED])

    def test_chunk_error_marks_remaining_not_executed_and_cannot_propose(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            task = RepairTask("task", "run", str(repo), base, (issue(),), budget=Budget(max_model_calls=2, max_tool_calls=10))
            decisions = [{"kind": "action_chunk", "action_chunk": {"chunk_id": "c", "actions": [
                {"action_id": "a", "tool": "read_file", "arguments": {"path": "src/sample.c"}},
                {"action_id": "b", "tool": "read_file", "arguments": {"path": "missing.c"}},
                {"action_id": "c", "tool": "read_file", "arguments": {"path": "src/sample.c"}},
            ]}}]
            result = AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=ToolExecutor(WorkspaceState(repo, base)), chunking_enabled=True).run("batch", task.issues)
            self.assertIsNone(result.proposal)
            self.assertTrue(result.review_required)
            self.assertEqual([item.status for item in result.observations], [ToolStatus.OK, ToolStatus.ERROR, ToolStatus.NOT_EXECUTED])

    def test_chunk_search_deadline_stops_scan_and_marks_remaining_not_executed(self) -> None:
        with temp_workspace(self) as temporary:
            repo, base = make_repo(Path(temporary))
            workspace = WorkspaceState(repo, base)
            executor = ToolExecutor(workspace)
            chunk = ActionChunk("deadline", (
                ChunkAction("search_code", {"query": "OLD"}, "search"),
                ChunkAction("read_file", {"path": "src/sample.c"}, "read"),
            ))
            with patch("repair_agent.tools.executor.time.monotonic", side_effect=(0.0, 0.0, 2.0, 2.0)):
                result = ChunkExecutor().execute(chunk, executor, expected_workspace_revision=0, deadline=1.0)
            self.assertFalse(result.accepted)
            self.assertEqual(result.completed[0].status, ToolStatus.PARTIAL)
            self.assertEqual(result.not_executed[0].status, ToolStatus.NOT_EXECUTED)

    def test_memory_worker_isolation_and_reviewed_experience_sharing(self) -> None:
        with temp_workspace(self) as temporary:
            first = EpisodeStore(temporary, worker_id="worker-1", task_id="task-1")
            second = EpisodeStore(temporary, worker_id="worker-2", task_id="task-1")
            pending = Episode("pending", "repo", "src", "R", "commit", "fix", ("null",), "pending", ("src/a.c:1",), "PENDING", "UNKNOWN")
            first.store_candidate(pending)
            self.assertEqual(second.retrieve(repo="repo", module="src", rule="R", keywords=("null",), source_commit="commit"), ())
            approved = Episode("approved", "repo", "src", "R", "commit", "fix", ("null",), "reviewed", ("src/a.c:1",), "APPROVED", "VALIDATION_PASS")
            first.store_experience(approved)
            self.assertEqual(second.retrieve(repo="repo", module="src", rule="R", keywords=("null",), source_commit="commit")[0].episode_id, "approved")

    def test_local_validator_ignores_build_output_and_checks_identity(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            (repo / "src" / "sample.c").write_text("int value = NEW;\n", encoding="utf-8")
            store = RunStore(root / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.INTEGRATING, "1", "model", {}), {})
            candidate = CandidateFreezer(store).freeze(run_id="run", workspace=repo, base_commit=base, proposals=(BatchProposal("b", "w", base, 1, {}, ("src/sample.c",), "", "", RiskClass.LOW),), finding_ids=()).candidate
            (repo / "build").mkdir()
            (repo / "build" / "temporary.o").write_text("binary-ish", encoding="utf-8")
            command = CommandSpec((sys.executable, "-c", "pass"), 5)
            result = LocalValidator().run(candidate=candidate, workspace=repo, commit=candidate.candidate_commit, config_id="cfg", commands={name: command for name in ("build", "ut", "scan")})
            self.assertEqual(result.classification, ValidationClass.VALIDATION_PASS)
            wrong = LocalValidator().run(candidate=candidate, workspace=repo, commit=base, config_id="cfg", commands={})
            self.assertEqual(wrong.classification, ValidationClass.INCONCLUSIVE)

    def test_worktree_gc_is_registered_dry_run_then_scoped_removal(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            manager = GitWorktreeManager(root / "worktrees")
            handle = manager.create(task_id="run-worker", source_repo=repo, base_commit=base, run_id="run", role="worker")
            preview = manager.gc(eligible_task_ids={"run-worker"})
            self.assertEqual(preview[0]["action"], "would_remove")
            self.assertTrue(handle.path.is_dir())
            removed = manager.gc(eligible_task_ids={"run-worker"}, apply=True)
            self.assertEqual(removed[0]["action"], "removed")
            self.assertFalse(handle.path.exists())

    def test_gc_preserves_old_repairing_worktrees(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            config = Config(source_repo=str(repo), workspace_parent=str(root / "worktrees"), artifact_root=str(root / "runs"), skill_root=str(root / "skills"), memory_root=str(root / "memory"))
            orchestrator = RepairOrchestrator(config)
            self.addCleanup(orchestrator.store.close)
            orchestrator.store.create_run(RunRecord("run", "task", Stage.REPAIRING, "1", "model", {}), {})
            manager = GitWorktreeManager(config.workspace_parent)
            handle = manager.create(task_id="worker", source_repo=repo, base_commit=base, run_id="run", role="worker")
            entries = json.loads(manager.registry_path.read_text(encoding="utf-8"))
            entries["worker"]["updated_at"] = "2000-01-01T00:00:00+00:00"
            manager.registry_path.write_text(json.dumps(entries), encoding="utf-8")
            outcome = orchestrator.gc(older_than_hours=1, apply=True)
            self.assertTrue(handle.path.is_dir())
            self.assertIn("worker", {item["task_id"] for item in outcome["preserved"]})

    def _orchestrator(self, root: Path, repo: Path, decisions: list[dict], gerrit=None, ci=None):
        config = Config(source_repo=str(repo), workspace_parent=str(root / "worktrees"), artifact_root=str(root / "runs"), skill_root=str(root / "skills"), memory_root=str(root / "memory"), max_workers=1)
        orchestrator = RepairOrchestrator(config, model_factory=lambda task, worker: ScriptedModel(decisions), gerrit=gerrit, ci=ci)
        self.addCleanup(orchestrator.store.close)
        return orchestrator

    def test_full_patch_approval_incremental_ci_and_verified_lifecycle(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            gerrit, ci = FakeGerrit(), FakeCI()
            orchestrator = self._orchestrator(root, repo, repair_decisions(), gerrit, ci)
            result = orchestrator.run(finding_payload(repo, base))
            self.assertEqual(result.stage, Stage.HUMAN_REVIEW, msg=str(result.review_reasons))
            candidate = result.candidate
            self.assertTrue(candidate.candidate_commit)
            approval = orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            self.assertEqual(approval.candidate_commit, candidate.candidate_commit)
            with self.assertRaises(OrchestratorError):
                orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=base)
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="reapproved", approved=True)
            request = orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            self.assertTrue(request.accepted)
            partial = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="metadata-only", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"build": "PASS"})
            self.assertEqual(partial.state.value, "PARTIAL")
            deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="metadata-only", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"ut": "PASS"})
            final = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="metadata-only", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"scan": "PASS"})
            self.assertEqual(final.classification, ValidationClass.VALIDATION_PASS)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)
            self.assertFalse(Path(orchestrator.store.get_run(candidate.run_id)["integration_workspace"]).exists())

    def test_ci_dispatch_lost_response_retries_with_same_idempotency_key(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            ci = FakeCI(lose_first_response=True)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(), ci)
            result = orchestrator.run(finding_payload(repo, base))
            candidate = result.candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            with self.assertRaises(OrchestratorError):
                orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            run = orchestrator.store.get_run(candidate.run_id)
            self.assertEqual(run["stage"], Stage.CI_DISPATCH_UNKNOWN.value)
            self.assertEqual(orchestrator.resume(candidate.run_id)["action"], "retry_ci_dispatch_with_same_idempotency_key")
            retried = orchestrator.retry_ci_dispatch(candidate.candidate_id)
            self.assertTrue(retried.accepted)
            self.assertEqual(ci.accepted_calls, 1)
            self.assertEqual(len(set(ci.idempotency_keys)), 1)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.CI_PENDING.value)

    def test_unknown_ci_dispatch_callback_requires_persisted_dispatch_identity(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            ci = FakeCI(lose_first_response=True)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(), ci)
            candidate = orchestrator.run(finding_payload(repo, base)).candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            with self.assertRaises(OrchestratorError):
                orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            run = orchestrator.store.get_run(candidate.run_id)
            dispatch_id = run["ci_dispatch_id"]
            callback = {"ci_run_id": "ci-run-1", "revision": "r1", "actual_tested_commit": candidate.candidate_commit, "config_id": "cfg", "backend": "fake-ci", "checks": {"build": "PASS", "ut": "PASS", "scan": "PASS"}, "final": True}
            with self.assertRaises(OrchestratorError):
                orchestrator.receive_ci(candidate.candidate_id, **callback)
            with self.assertRaises(OrchestratorError):
                orchestrator.receive_ci(candidate.candidate_id, dispatch_id="wrong-dispatch", **callback)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.CI_DISPATCH_UNKNOWN.value)
            verified = orchestrator.receive_ci(candidate.candidate_id, dispatch_id=dispatch_id, **callback)
            self.assertEqual(verified.classification, ValidationClass.VALIDATION_PASS)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)

    def test_submission_unknown_rejects_unbound_ci_callback(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(lose_response=True), FakeCI())
            candidate = orchestrator.run(finding_payload(repo, base)).candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            with self.assertRaises(OrchestratorError):
                orchestrator.receive_ci(candidate.candidate_id, ci_run_id="ci-run-1", revision="r1", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"build": "PASS", "ut": "PASS", "scan": "PASS"}, final=True)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.SUBMISSION_UNKNOWN.value)
            self.assertEqual(orchestrator.store.list_ci_accumulators(candidate.candidate_id), ())

    def test_inconclusive_ci_can_be_refined_to_pass_after_missing_commit_arrives(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(), FakeCI())
            candidate = orchestrator.run(finding_payload(repo, base)).candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            checks = {"build": "PASS", "ut": "PASS", "scan": "PASS"}
            first = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="r1", actual_tested_commit=None, config_id="cfg", backend="fake-ci", checks=checks, final=True)
            self.assertEqual(first.classification, ValidationClass.INCONCLUSIVE)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.REVIEW_REQUIRED.value)
            refined = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="r1", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks=checks, final=True)
            self.assertEqual(refined.classification, ValidationClass.VALIDATION_PASS)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)

    def test_resume_rebuilds_validation_from_final_ci_accumulator(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(), FakeCI())
            candidate = orchestrator.run(finding_payload(repo, base)).candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            orchestrator.store.merge_ci_checks(candidate_id=candidate.candidate_id, ci_run_id="ci-run-1", revision="r-final", actual_tested_commit=candidate.candidate_commit, checks={"build": "PASS", "ut": "PASS", "scan": "PASS"}, required_checks=("build", "ut", "scan"), final=True, config_id="cfg", backend="fake-ci")
            self.assertEqual(orchestrator.store.list_validations(candidate.candidate_id), ())
            recovered = orchestrator.resume(candidate.run_id)
            self.assertEqual(recovered["run"]["stage"], Stage.VERIFIED.value)
            self.assertEqual(orchestrator.store.list_validations(candidate.candidate_id)[0].classification, ValidationClass.VALIDATION_PASS)

    def test_resume_reconciles_downgraded_validation_before_run_state(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(), FakeCI())
            candidate = orchestrator.run(finding_payload(repo, base)).candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            final = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="r1", actual_tested_commit=candidate.candidate_commit, config_id="cfg", backend="fake-ci", checks={"build": "PASS", "ut": "PASS", "scan": "PASS"}, final=True)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)
            orchestrator.store.save_validation(replace(final, classification=ValidationClass.INCONCLUSIVE, evidence={"callback_conflict": True}))
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.VERIFIED.value)
            recovered = orchestrator.resume(candidate.run_id)
            self.assertEqual(recovered["run"]["stage"], Stage.REVIEW_REQUIRED.value)

    def test_candidate_freeze_orphan_artifacts_force_recovery_review(self) -> None:
        with temp_workspace(self) as temporary:
            store = RunStore(Path(temporary) / "runs")
            self.addCleanup(store.close)
            store.create_run(RunRecord("run", "task", Stage.INTEGRATING, "1", "model", {}), {})
            store.save_artifact("run", "cand-orphan-diff", "candidate-diff", "diff")
            store.save_artifact("run", "cand-orphan-report", "candidate-report", "{}")
            issues = store.reconcile("run")
            self.assertTrue(any("no committed candidate record" in issue for issue in issues))
            self.assertEqual(store.get_run("run")["stage"], Stage.REVIEW_REQUIRED.value)

    def test_worker_result_artifact_omits_raw_observation_content_and_error(self) -> None:
        observation = Observation("call", "read_file", ToolStatus.OK, content={"line": "password=source-secret"}, error="token=error-secret")
        result = AgentResult("batch", "worker", None, True, "HTTP body with unmarked secret", AgentUsage(), (observation,))
        serialized = canonical_json(_worker_result_artifact_payload(result))
        self.assertNotIn("source-secret", serialized)
        self.assertNotIn("error-secret", serialized)
        persisted_observation = json.loads(serialized)["observations"][0]
        self.assertTrue(persisted_observation["content_omitted"])
        self.assertTrue(persisted_observation["error_present"])
        self.assertNotIn("content", persisted_observation)
        self.assertNotIn("error", persisted_observation)
        self.assertIsNone(json.loads(serialized)["reason"])

    def test_ci_accumulator_serializes_conflicting_cross_connection_callbacks(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary) / "runs"
            first, second = RunStore(root), RunStore(root)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            barrier = threading.Barrier(2)

            def merge(store: RunStore, value: str) -> None:
                barrier.wait()
                store.merge_ci_checks(candidate_id="candidate", ci_run_id="ci", revision="r1", actual_tested_commit="commit", checks={"build": value}, required_checks=("build",), final=True, config_id="cfg", backend="ci")

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = (pool.submit(merge, first, "PASS"), pool.submit(merge, second, "FAIL"))
                for future in futures:
                    future.result()
            accumulator = first.list_ci_accumulators("candidate")[0]
            self.assertEqual(accumulator["checks"]["build"], "CONFLICT")

    def test_changed_candidate_invalidates_approval_before_submission(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            gerrit, ci = FakeGerrit(), FakeCI()
            orchestrator = self._orchestrator(root, repo, repair_decisions(), gerrit, ci)
            run_result = orchestrator.run(finding_payload(repo, base))
            self.assertIsNotNone(run_result.candidate, msg=str(run_result.review_reasons))
            candidate = run_result.candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            workspace = Path(orchestrator.store.get_run(candidate.run_id)["integration_workspace"])
            (workspace / "src" / "sample.c").write_text("int value = MUTATED;\n", encoding="utf-8")
            with self.assertRaises(OrchestratorError):
                orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            self.assertEqual(gerrit.submit_calls, 0)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.HUMAN_REVIEW.value)

    def test_ci_for_different_actual_commit_never_verifies_candidate(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            orchestrator = self._orchestrator(root, repo, repair_decisions(), FakeGerrit(), FakeCI())
            run_result = orchestrator.run(finding_payload(repo, base))
            candidate = run_result.candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            result = deliver_ci(orchestrator, candidate, ci_run_id="ci-run-1", revision="wrong-revision", actual_tested_commit=base, config_id="cfg", backend="fake-ci", checks={"build": "PASS", "ut": "PASS", "scan": "PASS"}, final=True)
            self.assertEqual(result.classification, ValidationClass.INCONCLUSIVE)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.REVIEW_REQUIRED.value)

    def test_lost_gerrit_response_reconciles_without_duplicate_ci_request(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            gerrit, ci = FakeGerrit(lose_response=True), FakeCI()
            orchestrator = self._orchestrator(root, repo, repair_decisions(), gerrit, ci)
            run_result = orchestrator.run(finding_payload(repo, base))
            self.assertIsNotNone(run_result.candidate, msg=str(run_result.review_reasons))
            candidate = run_result.candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="ok", approved=True)
            response = orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
            self.assertEqual(response.status, SubmissionStatus.SUBMISSION_UNKNOWN)
            intent = orchestrator.store.list_submissions(candidate.candidate_id)[0]
            self.assertEqual(intent.status, SubmissionStatus.SUBMISSION_UNKNOWN)
            orchestrator.reconcile_submission(candidate.candidate_id, submission_id=intent.submission_id)
            self.assertEqual(orchestrator.store.get_run(candidate.run_id)["stage"], Stage.CI_PENDING.value)
            orchestrator.reconcile_submission(candidate.candidate_id, submission_id=intent.submission_id)
            self.assertEqual(ci.calls, 1)

    def test_concurrent_submit_calls_claim_candidate_once(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            gerrit, ci = FakeGerrit(), FakeCI()
            orchestrator = self._orchestrator(root, repo, repair_decisions(), gerrit, ci)
            result = orchestrator.run(finding_payload(repo, base))
            candidate = result.candidate
            orchestrator.approve(candidate.candidate_id, reviewer="reviewer", reason="approved", approved=True)
            barrier = threading.Barrier(2)

            def submit() -> str:
                barrier.wait()
                try:
                    response = orchestrator.submit(candidate.candidate_id, branch="main", change_id="Iabc", candidate_commit=candidate.candidate_commit)
                    return "accepted" if response.accepted else "rejected"
                except OrchestratorError:
                    return "blocked"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: submit(), range(2)))
            self.assertEqual(outcomes.count("accepted"), 1)
            self.assertEqual(gerrit.submit_calls, 1)
            self.assertEqual(len(orchestrator.store.list_submissions(candidate.candidate_id)), 1)

    def test_resume_converts_interrupted_submission_to_reconciliation_gate(self) -> None:
        with temp_workspace(self) as temporary:
            root = Path(temporary)
            repo, base = make_repo(root)
            orchestrator = self._orchestrator(root, repo, repair_decisions())
            result = orchestrator.run(finding_payload(repo, base))
            candidate = result.candidate
            orchestrator.store.transition(candidate.run_id, Stage.SUBMITTING, payload_update={"submission_id": "submission-interrupted"})
            recovered = orchestrator.resume(candidate.run_id)
            self.assertEqual(recovered["run"]["stage"], Stage.SUBMISSION_UNKNOWN.value)
            self.assertEqual(recovered["action"], "reconcile_external_side_effect")
            self.assertLess(recovered["budget_remaining"]["max_model_calls"], 20)


if __name__ == "__main__":
    unittest.main()
