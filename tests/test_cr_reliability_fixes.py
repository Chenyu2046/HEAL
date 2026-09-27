from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.config import Config, ToolLimits
from repair_agent.domain import (
    ActionKind,
    BatchProposal,
    Candidate,
    RiskClass,
    RunRecord,
    Stage,
    ValidationClass,
    ValidationResult,
    sha256_bytes,
    sha256_text,
)
from repair_agent.orchestrator import RepairOrchestrator
from repair_agent.runtime.store import RunStore, StoreError
from repair_agent.runtime.workspace import GitWorktreeManager, git_tree_oid, tree_hash
from repair_agent.validation.candidate import CandidateError, CandidateFreezer
from repair_agent.validation.results import IndependentValidator, ValidationPolicy


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def make_repo(parent: Path, name: str) -> tuple[Path, str]:
    repo = parent / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run_git(repo, "config", "user.name", "CR test")
    run_git(repo, "config", "user.email", "cr@example.invalid")
    (repo / "source.c").write_text("int value = 1;\n", encoding="utf-8")
    run_git(repo, "add", "source.c")
    run_git(repo, "commit", "-m", "base")
    return repo, run_git(repo, "rev-parse", "HEAD")


class ReliabilityCRTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.commit = make_repo(self.root, "source")
        self.store = RunStore(self.root / "store")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def candidate(self, *, run_id: str = "run-1", candidate_id: str = "cand-1") -> Candidate:
        return Candidate(
            candidate_id=candidate_id,
            run_id=run_id,
            base_commit=self.commit,
            tree_hash=tree_hash(self.repo),
            artifact_hash=sha256_bytes(b"candidate diff"),
            report_hash=sha256_text("candidate report"),
            changed_files=(),
            finding_ids=("finding-1",),
            git_tree_oid=git_tree_oid(self.repo),
            candidate_commit=self.commit,
        )

    def persist_candidate(
        self,
        stage: Stage = Stage.CANDIDATE_FROZEN,
        *,
        run_id: str = "run-1",
        candidate_id: str = "cand-1",
    ) -> Candidate:
        candidate = self.candidate(run_id=run_id, candidate_id=candidate_id)
        self.store.create_run(
            RunRecord(run_id, "task-1", stage, "config-1", "model-1", {}),
            {
                "task": {"repo": str(self.repo), "base_commit": self.commit},
                "candidate_id": candidate.candidate_id,
                "integration_workspace": str(self.repo),
                "resolved_source_repo": str(self.repo.resolve()),
                "resolved_base_commit": self.commit,
            },
        )
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-diff", "candidate-diff", b"candidate diff")
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-report", "candidate-report", "candidate report")
        self.store.save_candidate(candidate)
        return candidate

    def test_local_infra_failure_has_a_legal_atomic_transition(self) -> None:
        candidate = self.persist_candidate(Stage.HUMAN_REVIEW)
        orchestrator = RepairOrchestrator(
            Config(workspace_parent=str(self.root / "worktrees"), artifact_root=str(self.root / "artifacts")),
            store=self.store,
        )

        result = orchestrator.validate_local(
            candidate.candidate_id,
            commit=self.commit,
            config_id="local-config",
            workspace=self.repo,
            commands={"build": ("heal-command-that-does-not-exist",)},
        )

        run = self.store.get_run(candidate.run_id)
        self.assertEqual(result.classification, ValidationClass.INFRA_FAIL)
        self.assertEqual(run["stage"], Stage.RETRY_INFRA.value)
        self.assertEqual(run["validation_id"], result.validation_id)
        self.assertEqual(run["validation_backend"], "local")
        self.assertEqual(run["failure_class"], "INFRA_FAIL")
        self.assertEqual(self.store.list_validations(candidate.candidate_id)[0].validation_id, result.validation_id)

    def test_local_validation_and_stage_update_roll_back_together(self) -> None:
        candidate = self.persist_candidate(Stage.HUMAN_REVIEW)
        result = ValidationResult(
            validation_id="validation-cr-atomic",
            candidate_id=candidate.candidate_id,
            candidate_tree_hash=candidate.tree_hash,
            revision=self.commit,
            actual_tested_commit=self.commit,
            ci_run_id="local-cr-atomic",
            config_id="local-config",
            backend="local",
            classification=ValidationClass.INFRA_FAIL,
            checks={"build": "INFRA_FAIL"},
            candidate_commit=self.commit,
        )
        self.store._db.execute(
            "CREATE TRIGGER fail_stage_update BEFORE UPDATE OF stage ON runs "
            "WHEN OLD.run_id='run-1' AND NEW.stage <> OLD.stage "
            "BEGIN SELECT RAISE(ABORT, 'injected stage write failure'); END"
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_local_validation_and_transition(result, run_id=candidate.run_id)

        self.assertEqual(self.store.get_run(candidate.run_id)["stage"], Stage.HUMAN_REVIEW.value)
        self.assertEqual(self.store.list_validations(candidate.candidate_id), ())

    def test_local_result_classes_follow_the_explicit_stage_mapping(self) -> None:
        cases = (
            (Stage.CANDIDATE_FROZEN, ValidationClass.VALIDATION_PASS, Stage.HUMAN_REVIEW),
            (Stage.HUMAN_REVIEW, ValidationClass.CODE_FAIL, Stage.REPAIRING),
            (Stage.RETRY_INFRA, ValidationClass.VALIDATION_PASS, Stage.HUMAN_REVIEW),
            (Stage.REVIEW_REQUIRED, ValidationClass.INCONCLUSIVE, Stage.REVIEW_REQUIRED),
        )
        for index, (source, classification, expected) in enumerate(cases):
            with self.subTest(source=source, classification=classification):
                run_id = f"mapping-run-{index}"
                candidate = self.candidate(run_id=run_id, candidate_id=f"mapping-candidate-{index}")
                self.store.create_run(
                    RunRecord(run_id, run_id, source, "config-1", "model-1", {}),
                    {"candidate_id": candidate.candidate_id},
                )
                self.store.save_candidate(candidate)
                result = ValidationResult(
                    validation_id=f"mapping-validation-{index}",
                    candidate_id=candidate.candidate_id,
                    candidate_tree_hash=candidate.tree_hash,
                    revision=self.commit,
                    actual_tested_commit=self.commit,
                    ci_run_id=f"local-mapping-{index}",
                    config_id="local-config",
                    backend="local",
                    classification=classification,
                    checks={},
                    candidate_commit=self.commit,
                )
                payload = {"new_attempt_id": f"attempt-{index}"} if classification == ValidationClass.CODE_FAIL else None

                _, target = self.store.save_local_validation_and_transition(
                    result,
                    run_id=run_id,
                    payload_update=payload,
                )

                self.assertEqual(target, expected)

    def test_resume_restores_from_persisted_repo_not_current_config(self) -> None:
        candidate = self.candidate()
        other_repo, _ = make_repo(self.root, "current-config-repo")
        run_id = candidate.run_id
        manager = GitWorktreeManager(self.root / "worktrees")
        integration = manager.create(
            task_id=f"{run_id}-integration",
            source_repo=self.repo,
            base_commit=self.commit,
            run_id=run_id,
            role="integration",
        )
        manager.remove(integration)
        self.store.create_run(
            RunRecord(run_id, "task-1", Stage.HUMAN_REVIEW, "config-1", "model-1", {}),
            {
                "task": {"repo": str(other_repo), "base_commit": self.commit},
                "candidate_id": candidate.candidate_id,
                "integration_workspace": str(integration.path),
                "resolved_source_repo": str(self.repo.resolve()),
                "resolved_base_commit": self.commit,
            },
        )
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-diff", "candidate-diff", b"candidate diff")
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-report", "candidate-report", "candidate report")
        self.store.save_candidate(candidate)
        orchestrator = RepairOrchestrator(
            Config(
                source_repo=str(other_repo),
                workspace_parent=str(self.root / "reconfigured-workspaces"),
                artifact_root=str(self.root / "artifacts"),
            ),
            store=self.store,
        )

        orchestrator._restore_candidate_workspace(candidate, self.store.get_run(run_id))

        integration = self.store.get_run(run_id)["integration_workspace"]
        entries = GitWorktreeManager(self.root / "reconfigured-workspaces").entries()
        restored = next(item for item in entries if Path(item["path"]).resolve() == Path(integration).resolve())
        self.assertEqual(Path(restored["source_repo"]).resolve(), self.repo.resolve())
        self.assertTrue(Path(integration).is_dir())

    def test_resume_does_not_replace_local_result_with_stale_ci_validation(self) -> None:
        candidate = self.candidate()
        manager = GitWorktreeManager(self.root / "worktrees")
        integration = manager.create(
            task_id=f"{candidate.run_id}-integration",
            source_repo=self.repo,
            base_commit=self.commit,
            run_id=candidate.run_id,
            role="integration",
        )
        self.store.create_run(
            RunRecord(candidate.run_id, "task-1", Stage.RETRY_INFRA, "config-1", "model-1", {}),
            {
                "task": {"repo": str(self.repo), "base_commit": self.commit},
                "candidate_id": candidate.candidate_id,
                "integration_workspace": str(integration.path),
                "resolved_source_repo": str(self.repo.resolve()),
                "resolved_base_commit": self.commit,
                "ci_run_id": "old-ci-run",
            },
        )
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-diff", "candidate-diff", b"candidate diff")
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-report", "candidate-report", "candidate report")
        self.store.save_candidate(candidate)
        remote_pass = ValidationResult(
            validation_id="validation-old-ci-pass",
            candidate_id=candidate.candidate_id,
            candidate_tree_hash=candidate.tree_hash,
            revision=self.commit,
            actual_tested_commit=self.commit,
            ci_run_id="old-ci-run",
            config_id="remote-config",
            backend="ci",
            classification=ValidationClass.VALIDATION_PASS,
            checks={"build": "PASS", "ut": "PASS", "scan": "PASS"},
            candidate_commit=self.commit,
        )
        self.store.save_validation(remote_pass)
        local_infra = ValidationResult(
            validation_id="validation-local-infra",
            candidate_id=candidate.candidate_id,
            candidate_tree_hash=candidate.tree_hash,
            revision=self.commit,
            actual_tested_commit=self.commit,
            ci_run_id="local-infra-run",
            config_id="local-config",
            backend="local",
            classification=ValidationClass.INFRA_FAIL,
            checks={"build": "INFRA_FAIL"},
            candidate_commit=self.commit,
        )
        self.store.save_local_validation_and_transition(local_infra, run_id=candidate.run_id)
        orchestrator = RepairOrchestrator(
            Config(workspace_parent=str(self.root / "worktrees"), artifact_root=str(self.root / "artifacts")),
            store=self.store,
        )

        resumed = orchestrator.resume(candidate.run_id)

        self.assertEqual(resumed["run"]["stage"], Stage.RETRY_INFRA.value)
        self.assertEqual(resumed["run"]["validation_backend"], "local")
        self.assertEqual(resumed["run"]["validation_id"], local_infra.validation_id)

    def test_resume_still_recovers_final_ci_evidence_after_local_validation(self) -> None:
        candidate = self.candidate()
        manager = GitWorktreeManager(self.root / "worktrees")
        integration = manager.create(
            task_id=f"{candidate.run_id}-integration",
            source_repo=self.repo,
            base_commit=self.commit,
            run_id=candidate.run_id,
            role="integration",
        )
        self.store.create_run(
            RunRecord(candidate.run_id, "task-1", Stage.CI_PENDING, "config-1", "model-1", {}),
            {
                "task": {"repo": str(self.repo), "base_commit": self.commit},
                "candidate_id": candidate.candidate_id,
                "integration_workspace": str(integration.path),
                "resolved_source_repo": str(self.repo.resolve()),
                "resolved_base_commit": self.commit,
                "ci_run_id": "current-ci-run",
                "validation_backend": "local",
            },
        )
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-diff", "candidate-diff", b"candidate diff")
        self.store.save_artifact(candidate.run_id, f"{candidate.candidate_id}-report", "candidate-report", "candidate report")
        self.store.save_candidate(candidate)
        self.store.merge_ci_checks(
            candidate_id=candidate.candidate_id,
            ci_run_id="current-ci-run",
            revision=self.commit,
            actual_tested_commit=self.commit,
            checks={"build": "PASS", "ut": "PASS", "scan": "PASS"},
            required_checks=("build", "ut", "scan"),
            final=True,
            config_id="ci-config",
            backend="ci",
        )
        orchestrator = RepairOrchestrator(
            Config(workspace_parent=str(self.root / "worktrees"), artifact_root=str(self.root / "artifacts")),
            store=self.store,
        )

        resumed = orchestrator.resume(candidate.run_id)

        self.assertEqual(resumed["run"]["stage"], Stage.VERIFIED.value)
        self.assertEqual(resumed["run"]["validation_backend"], "ci")

    def _freeze_inputs(self, run_id: str = "run-freeze") -> tuple[Path, str, BatchProposal]:
        repo, base = make_repo(self.root, run_id)
        self.store.create_run(RunRecord(run_id, run_id, Stage.INTEGRATING, "config-1", "model-1", {}), {})
        (repo / "source.c").write_text("int value = 2;\n", encoding="utf-8")
        run_git(repo, "add", "source.c")
        proposal = BatchProposal(
            batch_id="batch-1",
            worker_id="worker-1",
            base_commit=base,
            workspace_revision=1,
            action_map={"finding-1": ActionKind.FIX_CANDIDATE},
            changed_files=("source.c",),
            diff="candidate diff",
            diff_hash="diff-hash",
            risk=RiskClass.LOW,
        )
        return repo, base, proposal

    def _candidate_refs(self, repo: Path) -> str:
        return run_git(repo, "for-each-ref", "--format=%(refname)", "refs/heal/candidates")

    def test_freeze_scope_failure_removes_its_candidate_ref(self) -> None:
        repo, base, proposal = self._freeze_inputs()
        freezer = CandidateFreezer(self.store, ToolLimits(max_changed_files=0))

        with self.assertRaises(CandidateError):
            freezer.freeze(run_id="run-freeze", workspace=repo, base_commit=base, proposals=(proposal,), finding_ids=("finding-1",))

        self.assertEqual(self._candidate_refs(repo), "")

    def test_freeze_store_failure_removes_its_candidate_ref(self) -> None:
        repo, base, proposal = self._freeze_inputs("run-store-failure")
        freezer = CandidateFreezer(self.store)

        with patch.object(self.store, "save_frozen_candidate", side_effect=StoreError("injected persistence failure")):
            with self.assertRaises(CandidateError):
                freezer.freeze(run_id="run-store-failure", workspace=repo, base_commit=base, proposals=(proposal,), finding_ids=("finding-1",))

        self.assertEqual(self._candidate_refs(repo), "")

    def test_successful_freeze_keeps_candidate_ref_and_identity(self) -> None:
        repo, base, proposal = self._freeze_inputs("run-freeze-success")
        freezer = CandidateFreezer(self.store)

        frozen = freezer.freeze(
            run_id="run-freeze-success",
            workspace=repo,
            base_commit=base,
            proposals=(proposal,),
            finding_ids=("finding-1",),
        )

        stored = self.store.get_candidate(frozen.candidate.candidate_id)
        self.assertEqual(stored.candidate_commit, frozen.candidate.candidate_commit)
        self.assertEqual(stored.git_tree_oid, frozen.candidate.git_tree_oid)
        self.assertEqual(stored.tree_hash, frozen.candidate.tree_hash)
        self.assertEqual(self._candidate_refs(repo), f"refs/heal/candidates/{frozen.candidate.candidate_id}")

    def test_simulated_validation_policy_is_honored(self) -> None:
        candidate = self.candidate()
        args = {
            "candidate": candidate,
            "revision": self.commit,
            "actual_tested_commit": self.commit,
            "ci_run_id": "simulated-run",
            "config_id": "simulated-config",
            "backend": "simulated",
            "checks": {"build": "PASS", "ut": "PASS", "scan": "PASS"},
        }

        denied = IndependentValidator(ValidationPolicy(allow_simulated=False)).classify(**args)
        allowed = IndependentValidator(ValidationPolicy(allow_simulated=True)).classify(**args)

        self.assertEqual(denied.classification, ValidationClass.INCONCLUSIVE)
        self.assertEqual(allowed.classification, ValidationClass.VALIDATION_PASS)


if __name__ == "__main__":
    unittest.main()
