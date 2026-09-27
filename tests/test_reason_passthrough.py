"""G3/R6 acceptance tests: model review-reason passthrough (diagnostic metadata only).

Maps 1:1 to docs/tech-design.md §3.11 (R6 acceptances 1, 2, 4; acceptance 3 is the
untouched existing reason tests in the full suite).
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
from repair_agent.config import Config
from repair_agent.domain import Budget, Finding, RepairTask, Severity, canonical_json
from repair_agent.models import ScriptedModel
from repair_agent.orchestrator import RepairOrchestrator, _worker_result_artifact_payload
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.executor import ToolExecutor


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
    (repo / "src").mkdir()
    (repo / "src" / "sample.c").write_bytes(b"int value = OLD;\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


def issue() -> Finding:
    return Finding("finding-1", "R001", Severity.LOW, "src/sample.c", 1, "replace old value", symbol="sample", module="src", lifecycle_domain="module")


def task_for(repo: Path, base: str) -> RepairTask:
    return RepairTask(task_id="task-1", run_id="run-1", repo=str(repo), base_commit=base, issues=(issue(),), budget=Budget())


def finding_payload(repo: Path, base: str, run_id: str) -> dict:
    return {
        "task_id": "task-1", "run_id": run_id, "repo": str(repo), "base_commit": base,
        "findings": [{"id": "finding-1", "rule": "R001", "severity": "low", "file": "src/sample.c", "line": 1, "message": "replace old value", "symbol": "sample", "module": "src", "lifecycle_domain": "module"}],
    }


REVIEW_SECRET_REASON = "the fix is unsafe because api_key=hunter2secret is embedded in the test fixture"


class ModelReasonUnitTests(unittest.TestCase):
    """R6-1/R6-2: redaction, cap, durable-reason invariance."""

    def test_review_required_redacts_model_reason_and_keeps_durable_reason(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            decisions = [{"kind": "review_required", "reason": REVIEW_SECRET_REASON}]
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task_for(repo, base).issues)
            self.assertTrue(result.review_required)
            self.assertEqual(result.reason, "model requested human review")
            self.assertIsNotNone(result.model_reason)
            self.assertNotIn("hunter2secret", result.model_reason)
            self.assertIn("<redacted>", result.model_reason)
            self.assertIn("unsafe", result.model_reason)

    def test_clean_model_reason_survives_verbatim_within_cap(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            reason = "null dereference confirmed by observation trace, needs human sign-off"
            decisions = [{"kind": "review_required", "reason": reason}]
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task_for(repo, base).issues)
            self.assertEqual(result.model_reason, reason)

    def test_over_long_model_reason_is_truncated_with_marker(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            reason = "x" * 2_000
            decisions = [{"kind": "review_required", "reason": reason}]
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task_for(repo, base).issues)
            self.assertEqual(len(result.model_reason), 1_000 + len("…[truncated]"))
            self.assertTrue(result.model_reason.endswith("…[truncated]"))

    def test_budget_and_protocol_paths_carry_no_model_reason(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            # budget exhaustion is not a model decision, so no model reason was supplied
            task = RepairTask(task_id="task-1", run_id="run-1", repo=str(repo), base_commit=base, issues=(issue(),), budget=Budget(max_model_calls=0))
            loop = AgentLoop(task, worker_id="worker", model=ScriptedModel([]), executor=executor)
            result = loop.run("batch-1", task.issues)
            self.assertTrue(result.review_required)
            self.assertEqual(result.reason, "model call budget exhausted")
            self.assertIsNone(result.model_reason)

    def test_worker_result_artifact_keeps_reason_and_adds_model_reason(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            decisions = [{"kind": "review_required", "reason": REVIEW_SECRET_REASON}]
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task_for(repo, base).issues)
            payload = _worker_result_artifact_payload(result)
            self.assertEqual(payload["reason"], "model requested human review")
            self.assertNotIn("hunter2secret", canonical_json(payload))
            self.assertIn("<redacted>", payload["model_reason"])


class ModelReasonOrchestratorTests(unittest.TestCase):
    """R6-4: run report carries worker_model_reasons; artifacts keep both reasons."""

    def test_run_report_contains_worker_model_reasons(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            decisions = [{"kind": "review_required", "reason": REVIEW_SECRET_REASON}]
            config = Config(source_repo=str(repo), workspace_parent=str(root / "worktrees"), artifact_root=str(root / "runs"), skill_root=str(root / "skills"), memory_root=str(root / "memory"))
            orchestrator = RepairOrchestrator(config, model_factory=lambda task, worker: ScriptedModel([dict(item) for item in decisions]))
            orchestrator.run(finding_payload(repo, base, "run-1"))
            run_payload = orchestrator.store.get_run("run-1")
            reasons = run_payload.get("worker_model_reasons", [])
            self.assertTrue(reasons)
            self.assertNotIn("hunter2secret", canonical_json(reasons))
            self.assertIn("<redacted>", reasons[0])
            report_json = Path(config.artifact_root) / "run-1" / "report" / "report.json"
            report = json.loads(report_json.read_text(encoding="utf-8"))
            self.assertIn("worker_model_reasons", report)
            self.assertNotIn("hunter2secret", json.dumps(report))
            orchestrator.store.close()


if __name__ == "__main__":
    unittest.main()
