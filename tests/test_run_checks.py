"""G1/R1 acceptance tests: in-loop run_checks tool, budget, integrity, prefix.

Maps 1:1 to docs/tech-design.md §1.11 (PRD/requirements R1 acceptances 1-8).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.agent import AgentLoop
from repair_agent.config import CheckSpec, Config, ConfigError, ToolLimits, load_config
from repair_agent.domain import (
    ActionKind, Budget, Finding, RepairTask, RunRecord, Severity, Stage, ToolStatus, canonical_json,
)
from repair_agent.models import ScriptedModel, ToolCall
from repair_agent.orchestrator import RepairOrchestrator
from repair_agent.planning import InputNormalizer
from repair_agent.runtime.store import RunStore
from repair_agent.runtime.workspace import WorkspaceError, WorkspaceState
from repair_agent.tools.chunking import ActionChunk, ChunkAction, ChunkExecutor
from repair_agent.tools.executor import ToolExecutor


@contextmanager
def temp_dir(testcase: unittest.TestCase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    yield Path(temporary.name)


def git(repo: Path, *args: str) -> str:
    import subprocess

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


def task_for(repo: Path, base: str, **budget_overrides) -> RepairTask:
    return RepairTask(
        task_id="task-1", run_id="run-1", repo=str(repo), base_commit=base,
        issues=(issue(),), budget=Budget(**budget_overrides),
    )


PASS_EXIT0 = (sys.executable, "-c", "import sys; sys.exit(0)")
FAIL_EXIT2 = (sys.executable, "-c", "import sys; print('api_key=supersecretvalue'); sys.exit(2)")


def executor_with(workspace: WorkspaceState, *specs: CheckSpec, prefix: tuple[str, ...] = (), limits: ToolLimits | None = None) -> ToolExecutor:
    return ToolExecutor(workspace, limits=limits or ToolLimits(), check_specs=tuple(specs), check_command_prefix=prefix)


def run_checks_call(names: list[str], call_id: str = "checks-1", **extra) -> ToolCall:
    arguments = {"names": names, **extra}
    return ToolCall("run_checks", arguments, call_id)


def checks_content(observation) -> list[dict]:
    content = observation.content
    assert isinstance(content, dict), observation
    return content["checks"]


class RunChecksToolTests(unittest.TestCase):
    """Acceptance 1, 7, 8 and integrity directions (acceptance 3)."""

    def test_verdict_from_exit_code_with_redacted_bounded_output(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            workspace = WorkspaceState(repo, base)
            executor = executor_with(workspace, CheckSpec("pass_check", PASS_EXIT0), CheckSpec("fail_check", FAIL_EXIT2))
            observation = executor.execute(run_checks_call(["pass_check", "fail_check"]))
            self.assertEqual(observation.status, ToolStatus.OK)
            self.assertTrue(observation.complete)
            self.assertIsNone(observation.error)
            self.assertEqual(observation.source_paths, ())
            self.assertEqual(observation.file_hashes, {})
            entries = checks_content(observation)
            self.assertEqual([entry["name"] for entry in entries], ["pass_check", "fail_check"])
            self.assertEqual(entries[0]["verdict"], "PASS")
            self.assertEqual(entries[0]["returncode"], 0)
            self.assertIsNone(entries[0]["error"])
            self.assertEqual(entries[1]["verdict"], "FAIL")
            self.assertEqual(entries[1]["returncode"], 2)
            # exit code is the sole verdict source; secret-shaped output is redacted, never parsed
            self.assertNotIn("supersecretvalue", canonical_json(observation.content))
            self.assertIn("<redacted>", entries[1]["stdout_tail"])
            integrity = observation.content["integrity"]
            self.assertTrue(integrity["verified"])
            self.assertEqual(integrity["before_tree_hash"], integrity["after_tree_hash"])
            self.assertEqual(integrity["before_git_tree_oid"], integrity["after_git_tree_oid"])

    def test_unconfigured_tool_is_unsupported(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            self.assertIsNotNone(executor.registry.get("run_checks"))
            observation = executor.execute(run_checks_call(["anything"]))
            self.assertEqual(observation.status, ToolStatus.UNSUPPORTED)
            self.assertFalse(observation.complete)
            self.assertEqual(observation.error, "no checks are configured")

    def test_schema_preflight_rejections_execute_nothing(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            marker = (sys.executable, "-c", "open('marker.txt', 'w').write('x')")
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("marker", marker))
            extra_key = executor.execute(run_checks_call(["marker"], bogus=1))
            self.assertEqual(extra_key.status, ToolStatus.ERROR)
            self.assertFalse(extra_key.complete)
            self.assertIn("unsupported arguments", str(extra_key.error))
            unknown = executor.execute(run_checks_call(["nope"]))
            self.assertEqual(unknown.status, ToolStatus.ERROR)
            self.assertFalse(unknown.complete)
            self.assertIn("unknown checks: nope", str(unknown.error))
            empty = executor.execute(run_checks_call([]))
            self.assertEqual(empty.status, ToolStatus.ERROR)
            self.assertFalse(empty.complete)
            # no subprocess was ever spawned for the pre-execution rejections
            self.assertFalse((repo / "marker.txt").exists())

    def test_integrity_mismatch_on_untracked_write_blocks(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            dirty = (sys.executable, "-c", "open('scratch.txt', 'w').write('x')")
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("dirty", dirty))
            observation = executor.execute(run_checks_call(["dirty"]))
            self.assertEqual(observation.status, ToolStatus.ERROR)
            self.assertFalse(observation.complete)
            self.assertIn("check integrity mismatch", str(observation.error))
            # verdicts already obtained remain in content; only the worker is blocked
            self.assertEqual(checks_content(observation)[0]["verdict"], "PASS")
            self.assertEqual(observation.content["integrity"]["verified"], False)

    def test_gitignored_write_keeps_integrity_verified(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            build_only = (sys.executable, "-c", "import pathlib; pathlib.Path('build').mkdir(exist_ok=True); (pathlib.Path('build') / 'out.txt').write_text('x')")
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("build_out", build_only))
            observation = executor.execute(run_checks_call(["build_out"]))
            self.assertEqual(observation.status, ToolStatus.OK)
            self.assertTrue(observation.complete)
            self.assertTrue(observation.content["integrity"]["verified"])

    def test_identity_unavailable_blocks_before_execution(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("pass_check", PASS_EXIT0))
            with patch("repair_agent.tools.checks.tree_hash", side_effect=WorkspaceError("git broken")):
                observation = executor.execute(run_checks_call(["pass_check"]))
            self.assertEqual(observation.status, ToolStatus.ERROR)
            self.assertFalse(observation.complete)
            self.assertIn("identity unavailable", str(observation.error))

    def test_git_failure_after_checks_blocks_with_verdicts_kept(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("pass_check", PASS_EXIT0))
            real_hash = WorkspaceState(repo, base)
            from repair_agent.runtime.workspace import tree_hash as real_tree_hash

            with patch("repair_agent.tools.checks.tree_hash", side_effect=[real_tree_hash(repo), WorkspaceError("boom")]):
                observation = executor.execute(run_checks_call(["pass_check"]))
            self.assertEqual(observation.status, ToolStatus.ERROR)
            self.assertFalse(observation.complete)
            self.assertIn("could not verify source tree after checks", str(observation.error))
            self.assertEqual(checks_content(observation)[0]["verdict"], "PASS")


class RunChecksInfraTests(unittest.TestCase):
    """Acceptance 4: INFRA_FAIL is not-PASS, non-blocking, honestly surfaced."""

    def test_timeout_and_missing_binary_are_infra_fail(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(
                WorkspaceState(repo, base),
                CheckSpec("slow", (sys.executable, "-c", "import time; time.sleep(30)"), timeout_seconds=0.5),
                CheckSpec("missing", ("definitely-not-a-real-heal-binary-xyz",)),
            )
            observation = executor.execute(run_checks_call(["slow", "missing"]))
            self.assertEqual(observation.status, ToolStatus.OK)
            self.assertTrue(observation.complete)
            entries = checks_content(observation)
            self.assertEqual(entries[0]["verdict"], "INFRA_FAIL")
            self.assertIsNone(entries[0]["returncode"])
            self.assertEqual(entries[0]["error"], "timeout")
            self.assertEqual(entries[1]["verdict"], "INFRA_FAIL")
            self.assertTrue(str(entries[1]["error"]).strip())

    def test_infra_fail_does_not_block_batch_ready(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(
                WorkspaceState(repo, base),
                CheckSpec("slow", (sys.executable, "-c", "import time; time.sleep(30)"), timeout_seconds=0.5),
            )
            decisions = [
                {"kind": "tool_call", "tool_call": {"name": "run_checks", "arguments": {"names": ["slow"]}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
            ]
            loop = AgentLoop(task_for(repo, base), worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task_for(repo, base).issues)
            self.assertFalse(result.review_required, msg=str(result.reason))
            self.assertIsNotNone(result.proposal)
            notes = " | ".join(result.proposal.review_notes)
            self.assertIn("slow=INFRA_FAIL(timeout)", notes)


class RunChecksProposalNoteTests(unittest.TestCase):
    """Acceptance 2: FAIL with oversized output stays OK/complete, non-blocking, in review notes."""

    def test_oversized_fail_output_is_bounded_and_non_blocking(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            # newline separators keep each token a separate redaction; raw volume exceeds the cap
            loud = (sys.executable, "-c", "import sys; print('token=abc123secret\\n' * 400); sys.exit(1)")
            limits = ToolLimits(max_output_chars=2_000)
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("loud_fail", loud), limits=limits)
            task = task_for(repo, base)
            decisions = [
                {"kind": "tool_call", "tool_call": {"name": "run_checks", "arguments": {"names": ["loud_fail"]}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
            ]
            loop = AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=executor, tool_limits=limits)
            result = loop.run("batch-1", task.issues)
            self.assertFalse(result.review_required, msg=str(result.reason))
            self.assertIsNotNone(result.proposal)
            check_observations = [item for item in result.observations if item.tool == "run_checks"]
            self.assertEqual(len(check_observations), 1)
            observation = check_observations[0]
            self.assertEqual(observation.status, ToolStatus.OK)
            self.assertTrue(observation.complete)
            self.assertLess(len(canonical_json(observation.content)), limits.max_output_chars)
            entries = checks_content(observation)
            self.assertEqual(entries[0]["verdict"], "FAIL")
            self.assertTrue(entries[0]["truncated_streams"])
            self.assertNotIn("abc123secret", canonical_json(observation.content))
            notes = " | ".join(result.proposal.review_notes)
            self.assertIn("In-loop checks: loud_fail=FAIL(exit 1)", notes)

    def test_checks_without_failures_still_note_verdicts(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("pass_check", PASS_EXIT0))
            task = task_for(repo, base)
            decisions = [
                {"kind": "tool_call", "tool_call": {"name": "run_checks", "arguments": {"names": ["pass_check"]}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
            ]
            loop = AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task.issues)
            self.assertIsNotNone(result.proposal)
            notes = " | ".join(result.proposal.review_notes)
            self.assertIn("In-loop checks: pass_check=PASS(exit 0)", notes)


class RunChecksBudgetTests(unittest.TestCase):
    """Acceptance 5: budget plumbing, exhaustion reason, all-or-nothing gate, split, resume."""

    def test_budget_settable_from_config_json_and_cli_override(self) -> None:
        with temp_dir(self) as root:
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"budget": {"max_check_runs": 3}}), encoding="utf-8")
            self.assertEqual(load_config(config_path).budget.max_check_runs, 3)
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            payload = {
                "task_id": "task-1", "run_id": "run-1", "repo": str(repo), "base_commit": base,
                "findings": [{"id": "finding-1", "rule": "R001", "severity": "low", "file": "src/sample.c", "line": 1, "message": "m"}],
            }
            normalized = InputNormalizer().normalize(
                payload, default_budget=Budget(max_check_runs=2), cli_budget_overrides={"max_check_runs": 9},
            )
            self.assertEqual(normalized.task.budget.max_check_runs, 9)

    def test_exhaustion_yields_check_run_budget_reason(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("pass_check", PASS_EXIT0))
            task = task_for(repo, base, max_check_runs=1)
            decisions = [
                {"kind": "tool_call", "tool_call": {"name": "run_checks", "arguments": {"names": ["pass_check"]}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
            ]
            loop = AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task.issues)
            self.assertTrue(result.review_required)
            self.assertEqual(result.reason, "check run budget exhausted")

    def test_all_or_nothing_preflight_rejects_oversized_call(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            markers = tuple(
                (sys.executable, "-c", f"open('marker{index}.txt', 'w').write('x')") for index in range(1, 4)
            )
            executor = executor_with(WorkspaceState(repo, base), *(CheckSpec(f"check{index}", argv) for index, argv in zip(range(1, 4), markers)))
            task = task_for(repo, base, max_check_runs=2)
            decisions = [
                {"kind": "tool_call", "tool_call": {"name": "run_checks", "arguments": {"names": ["check1", "check2", "check3"]}}},
                {"kind": "batch_ready", "action_map": {"finding-1": ActionKind.FIX_CANDIDATE.value}},
            ]
            loop = AgentLoop(task, worker_id="worker", model=ScriptedModel(decisions), executor=executor)
            result = loop.run("batch-1", task.issues)
            self.assertTrue(result.review_required)
            self.assertEqual(result.reason, "check run budget exhausted")
            self.assertFalse(any((repo / f"marker{index}.txt").exists() for index in range(1, 4)))

    def test_slot_allocation_splits_remaining_check_runs(self) -> None:
        slot = (SimpleNamespace(batch_id="b1"), SimpleNamespace(batch_id="b2"))
        budgets = RepairOrchestrator._allocate_slot_budgets(Budget(max_check_runs=7), {"check_runs": 3}, slot)
        self.assertEqual(budgets["b1"].max_check_runs, 2)
        self.assertEqual(budgets["b2"].max_check_runs, 2)

    def test_resume_reports_remaining_check_runs(self) -> None:
        with temp_dir(self) as root:
            store = RunStore(root / "runs")
            store.create_run(
                RunRecord(run_id="run-1", task_id="task-1", stage=Stage.REVIEW_REQUIRED, config_version="1", model_id="scripted", budget_used={}),
                {"task": {"budget": {"max_check_runs": 10}}, "budget_used": {"check_runs": 4, "token_usage_known": True}},
            )
            orchestrator = RepairOrchestrator(Config(artifact_root=str(root / "artifacts")), store=store)
            recovered = orchestrator.resume("run-1")
            store.close()
            self.assertEqual(recovered["budget_remaining"]["max_check_runs"], 6)


class RunChecksChunkTests(unittest.TestCase):
    """Acceptance 6: run_checks is never chunk-eligible."""

    def test_chunk_containing_run_checks_is_rejected(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = executor_with(WorkspaceState(repo, base), CheckSpec("pass_check", PASS_EXIT0))
            chunk = ActionChunk("chunk-1", (ChunkAction("run_checks", {"names": ["pass_check"]}, "action-1"),))
            result = ChunkExecutor().execute(chunk, executor, expected_workspace_revision=0)
            self.assertFalse(result.accepted)
            self.assertIn("non-read-only tool is not eligible for chunking: run_checks", str(result.reason))


class CheckConfigTests(unittest.TestCase):
    """§1.2 validation rules: fail at startup, never mid-run."""

    def test_load_config_parses_mapping_and_bare_argv_forms(self) -> None:
        with temp_dir(self) as root:
            config_path = root / "config.json"
            config_path.write_text(json.dumps({
                "checks": {
                    "build": {"argv": ["make", "build"], "timeout_seconds": 180},
                    "ut": ["ctest", "--output-on-failure"],
                },
                "check_command_prefix": ["docker", "run", "--rm"],
                "budget": {"max_check_runs": 3},
                "tools": {"check_timeout_seconds": 60},
            }), encoding="utf-8")
            config = load_config(config_path)
            self.assertEqual(tuple(spec.name for spec in config.checks), ("build", "ut"))
            self.assertEqual(config.checks[0].argv, ("make", "build"))
            self.assertEqual(config.checks[0].timeout_seconds, 180.0)
            # bare argv form inherits the configured tool default timeout
            self.assertEqual(config.checks[1].argv, ("ctest", "--output-on-failure"))
            self.assertEqual(config.checks[1].timeout_seconds, 60.0)
            self.assertEqual(config.check_command_prefix, ("docker", "run", "--rm"))
            self.assertEqual(config.budget.max_check_runs, 3)

    def test_invalid_check_specs_raise_config_error_at_load(self) -> None:
        with self.assertRaises(ConfigError):
            CheckSpec("Build", ("cmd",))
        with self.assertRaises(ConfigError):
            CheckSpec("1x", ("cmd",))
        with self.assertRaises(ConfigError):
            CheckSpec("a b", ("cmd",))
        with self.assertRaises(ConfigError):
            CheckSpec("x" * 65, ("cmd",))
        with self.assertRaises(ConfigError):
            CheckSpec("ok", ())
        with self.assertRaises(ConfigError):
            CheckSpec("ok", ("cmd", ""))
        with self.assertRaises(ConfigError):
            CheckSpec("ok", ("cmd",), timeout_seconds=0)
        with self.assertRaises(ConfigError):
            CheckSpec("ok", ("cmd",), timeout_seconds=-1.0)

    def test_load_config_rejects_invalid_check_section(self) -> None:
        with temp_dir(self) as root:
            bad_shapes = [
                {"checks": ["build"]},
                {"checks": {"build": "make build"}},
                {"checks": {"build": {"argv": []}}},
                {"checks": {"build": {"argv": ["make", ""]}}},
                {"checks": {"build": {"argv": ["make"], "timeout_seconds": 0}}},
            ]
            for section in bad_shapes:
                config_path = root / "config.json"
                config_path.write_text(json.dumps(section), encoding="utf-8")
                with self.assertRaises(ConfigError, msg=str(section)):
                    load_config(config_path)

    def test_load_config_rejects_invalid_command_prefix(self) -> None:
        with temp_dir(self) as root:
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"check_command_prefix": ["docker", ""]}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)
            config_path.write_text(json.dumps({"check_command_prefix": "docker"}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
