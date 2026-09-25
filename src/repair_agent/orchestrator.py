"""End-to-end orchestration from normalized input to candidate and CI feedback."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .adapters.ci import CIAdapter, CIRequest, NotConfiguredCIAdapter
from .adapters.gerrit import GerritAdapter, NotConfiguredGerritAdapter, SubmissionResponse
from .agent import AgentLoop, AgentResult
from .concurrency import IntegrationEngine, WorkerPool, requeue_for_expanded_scope
from .config import Config
from .domain import (
    Candidate,
    HumanApproval,
    RepairTask,
    RiskClass,
    RunRecord,
    Stage,
    SubmissionIntent,
    SubmissionStatus,
    ValidationClass,
    ValidationResult,
    canonical_json,
    issue_id,
    to_primitive,
    utc_now,
)
from .memory import EpisodeStore
from .models import ModelAdapter, ScriptedModel
from .planning import BatchPlanner, InputNormalizer, WorkingBatch, ConflictAwareScheduler
from .reporting import ReportWriter
from .runtime.store import RunStore, StoreError
from .runtime.workspace import GitWorktreeManager, WorkspaceError, WorkspaceState
from .skills import SkillRouter, SkillStore
from .tools.executor import ToolExecutor
from .validation.candidate import CandidateError, CandidateFreezer
from .validation.local import CommandSpec, LocalValidator
from .validation.results import IndependentValidator


ModelFactory = Callable[[RepairTask, str], ModelAdapter]


class OrchestratorError(RuntimeError):
    """A workflow operation violated an explicit stage or identity contract."""


@dataclass(frozen=True)
class WorkflowResult:
    run_id: str
    stage: Stage
    candidate: Candidate | None = None
    proposals: tuple[object, ...] = ()
    review_reasons: tuple[str, ...] = ()
    report_paths: tuple[str, ...] = ()


class RepairOrchestrator:
    def __init__(
        self,
        config: Config,
        *,
        store: RunStore | None = None,
        model_factory: ModelFactory | None = None,
        gerrit: GerritAdapter | None = None,
        ci: CIAdapter | None = None,
    ) -> None:
        self.config = config
        self.store = store or RunStore(config.artifact_root)
        self.model_factory = model_factory or (lambda task, worker_id: ScriptedModel([], model_id="scripted"))
        self.gerrit = gerrit or NotConfiguredGerritAdapter()
        self.ci = ci or NotConfiguredCIAdapter()
        self.normalizer = InputNormalizer()
        self.batch_planner = BatchPlanner()
        self.scheduler = ConflictAwareScheduler()
        self.freezer = CandidateFreezer(self.store)
        self.validator = IndependentValidator()
        self.local_validator = LocalValidator(self.validator)
        self.report_writer = ReportWriter()

    def run(self, payload: Mapping[str, Any]) -> WorkflowResult:
        normalized = self.normalizer.normalize(payload, default_repo=self.config.source_repo)
        task = normalized.task
        record = RunRecord(task.run_id, task.task_id, Stage.RECEIVED, self.config.version, task.model_id, {})
        self.store.create_run(record, {"task": to_primitive(task), "normalization_warnings": list(normalized.warnings), "config": to_primitive(self.config)})
        self.store.transition(task.run_id, Stage.REPAIRING)
        source_repo = Path(self.config.source_repo or task.repo).resolve()
        manager = GitWorktreeManager(self.config.workspace_parent)
        try:
            self._assert_git_source(source_repo, task.base_commit)
        except WorkspaceError as exc:
            return self._finish_review(task.run_id, (f"NOT_CONFIGURED/WORKSPACE: {exc}",))

        batches = self.batch_planner.plan(task)
        slots = self.scheduler.schedule(batches, self.config.max_workers)
        worker_pool = WorkerPool(self.config.max_workers)
        agent_results: list[AgentResult] = []
        worktrees: dict[str, str] = {}
        try:
            for slot in slots:
                envelopes = worker_pool.run(slot, lambda batch, worker_id: self._run_worker(task, batch, worker_id, manager, source_repo, worktrees))
                for envelope in envelopes:
                    result = envelope.result
                    if result.proposal is not None and requeue_for_expanded_scope(envelope.batch, result.proposal.changed_files):
                        expanded_batch = replace(envelope.batch, batch_id=f"{envelope.batch.batch_id}-rescheduled", known_files=frozenset(set(envelope.batch.known_files) | set(result.proposal.changed_files)))
                        retry = worker_pool.run((expanded_batch,), lambda batch, worker_id: self._run_worker(task, batch, worker_id, manager, source_repo, worktrees))[0].result
                        if retry.proposal is not None and requeue_for_expanded_scope(expanded_batch, retry.proposal.changed_files):
                            result = replace(retry, proposal=None, review_required=True, reason="worker expanded its rescheduled modification range; scheduler review is required")
                        else:
                            result = retry
                    agent_results.append(result)
        except Exception as exc:
            return self._finish_review(task.run_id, (f"worker execution failed: {type(exc).__name__}: {exc}",))
        proposals = tuple(result.proposal for result in agent_results if result.proposal is not None)
        reasons = tuple(result.reason for result in agent_results if result.review_required and result.reason)
        not_executed = [observation.tool_call_id for result in agent_results for observation in result.observations if observation.status.value == "NOT_EXECUTED"]
        if reasons or len(proposals) != len(batches):
            self.store.transition(task.run_id, Stage.BATCH_REVIEW, payload_update={"worker_review_reasons": list(reasons), "not_executed": not_executed, "worktrees": worktrees, "budget_used": self._aggregate_usage(agent_results)})
            return self._finish_review(task.run_id, reasons or ("not every batch produced a complete proposal",), stage=Stage.REVIEW_REQUIRED)

        self.store.transition(task.run_id, Stage.BATCH_REVIEW, payload_update={"worktrees": worktrees, "batch_count": len(batches), "budget_used": self._aggregate_usage(agent_results)})
        self.store.transition(task.run_id, Stage.INTEGRATING)
        try:
            integration = manager.create(task_id=f"{task.run_id}-integration", source_repo=source_repo, base_commit=task.base_commit)
            integration_workspace = integration.path
            worktrees = {**worktrees, "integration": str(integration_workspace)}
            integration_result = IntegrationEngine().integrate(integration_workspace, proposals)
        except WorkspaceError as exc:
            return self._finish_review(task.run_id, (f"integration workspace unavailable: {exc}",), stage=Stage.REVIEW_REQUIRED)
        if not integration_result.applied:
            self.store.transition(task.run_id, Stage.REPAIRING, payload_update={"integration_conflicts": list(integration_result.conflicts), "integration_feedback": "re-plan conflicting batches before another integration attempt", "worktrees": worktrees}, failure_class="INTEGRATION_CONFLICT")
            paths = self._write_report(task.run_id)
            return WorkflowResult(task.run_id, Stage.REPAIRING, None, proposals, integration_result.conflicts, paths)

        self.store.transition(task.run_id, Stage.INTEGRATING, payload_update={"semantic_warnings": list(integration_result.semantic_warnings), "integration_workspace": str(integration_workspace), "worktrees": worktrees})
        try:
            frozen = self.freezer.freeze(run_id=task.run_id, workspace=integration_workspace, base_commit=task.base_commit, proposals=proposals, finding_ids=(issue_id(issue) for issue in task.issues))
        except CandidateError as exc:
            return self._finish_review(task.run_id, (f"candidate freeze blocked: {exc}",), stage=Stage.REVIEW_REQUIRED)
        self.store.transition(task.run_id, Stage.CANDIDATE_FROZEN, payload_update={"candidate_id": frozen.candidate.candidate_id, "tree_hash": frozen.candidate.tree_hash, "candidate_artifacts": list(frozen.artifact_ids)})
        self.store.transition(task.run_id, Stage.HUMAN_REVIEW)
        report_paths = self._write_report(task.run_id)
        return WorkflowResult(task.run_id, Stage.HUMAN_REVIEW, frozen.candidate, proposals, integration_result.semantic_warnings, report_paths)

    def _assert_git_source(self, source_repo: Path, base_commit: str) -> None:
        manager = GitWorktreeManager(self.config.workspace_parent)
        # A non-mutating validation keeps the actual worktree creation in the worker path.
        if not source_repo.is_dir():
            raise WorkspaceError(f"source repository is not configured: {source_repo}")
        result = manager._git(["rev-parse", "--verify", base_commit], source_repo)
        if result.returncode != 0:
            raise WorkspaceError(f"base commit is not available: {base_commit}")

    def _run_worker(self, task: RepairTask, batch: WorkingBatch, worker_id: str, manager: GitWorktreeManager, source_repo: Path, worktrees: dict[str, str]) -> AgentResult:
        handle = manager.create(task_id=f"{task.run_id}-{batch.batch_id}", source_repo=source_repo, base_commit=task.base_commit)
        worktrees[batch.batch_id] = str(handle.path)
        workspace = WorkspaceState(handle.path, task.base_commit, protected_paths=self.config.protected_paths)
        skill_store = SkillStore(self.config.skill_root)
        isolated_worker_id = f"{worker_id}-{batch.batch_id}"
        episode_store = EpisodeStore(self.config.memory_root, worker_id=isolated_worker_id)
        executor = ToolExecutor(workspace, limits=self.config.tools, skill_store=skill_store, episode_store=episode_store)
        router = SkillRouter(skill_store)
        model = self.model_factory(task, isolated_worker_id)
        loop = AgentLoop(task, worker_id=isolated_worker_id, model=model, executor=executor, skill_router=router, chunking_enabled=self.config.chunking_enabled, trace_callback=lambda observation: self.store.record_trace(task.run_id, {"worker_id": isolated_worker_id, "batch_id": batch.batch_id, "observation": observation}))
        result = loop.run(batch.batch_id, batch.issues)
        self.store.record_trace(task.run_id, {"worker_id": isolated_worker_id, "batch_id": batch.batch_id, "usage": to_primitive(result.usage), "review_required": result.review_required, "reason": result.reason})
        return result

    @staticmethod
    def _aggregate_usage(results: Sequence[AgentResult]) -> dict[str, int | float]:
        return {
            "model_calls": sum(result.usage.model_calls for result in results),
            "tool_calls": sum(result.usage.tool_calls for result in results),
            "tokens": sum(result.usage.tokens for result in results),
            "edit_attempts": sum(result.usage.edit_attempts for result in results),
        }

    def approve(self, candidate_id: str, *, reviewer: str, reason: str, approved: bool) -> HumanApproval:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        self._assert_candidate_current(candidate)
        approval = HumanApproval(candidate_id, candidate.tree_hash, approved, reviewer, reason)
        self.store.save_approval(approval)
        self.store.transition(candidate.run_id, Stage.HUMAN_REVIEW, payload_update={"approval": to_primitive(approval)})
        return approval

    def submit(self, candidate_id: str, *, branch: str, change_id: str, fixed_commit: str) -> SubmissionResponse | CIRequest:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        approval = self.store.get_approval(candidate_id)
        if approval is None or not approval.approved or approval.tree_hash != candidate.tree_hash:
            raise OrchestratorError("candidate requires approval bound to its current tree")
        self._assert_candidate_current(candidate)
        if not fixed_commit:
            raise OrchestratorError("submission requires a fixed commit revision")
        current_run = self.store.get_run(candidate.run_id) or {}
        if current_run.get("stage") == Stage.SUBMISSION_UNKNOWN.value:
            raise OrchestratorError("submission response is unknown; reconcile the same Change-Id and fixed revision before retrying")
        task_payload = current_run.get("task", {})
        repo = str(task_payload.get("repo", candidate.run_id)) if isinstance(task_payload, Mapping) else candidate.run_id
        intent = SubmissionIntent(f"submission-{uuid.uuid4().hex}", candidate_id, candidate.tree_hash, repo, branch, change_id, fixed_commit)
        self.store.save_submission(intent)
        self.store.transition(candidate.run_id, Stage.SUBMITTING, payload_update={"submission_id": intent.submission_id, "change_id": change_id, "fixed_commit": fixed_commit})
        response = self.gerrit.submit(intent, candidate)
        if response.status == SubmissionStatus.SUBMISSION_UNKNOWN or not response.response_known:
            self.store.transition(candidate.run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"submission_response": to_primitive(response)})
            return response
        if response.status != SubmissionStatus.SUBMITTED:
            self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"submission_response": to_primitive(response)}, failure_class=response.status.value)
            return response
        submitted_intent = SubmissionIntent(intent.submission_id, intent.candidate_id, intent.candidate_tree_hash, intent.repo, intent.branch, intent.change_id, intent.fixed_commit, SubmissionStatus.SUBMITTED, response.remote_change, response.patch_set)
        self.store.save_submission(submitted_intent)
        self.store.transition(candidate.run_id, Stage.CI_PENDING, payload_update={"remote_change": response.remote_change, "patch_set": response.patch_set})
        ci_request = self.ci.submit(candidate, submitted_intent)
        if not ci_request.accepted:
            self.store.transition(candidate.run_id, Stage.RETRY_INFRA, payload_update={"ci_request": to_primitive(ci_request)}, failure_class="INFRA_FAIL")
        else:
            self.store.transition(candidate.run_id, Stage.CI_PENDING, payload_update={"ci_run_id": ci_request.run_id, "validation_backend": ci_request.backend})
        return ci_request

    def reconcile_submission(self, candidate_id: str, intent: SubmissionIntent) -> SubmissionResponse:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None or candidate.tree_hash != intent.candidate_tree_hash:
            raise OrchestratorError("submission reconciliation identity mismatch")
        response = self.gerrit.query(intent)
        if response.status == SubmissionStatus.SUBMITTED and response.response_known:
            self.store.transition(candidate.run_id, Stage.CI_PENDING, payload_update={"remote_change": response.remote_change, "patch_set": response.patch_set, "reconciled": True})
        elif not response.response_known:
            self.store.transition(candidate.run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"reconcile_response": to_primitive(response)})
        elif response.safe_to_retry:
            self.store.transition(candidate.run_id, Stage.HUMAN_REVIEW, payload_update={"reconciled_absent": True, "reconcile_response": to_primitive(response)})
        return response

    def receive_ci(self, candidate_id: str, *, ci_run_id: str, revision: str, actual_tested_commit: str | None, config_id: str, backend: str, checks: Mapping[str, str], evidence: Mapping[str, Any] | None = None) -> ValidationResult:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        run = self.store.get_run(candidate.run_id) or {}
        current_stage = run.get("stage")
        expected_revision = run.get("fixed_commit")
        identity_match = not expected_revision or expected_revision == revision or expected_revision == actual_tested_commit
        if not identity_match:
            evidence = {**dict(evidence or {}), "ignored_current_stage": True, "reason": "callback revision is older than current submission"}
            result = self.validator.classify(candidate=candidate, revision=revision, actual_tested_commit=actual_tested_commit, ci_run_id=ci_run_id, config_id=config_id, backend=backend, checks={}, evidence=evidence)
            return self.store.save_validation(result)
        result = self.validator.classify(candidate=candidate, revision=revision, actual_tested_commit=actual_tested_commit, ci_run_id=ci_run_id, config_id=config_id, backend=backend, checks=checks, evidence=evidence)
        result = self.store.save_validation(result)
        if result.duplicate:
            return result
        if current_stage == Stage.VERIFIED.value or current_stage not in {Stage.CI_PENDING.value, Stage.RETRY_INFRA.value}:
            return result
        if result.classification == ValidationClass.VALIDATION_PASS:
            self.store.transition(candidate.run_id, Stage.VERIFIED, payload_update={"validation_id": result.validation_id, "validation_backend": backend})
        elif result.classification == ValidationClass.CODE_FAIL:
            self.store.transition(candidate.run_id, Stage.REPAIRING, payload_update={"validation_id": result.validation_id, "new_attempt_id": f"attempt-{uuid.uuid4().hex}"}, failure_class="CODE_FAIL")
        elif result.classification == ValidationClass.INFRA_FAIL:
            self.store.transition(candidate.run_id, Stage.RETRY_INFRA, payload_update={"validation_id": result.validation_id}, failure_class="INFRA_FAIL")
        else:
            self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"validation_id": result.validation_id}, failure_class="INCONCLUSIVE")
        return result

    def resume(self, run_id: str) -> dict[str, Any]:
        issues = self.store.reconcile(run_id)
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError(f"run not found: {run_id}")
        candidates = self.store.list_candidates(run_id)
        if not issues and candidates and run["stage"] == Stage.INTEGRATING.value:
            candidate = candidates[-1]
            self.store.transition(run_id, Stage.CANDIDATE_FROZEN, payload_update={"candidate_id": candidate.candidate_id, "tree_hash": candidate.tree_hash, "recovered": True})
            self.store.transition(run_id, Stage.HUMAN_REVIEW)
            run = self.store.get_run(run_id) or run
        elif not issues and run["stage"] == Stage.SUBMITTING.value:
            self.store.transition(run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"recovered": True, "recovery_action": "reconcile before retry"})
            run = self.store.get_run(run_id) or run
        action = "replan_required" if issues or run["stage"] in {Stage.REVIEW_REQUIRED.value, Stage.FAILED.value} else "reconcile_external_side_effect" if run["stage"] == Stage.SUBMISSION_UNKNOWN.value else "continue_from_checkpoint"
        return {"run": run, "recovery_issues": issues, "action": action}

    def validate_local(self, candidate_id: str, *, commit: str, config_id: str, workspace: str | Path, commands: Mapping[str, Sequence[str]]) -> ValidationResult:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        self._assert_candidate_current(candidate)
        specs = {name: CommandSpec(tuple(argv), self.config.tools.command_timeout_seconds) for name, argv in commands.items()}
        result = self.local_validator.run(candidate=candidate, workspace=Path(workspace), commit=commit, config_id=config_id, commands=specs)
        result = self.store.save_validation(result)
        if result.duplicate:
            return result
        current_stage = (self.store.get_run(candidate.run_id) or {}).get("stage")
        if result.classification == ValidationClass.VALIDATION_PASS:
            if current_stage in {Stage.CANDIDATE_FROZEN.value, Stage.HUMAN_REVIEW.value}:
                self.store.transition(candidate.run_id, Stage.HUMAN_REVIEW, payload_update={"validation_id": result.validation_id, "validation_backend": "local"})
            else:
                self.store.transition(candidate.run_id, Stage.VERIFIED, payload_update={"validation_id": result.validation_id, "validation_backend": "local"})
        elif result.classification == ValidationClass.CODE_FAIL:
            self.store.transition(candidate.run_id, Stage.REPAIRING, payload_update={"validation_id": result.validation_id, "new_attempt_id": f"attempt-{uuid.uuid4().hex}"}, failure_class="CODE_FAIL")
        elif result.classification == ValidationClass.INFRA_FAIL:
            self.store.transition(candidate.run_id, Stage.RETRY_INFRA, payload_update={"validation_id": result.validation_id}, failure_class="INFRA_FAIL")
        else:
            self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"validation_id": result.validation_id}, failure_class="INCONCLUSIVE")
        return result

    def _assert_candidate_current(self, candidate: Candidate) -> None:
        run = self.store.get_run(candidate.run_id) or {}
        workspace = run.get("integration_workspace")
        if not workspace:
            raise OrchestratorError("candidate integration workspace is not recorded")
        try:
            self.freezer.assert_current(candidate, Path(workspace))
        except (CandidateError, OSError) as exc:
            raise OrchestratorError(f"candidate invalidated by workspace change: {exc}") from exc

    def report(self, run_id: str) -> tuple[str, ...]:
        if self.store.get_run(run_id) is None:
            raise OrchestratorError(f"run not found: {run_id}")
        return self._write_report(run_id)

    def _finish_review(self, run_id: str, reasons: Sequence[str], *, stage: Stage = Stage.REVIEW_REQUIRED) -> WorkflowResult:
        current = self.store.get_run(run_id)
        if current and Stage(current["stage"]) != stage:
            try:
                self.store.transition(run_id, stage, payload_update={"review_reasons": list(reasons)})
            except StoreError:
                self.store.transition(run_id, Stage.REVIEW_REQUIRED, payload_update={"review_reasons": list(reasons)})
        paths = self._write_report(run_id)
        return WorkflowResult(run_id, Stage.REVIEW_REQUIRED, None, (), tuple(reasons), paths)

    def _write_report(self, run_id: str) -> tuple[str, ...]:
        run = self.store.get_run(run_id) or {}
        candidates = self.store.list_candidates(run_id)
        validations = tuple(validation for candidate in candidates for validation in self.store.list_validations(candidate.candidate_id))
        approved_suppressions = []
        for candidate in candidates:
            approval = self.store.get_approval(candidate.candidate_id)
            if candidate.suppression_candidate_ids and approval is not None and approval.approved:
                approved_suppressions.append({"candidate_id": candidate.candidate_id, "finding_ids": list(candidate.suppression_candidate_ids)})
        payload = {
            "run_id": run_id,
            "stage": run.get("stage"),
            "candidate_id": run.get("candidate_id"),
            "candidate_patches": [to_primitive(candidate) for candidate in candidates],
            "validation_passed": [to_primitive(item) for item in validations if item.classification == ValidationClass.VALIDATION_PASS],
            "approved_suppressions": approved_suppressions,
            "unresolved": run.get("review_reasons", run.get("worker_review_reasons", run.get("integration_conflicts", []))),
            "not_executed": run.get("not_executed", []),
            "infrastructure": run.get("failure_class") or run.get("submission_response") or run.get("ci_request"),
            "semantic_warnings": run.get("semantic_warnings", []),
        }
        output_dir = Path(self.config.artifact_root) / run_id / "report"
        markdown, machine = self.report_writer.write(output_dir, payload)
        self.store.transition(run_id, Stage(run.get("stage", Stage.REVIEW_REQUIRED.value)), payload_update={"report_path": str(markdown), "report_json_path": str(machine)})
        return str(markdown), str(machine)
