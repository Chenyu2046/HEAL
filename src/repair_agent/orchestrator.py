"""End-to-end orchestration from normalized input to candidate and CI feedback."""

from __future__ import annotations

import uuid
import time
from functools import wraps
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .adapters.ci import CIAdapter, CIRequest, NotConfiguredCIAdapter
from .adapters.gerrit import GerritAdapter, NotConfiguredGerritAdapter, SubmissionResponse
from .agent import AgentLoop, AgentResult
from .concurrency import IntegrationEngine, WorkerPool, requeue_for_expanded_scope
from .config import Config
from .context import ContextCache
from .domain import (
    BatchProposal,
    Candidate,
    Budget,
    HumanApproval,
    Observation,
    RepairTask,
    RiskClass,
    RunRecord,
    Stage,
    SubmissionIntent,
    SubmissionStatus,
    ValidationClass,
    ValidationResult,
    ValidationState,
    canonical_json,
    issue_id,
    to_primitive,
    utc_now,
)
from .memory import EpisodeStore
from .models import ModelAdapter, ScriptedModel
from .planning import BatchPlanner, InputNormalizer, WorkingBatch, ConflictAwareScheduler
from .reporting import ReportWriter
from .retry import RetryPolicy
from .runtime.store import RunStore, StoreError
from .runtime.trace import sanitize
from .runtime.workspace import GitWorktreeManager, WorkspaceError, WorkspaceState, cleanup_stale_temp_files
from .skills import SkillRouter, SkillStore
from .tools.executor import ToolExecutor
from .validation.candidate import CandidateError, CandidateFreezer
from .validation.local import CommandSpec, LocalValidator
from .validation.results import IndependentValidator


ModelFactory = Callable[[RepairTask, str], ModelAdapter]


def _candidate_lifecycle_guard(method):
    @wraps(method)
    def guarded(self, candidate_id, *args, **kwargs):
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            return method(self, candidate_id, *args, **kwargs)
        with self.store.lifecycle_guard(candidate.run_id):
            return method(self, candidate_id, *args, **kwargs)
    return guarded


def _run_lifecycle_guard(method):
    @wraps(method)
    def guarded(self, run_id, *args, **kwargs):
        with self.store.lifecycle_guard(run_id):
            return method(self, run_id, *args, **kwargs)
    return guarded


def _observation_trace(observation: Observation) -> dict[str, Any]:
    """Persist outcome metadata without retaining source contents or errors."""
    return {
        "tool_call_id": observation.tool_call_id,
        "tool": observation.tool,
        "status": observation.status.value,
        "artifact_ref": observation.artifact_ref,
        "source_paths": list(observation.source_paths),
        "workspace_revision": observation.workspace_revision,
        "file_hashes": dict(observation.file_hashes),
        "complete": observation.complete,
        "elapsed_ms": observation.elapsed_ms,
        "content_omitted": observation.content is not None,
        "error_present": observation.error is not None,
    }


def _worker_result_artifact_payload(result: AgentResult) -> dict[str, Any]:
    payload = to_primitive(result)
    payload["observations"] = [_observation_trace(item) for item in result.observations]
    payload["reason"] = None
    return sanitize(payload)


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
        self.freezer = CandidateFreezer(self.store, config.tools)
        self.validator = IndependentValidator()
        self.local_validator = LocalValidator(self.validator)
        self.report_writer = ReportWriter()

    def run(self, payload: Mapping[str, Any], *, cli_budget_overrides: Mapping[str, Any] | None = None) -> WorkflowResult:
        normalized = self.normalizer.normalize(payload, default_repo=self.config.source_repo, default_budget=self.config.budget, default_mode=self.config.mode, default_model_id=self.config.model.model_id, cli_budget_overrides=cli_budget_overrides)
        with self.store.lifecycle_guard(normalized.task.run_id):
            return self._run_normalized(normalized)

    def _run_normalized(self, normalized) -> WorkflowResult:
        task = normalized.task
        workflow_started_at = time.monotonic()
        manager = GitWorktreeManager(self.config.workspace_parent)
        try:
            source_repo, resolved_base = self._assert_git_source(Path(self.config.source_repo or task.repo), task.base_commit)
        except WorkspaceError as exc:
            record = RunRecord(task.run_id, task.task_id, Stage.RECEIVED, self.config.version, task.model_id, {})
            self.store.create_run(record, {"task": to_primitive(task), "normalization_warnings": list(normalized.warnings), "config": to_primitive(self.config), "budget_used": {}, "worker_budget_usage": {}})
            self.store.transition(task.run_id, Stage.REPAIRING)
            return self._finish_review(task.run_id, (f"NOT_CONFIGURED/WORKSPACE: {exc}",))
        task = replace(task, base_commit=resolved_base, issues=tuple(replace(issue, base_commit=resolved_base) for issue in task.issues))
        record = RunRecord(task.run_id, task.task_id, Stage.RECEIVED, self.config.version, task.model_id, {})
        self.store.create_run(record, {
            "task": to_primitive(task),
            "normalization_warnings": list(normalized.warnings),
            "config": to_primitive(self.config),
            "budget_used": {},
            "worker_budget_usage": {},
            "resolved_source_repo": str(source_repo),
            "resolved_base_commit": resolved_base,
        })
        resolved_run = self.store.get_run(task.run_id) or {}
        source_repo = Path(str(resolved_run["resolved_source_repo"]))
        task = replace(task, base_commit=str(resolved_run["resolved_base_commit"]))
        self.store.transition(task.run_id, Stage.REPAIRING)

        batches = self.batch_planner.plan(task)
        worker_pool = WorkerPool(self.config.max_workers)
        agent_results: list[AgentResult] = []
        completed_batches: dict[str, WorkingBatch] = {}
        worktrees: dict[str, str] = {}
        pending_batches = list(batches)
        expansion_retries: dict[str, int] = {}
        try:
            while pending_batches:
                scheduled = self.scheduler.schedule(tuple(pending_batches), self.config.max_workers)
                slot = scheduled[0]
                selected_ids = {batch.batch_id for batch in slot}
                pending_batches = [batch for batch in pending_batches if batch.batch_id not in selected_ids]
                current_run = self.store.get_run(task.run_id) or {}
                slot_budgets = self._allocate_slot_budgets(task.budget, current_run.get("budget_used", {}), slot)
                envelopes = worker_pool.run(
                    slot,
                    lambda batch, worker_id: self._run_worker(replace(task, budget=slot_budgets[batch.batch_id]), batch, worker_id, manager, source_repo, worktrees, budget_started_at=workflow_started_at),
                )
                for envelope in envelopes:
                    result = envelope.result
                    if result.proposal is not None and requeue_for_expanded_scope(envelope.batch, result.proposal.changed_files):
                        retries = expansion_retries.get(envelope.batch.batch_id, 0)
                        if retries >= 1:
                            result = replace(result, proposal=None, review_required=True, reason="worker expanded its rescheduled modification range; scheduler review is required")
                            agent_results.append(result)
                            completed_batches[envelope.batch.batch_id] = envelope.batch
                            continue
                        expanded_batch = replace(
                            envelope.batch,
                            batch_id=f"{envelope.batch.batch_id}-rescheduled",
                            known_files=frozenset(set(envelope.batch.known_files) | set(result.proposal.changed_files)),
                        )
                        expansion_retries[expanded_batch.batch_id] = retries + 1
                        pending_batches.insert(0, expanded_batch)
                        continue
                    agent_results.append(result)
                    completed_batches[envelope.batch.batch_id] = envelope.batch
        except Exception as exc:
            return self._finish_review(task.run_id, (f"worker execution failed: {type(exc).__name__}",))
        proposals = tuple(result.proposal for result in agent_results if result.proposal is not None)
        reasons = tuple(result.reason for result in agent_results if result.review_required and result.reason)
        not_executed = [observation.tool_call_id for result in agent_results for observation in result.observations if observation.status.value == "NOT_EXECUTED"]
        if reasons or len(proposals) != len(batches):
            current_budget = (self.store.get_run(task.run_id) or {}).get("budget_used", {})
            self.store.transition(task.run_id, Stage.BATCH_REVIEW, payload_update={"worker_review_reasons": list(reasons), "not_executed": not_executed, "worktrees": worktrees, "budget_used": current_budget})
            return self._finish_review(task.run_id, reasons or ("not every batch produced a complete proposal",), stage=Stage.REVIEW_REQUIRED)

        current_budget = (self.store.get_run(task.run_id) or {}).get("budget_used", {})
        self.store.transition(task.run_id, Stage.BATCH_REVIEW, payload_update={"worktrees": worktrees, "batch_count": len(batches), "budget_used": current_budget})
        self.store.transition(task.run_id, Stage.INTEGRATING)
        try:
            integration = manager.create(task_id=f"{task.run_id}-integration", source_repo=source_repo, base_commit=task.base_commit, run_id=task.run_id, role="integration")
            integration_workspace = integration.path
            worktrees = {**worktrees, "integration": str(integration_workspace)}
            integration_result = IntegrationEngine().integrate(integration_workspace, proposals)
        except WorkspaceError as exc:
            return self._finish_review(task.run_id, (f"integration workspace unavailable: {exc}",), stage=Stage.REVIEW_REQUIRED)
        if not integration_result.applied:
            manager.mark(integration, "RECOVERABLE")
            self.store.transition(task.run_id, Stage.REPAIRING, payload_update={"integration_conflicts": list(integration_result.conflicts), "integration_feedback": "re-plan conflicting batches before another integration attempt", "worktrees": worktrees, "integration_replan_attempted": True}, failure_class="INTEGRATION_CONFLICT")
            if not integration_result.conflicting_batches:
                self.store.transition(task.run_id, Stage.REVIEW_REQUIRED, payload_update={"replan_required": True}, failure_class="INTEGRATION_FAILURE")
                paths = self._write_report(task.run_id)
                return WorkflowResult(task.run_id, Stage.REVIEW_REQUIRED, None, proposals, integration_result.conflicts, paths)
            try:
                replan_batch, unaffected_proposals = self._build_integration_replan(task, tuple(completed_batches.values()), proposals, integration_result.conflicting_batches)
            except OrchestratorError as exc:
                return self._finish_review(task.run_id, (f"integration conflict requires manual re-plan: {exc}",))
            current_budget = (self.store.get_run(task.run_id) or {}).get("budget_used", {})
            replan_budget = self._allocate_slot_budgets(task.budget, current_budget, (replan_batch,))[replan_batch.batch_id]
            try:
                replan_result = self._run_worker(replace(task, budget=replan_budget), replan_batch, "integration-replan-1", manager, source_repo, worktrees, budget_started_at=workflow_started_at)
            except Exception as exc:
                return self._finish_review(task.run_id, (f"integration re-plan worker failed: {type(exc).__name__}",))
            if replan_result.proposal is None or replan_result.review_required:
                return self._finish_review(task.run_id, (replan_result.reason or "integration re-plan did not produce a complete proposal",))
            if requeue_for_expanded_scope(replan_batch, replan_result.proposal.changed_files):
                return self._finish_review(task.run_id, ("integration re-plan expanded its known modification range; another scheduler pass is required",))
            proposals = (*unaffected_proposals, replan_result.proposal)
            self.store.transition(task.run_id, Stage.BATCH_REVIEW, payload_update={"integration_replan_batch_id": replan_batch.batch_id, "integration_replan_proposal": to_primitive(replan_result.proposal), "failure_class": None})
            self.store.transition(task.run_id, Stage.INTEGRATING)
            try:
                integration = manager.create(task_id=f"{task.run_id}-integration-replan-1", source_repo=source_repo, base_commit=task.base_commit, run_id=task.run_id, role="integration")
                integration_workspace = integration.path
                worktrees = {**worktrees, "integration-replan-1": str(integration_workspace)}
                integration_result = IntegrationEngine().integrate(integration_workspace, proposals)
            except WorkspaceError as exc:
                return self._finish_review(task.run_id, (f"integration re-plan workspace unavailable: {exc}",))
            if not integration_result.applied:
                manager.mark(integration, "RECOVERABLE")
                self.store.transition(task.run_id, Stage.REVIEW_REQUIRED, payload_update={"integration_replan_conflicts": list(integration_result.conflicts), "replan_required": True}, failure_class="INTEGRATION_CONFLICT")
                paths = self._write_report(task.run_id)
                return WorkflowResult(task.run_id, Stage.REVIEW_REQUIRED, None, proposals, integration_result.conflicts, paths)

        self.store.transition(task.run_id, Stage.INTEGRATING, payload_update={"semantic_warnings": list(integration_result.semantic_warnings), "integration_workspace": str(integration_workspace), "worktrees": worktrees})
        try:
            frozen = self.freezer.freeze(run_id=task.run_id, workspace=integration_workspace, base_commit=task.base_commit, proposals=proposals, finding_ids=(issue_id(issue) for issue in task.issues))
        except CandidateError as exc:
            return self._finish_review(task.run_id, (f"candidate freeze blocked: {exc}",), stage=Stage.REVIEW_REQUIRED)
        manager.mark(integration, "RECOVERABLE")
        self.store.transition(task.run_id, Stage.CANDIDATE_FROZEN, payload_update={"candidate_id": frozen.candidate.candidate_id, "tree_hash": frozen.candidate.tree_hash, "candidate_artifacts": list(frozen.artifact_ids)})
        self.store.transition(task.run_id, Stage.HUMAN_REVIEW)
        report_paths = self._write_report(task.run_id)
        return WorkflowResult(task.run_id, Stage.HUMAN_REVIEW, frozen.candidate, proposals, integration_result.semantic_warnings, report_paths)

    def _assert_git_source(self, source_repo: Path, base_commit: str) -> tuple[Path, str]:
        manager = GitWorktreeManager(self.config.workspace_parent)
        canonical_repo = manager.resolve_source_repo(source_repo)
        return canonical_repo, manager.resolve_commit(canonical_repo, base_commit)

    def _build_integration_replan(
        self,
        task: RepairTask,
        batches: Sequence[WorkingBatch],
        proposals: Sequence[BatchProposal],
        conflicting_batch_ids: Sequence[str],
    ) -> tuple[WorkingBatch, tuple[BatchProposal, ...]]:
        batch_by_id = {batch.batch_id: batch for batch in batches}
        proposal_by_id = {proposal.batch_id: proposal for proposal in proposals}
        selected_ids = set(conflicting_batch_ids)
        if not selected_ids or not selected_ids.issubset(batch_by_id) or not selected_ids.issubset(proposal_by_id):
            raise OrchestratorError("conflicting batch identities cannot be reconstructed")
        selected_batches = [batch_by_id[batch_id] for batch_id in sorted(selected_ids)]
        selected_proposals = [proposal_by_id[batch_id] for batch_id in sorted(selected_ids)]
        issues_by_id = {(str(issue.source_type), issue_id(issue)): issue for batch in selected_batches for issue in batch.issues}
        issues = tuple(issues_by_id.values())
        known_files = set().union(*(batch.known_files for batch in selected_batches))
        known_files.update(path for proposal in selected_proposals for path in proposal.changed_files)
        if not issues or len(issues) > self.batch_planner.max_issues or len(known_files) > self.batch_planner.max_files:
            raise OrchestratorError("conflicting batch exceeds safe re-plan issue/file limits")
        risk_order = {RiskClass.UNKNOWN: 0, RiskClass.LOW: 1, RiskClass.MEDIUM: 2, RiskClass.HIGH: 3}
        risk = max((batch.risk for batch in selected_batches), key=lambda item: risk_order[item])
        batch = WorkingBatch(
            batch_id=f"{task.task_id}-integration-replan-1",
            task_id=task.task_id,
            issues=issues,
            affinity_key=("integration-replan", "<unknown-symbol>", "<unknown-lifecycle>"),
            known_files=frozenset(known_files),
            known_symbols=frozenset().union(*(batch.known_symbols for batch in selected_batches)),
            lifecycle_domains=frozenset().union(*(batch.lifecycle_domains for batch in selected_batches)),
            risk=risk,
        )
        return batch, tuple(proposal for proposal in proposals if proposal.batch_id not in selected_ids)

    def _run_worker(self, task: RepairTask, batch: WorkingBatch, worker_id: str, manager: GitWorktreeManager, source_repo: Path, worktrees: dict[str, str], *, budget_started_at: float) -> AgentResult:
        handle = manager.create(task_id=f"{task.run_id}-{batch.batch_id}", source_repo=source_repo, base_commit=task.base_commit, run_id=task.run_id, role="worker")
        worktrees[batch.batch_id] = str(handle.path)
        try:
            workspace = WorkspaceState(handle.path, task.base_commit, protected_paths=self.config.protected_paths)
            skill_store = SkillStore(self.config.skill_root)
            isolated_worker_id = f"{worker_id}-{batch.batch_id}-{uuid.uuid4().hex[:8]}"
            episode_store = EpisodeStore(self.config.memory_root, worker_id=isolated_worker_id, task_id=task.task_id)
            cache = ContextCache() if self.config.context_cache_enabled else None
            executor = ToolExecutor(workspace, limits=self.config.tools, skill_store=skill_store, episode_store=episode_store, cache=cache)
            router = SkillRouter(skill_store)
            model = self.model_factory(task, isolated_worker_id)
            loop = AgentLoop(task, worker_id=isolated_worker_id, model=model, executor=executor, skill_router=router, chunking_enabled=self.config.chunking_enabled, retry_policy=RetryPolicy(self.config.model.max_retries), tool_limits=self.config.tools, max_recent_observations=self.config.max_recent_observations, max_observation_chars=self.config.max_observation_chars, max_skill_context_chars=self.config.max_skill_context_chars, max_skill_scan_bytes=self.config.max_skill_scan_bytes, trace_callback=lambda observation: self.store.record_trace(task.run_id, {"worker_id": isolated_worker_id, "batch_id": batch.batch_id, "observation": _observation_trace(observation)}), usage_callback=lambda usage: self.store.update_worker_budget(task.run_id, isolated_worker_id, to_primitive(usage)), budget_started_at=budget_started_at)
            result = loop.run(batch.batch_id, batch.issues)
            self.store.record_trace(task.run_id, {"worker_id": isolated_worker_id, "batch_id": batch.batch_id, "usage": to_primitive(result.usage), "review_required": result.review_required, "reason_present": bool(result.reason)})
            result_artifact = self.store.save_artifact(task.run_id, f"worker-result-{uuid.uuid4().hex}", "worker-result", canonical_json(_worker_result_artifact_payload(result)))
            self.store.save_checkpoint(task.run_id, f"checkpoint-worker-{uuid.uuid4().hex}", Stage.BATCH_REVIEW, [result_artifact["artifact_id"]], {"batch_id": batch.batch_id, "worker_id": isolated_worker_id, "proposal_present": result.proposal is not None, "review_required": result.review_required})
            manager.mark(handle, "FINISHED")
            manager.remove(handle)
            return result
        except Exception:
            manager.mark(handle, "RECOVERABLE")
            raise

    @staticmethod
    def _allocate_slot_budgets(total: Budget, used: Mapping[str, Any], slot: Sequence[WorkingBatch]) -> dict[str, Budget]:
        count = max(1, len(slot))
        remaining = {
            "max_model_calls": max(0, total.max_model_calls - int(used.get("model_attempts", 0))),
            "max_tool_calls": max(0, total.max_tool_calls - int(used.get("tool_calls", 0))),
            "max_tokens": max(0, total.max_tokens - int(used.get("tokens", 0))) if bool(used.get("token_usage_known", True)) else 0,
            "max_edit_attempts": max(0, total.max_edit_attempts - int(used.get("edit_attempts", 0))),
        }
        allocated: dict[str, Budget] = {}
        for index, batch in enumerate(slot):
            values = {"max_wall_seconds": total.max_wall_seconds, "max_chunk_actions": total.max_chunk_actions}
            for key, value in remaining.items():
                base, extra = divmod(value, count)
                values[key] = base + (1 if index < extra else 0)
            allocated[batch.batch_id] = Budget(**values)
        return allocated

    @_candidate_lifecycle_guard
    def approve(self, candidate_id: str, *, reviewer: str, reason: str, approved: bool) -> HumanApproval:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        self._assert_candidate_current(candidate)
        approval = HumanApproval(candidate_id, candidate.tree_hash, approved, reviewer, reason, git_tree_oid=candidate.git_tree_oid, candidate_commit=candidate.candidate_commit)
        self.store.save_approval(approval)
        return approval

    @_candidate_lifecycle_guard
    def submit(self, candidate_id: str, *, branch: str, change_id: str, candidate_commit: str) -> SubmissionResponse | CIRequest:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        approval = self.store.get_approval(candidate_id)
        if (approval is None or not approval.approved or approval.tree_hash != candidate.tree_hash
                or approval.git_tree_oid != candidate.git_tree_oid
                or approval.candidate_commit != candidate.candidate_commit):
            raise OrchestratorError("candidate requires approval bound to its current tree")
        self._assert_candidate_current(candidate)
        try:
            current_run = self.store.get_run(candidate.run_id) or {}
            workspace = current_run.get("integration_workspace")
            if not workspace:
                raise CandidateError("candidate integration workspace is not recorded")
            self.freezer.assert_commit_identity(candidate, Path(workspace), candidate_commit)
        except (CandidateError, OSError, KeyError, TypeError) as exc:
            current = self.store.get_run(candidate.run_id) or {}
            if current.get("stage") == Stage.HUMAN_REVIEW.value:
                self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"identity_error": str(exc)}, failure_class="IDENTITY_MISMATCH")
            raise OrchestratorError(f"IDENTITY_MISMATCH: submission commit is not the frozen candidate: {exc}") from exc
        current_run = self.store.get_run(candidate.run_id) or {}
        if current_run.get("stage") == Stage.SUBMISSION_UNKNOWN.value:
            raise OrchestratorError("submission response is unknown; reconcile the same Change-Id and fixed revision before retrying")
        task_payload = current_run.get("task", {})
        repo = str(task_payload.get("repo", candidate.run_id)) if isinstance(task_payload, Mapping) else candidate.run_id
        intent = SubmissionIntent(
            submission_id=f"submission-{uuid.uuid4().hex}", candidate_id=candidate_id,
            candidate_tree_hash=candidate.tree_hash, repo=repo, branch=branch,
            change_id=change_id, candidate_commit=candidate_commit,
            candidate_git_tree_oid=candidate.git_tree_oid,
        )
        try:
            self.store.claim_submission(intent)
        except StoreError as exc:
            raise OrchestratorError(str(exc)) from exc
        response = self.gerrit.submit(intent, candidate)
        if response.status == SubmissionStatus.SUBMISSION_UNKNOWN or not response.response_known:
            self.store.save_submission(replace(intent, status=SubmissionStatus.SUBMISSION_UNKNOWN))
            self.store.transition(candidate.run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"submission_response": to_primitive(response)})
            return response
        if response.status != SubmissionStatus.SUBMITTED:
            self.store.save_submission(replace(intent, status=response.status))
            self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"submission_response": to_primitive(response)}, failure_class=response.status.value)
            return response
        submitted_intent = replace(intent, status=SubmissionStatus.SUBMITTED, remote_change=response.remote_change, patch_set=response.patch_set)
        self.store.save_submission(submitted_intent)
        self.store.transition(candidate.run_id, Stage.SUBMITTING, payload_update={"remote_change": response.remote_change, "patch_set": response.patch_set})
        return self._dispatch_ci(candidate, submitted_intent, f"ci-{submitted_intent.submission_id}")

    def _dispatch_ci(self, candidate: Candidate, intent: SubmissionIntent, dispatch_id: str, *, retry_from_unknown: bool = False) -> CIRequest:
        try:
            self.store.begin_ci_dispatch(candidate.run_id, candidate.candidate_id, intent.submission_id, dispatch_id, retry_unknown=retry_from_unknown)
        except StoreError as exc:
            raise OrchestratorError(str(exc)) from exc
        try:
            request = self.ci.submit(candidate, intent, idempotency_key=dispatch_id)
        except Exception as exc:
            self.store.finish_ci_dispatch(candidate.run_id, dispatch_id, accepted=None, backend="unknown")
            raise OrchestratorError(f"CI dispatch outcome is unknown; resume/retry with idempotency key {dispatch_id} only") from exc
        if request.accepted and request.run_id:
            self.store.finish_ci_dispatch(candidate.run_id, dispatch_id, accepted=True, ci_run_id=request.run_id, backend=request.backend)
        elif request.accepted:
            self.store.finish_ci_dispatch(candidate.run_id, dispatch_id, accepted=None, backend=request.backend, missing_contract=request.missing_contract)
        else:
            self.store.finish_ci_dispatch(candidate.run_id, dispatch_id, accepted=False, backend=request.backend, missing_contract=request.missing_contract)
        return replace(request, dispatch_id=dispatch_id)

    @_candidate_lifecycle_guard
    def retry_ci_dispatch(self, candidate_id: str) -> CIRequest:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        run = self.store.get_run(candidate.run_id) or {}
        dispatch_id = run.get("ci_dispatch_id")
        submission_id = run.get("submission_id")
        if not dispatch_id or not submission_id:
            raise OrchestratorError("CI dispatch intent is missing; cannot safely retry")
        if run.get("stage") == Stage.CI_DISPATCH_UNKNOWN.value:
            retry_unknown = True
        elif run.get("stage") == Stage.RETRY_INFRA.value and run.get("ci_dispatch_state") == "NOT_ACCEPTED":
            retry_unknown = True
        else:
            raise OrchestratorError("CI dispatch may only be retried after an unknown or confirmed-not-accepted outcome")
        intent = self.store.get_submission(submission_id)
        if intent is None:
            raise OrchestratorError("recorded Gerrit submission is missing; cannot retry CI dispatch")
        return self._dispatch_ci(candidate, intent, dispatch_id, retry_from_unknown=retry_unknown)

    @_candidate_lifecycle_guard
    def reconcile_submission(self, candidate_id: str, intent: SubmissionIntent | None = None, *, submission_id: str | None = None) -> SubmissionResponse | CIRequest:
        if intent is None:
            if not submission_id:
                raise OrchestratorError("submission_id is required to reconcile a recorded intent")
            intent = self.store.get_submission(submission_id)
            if intent is None:
                raise OrchestratorError(f"submission intent not found: {submission_id}")
        candidate = self.store.get_candidate(candidate_id)
        current_run = self.store.get_run(candidate.run_id) if candidate else None
        persisted_intent = self.store.get_submission(intent.submission_id)
        immutable_intent = lambda item: (
            item.submission_id, item.candidate_id, item.candidate_tree_hash,
            item.candidate_git_tree_oid, item.candidate_commit, item.repo,
            item.branch, item.change_id,
        )
        if (candidate is None or candidate.tree_hash != intent.candidate_tree_hash
                or candidate.git_tree_oid != intent.candidate_git_tree_oid
                or candidate.candidate_commit != intent.candidate_commit
                or current_run is None
                or current_run.get("submission_id") != intent.submission_id
                or persisted_intent is None
                or immutable_intent(persisted_intent) != immutable_intent(intent)):
            raise OrchestratorError("submission reconciliation identity mismatch")
        intent = persisted_intent
        current_run = self.store.get_run(candidate.run_id) or {}
        if current_run.get("stage") in {Stage.CI_DISPATCHING.value, Stage.CI_DISPATCH_UNKNOWN.value} or current_run.get("ci_dispatch_state") in {"IN_FLIGHT", "UNKNOWN", "NOT_ACCEPTED"}:
            raise OrchestratorError("Gerrit is already reconciled; use retry_ci_dispatch so CI reuses the recorded idempotency key")
        if current_run.get("stage") == Stage.CI_PENDING.value and current_run.get("ci_run_id"):
            return self.gerrit.query(intent)
        if current_run.get("stage") not in {Stage.SUBMITTING.value, Stage.SUBMISSION_UNKNOWN.value}:
            raise OrchestratorError(f"submission reconciliation is not valid from stage {current_run.get('stage')}")
        response = self.gerrit.query(intent)
        if response.status == SubmissionStatus.SUBMITTED and response.response_known:
            submitted_intent = replace(intent, status=SubmissionStatus.SUBMITTED, remote_change=response.remote_change, patch_set=response.patch_set)
            self.store.save_submission(submitted_intent)
            self.store.transition(candidate.run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"remote_change": response.remote_change, "patch_set": response.patch_set, "candidate_commit": candidate.candidate_commit, "reconciled": True})
            return self._dispatch_ci(candidate, submitted_intent, f"ci-{submitted_intent.submission_id}")
        elif not response.response_known:
            self.store.transition(candidate.run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"reconcile_response": to_primitive(response)})
        elif response.safe_to_retry:
            self.store.save_submission(replace(intent, status=SubmissionStatus.RECONCILED))
            if current_run.get("stage") == Stage.SUBMITTING.value:
                self.store.transition(candidate.run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"reconciled_absent": True, "reconcile_response": to_primitive(response)})
            self.store.transition(candidate.run_id, Stage.HUMAN_REVIEW, payload_update={"reconciled_absent": True, "reconcile_response": to_primitive(response)})
        return response

    @_candidate_lifecycle_guard
    def receive_ci(self, candidate_id: str, *, ci_run_id: str, revision: str, actual_tested_commit: str | None, config_id: str, backend: str, checks: Mapping[str, str], dispatch_id: str | None = None, evidence: Mapping[str, Any] | None = None, final: bool = False) -> ValidationResult:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        run = self.store.get_run(candidate.run_id) or {}
        current_stage = run.get("stage")
        expected_ci_run = run.get("ci_run_id")
        expected_dispatch = run.get("ci_dispatch_id")
        if not ci_run_id.strip() or not revision.strip() or not config_id.strip() or not backend.strip():
            raise OrchestratorError("CI callback requires run id, revision, config id, and backend")
        uncertain_submission = current_stage in {Stage.SUBMITTING.value, Stage.SUBMISSION_UNKNOWN.value}
        uncertain_dispatch = current_stage in {Stage.CI_DISPATCHING.value, Stage.CI_DISPATCH_UNKNOWN.value}
        if uncertain_submission:
            raise OrchestratorError("CI callback cannot bypass Gerrit submission reconciliation")
        if uncertain_dispatch and (not expected_dispatch or dispatch_id != expected_dispatch):
            raise OrchestratorError("CI callback requires the exact persisted dispatch id for an unknown dispatch")
        if expected_dispatch and dispatch_id != expected_dispatch:
            raise OrchestratorError("CI callback dispatch identity does not match the current candidate dispatch")
        if not expected_dispatch and not expected_ci_run:
            raise OrchestratorError("CI callback has no persisted dispatch/run identity to bind against")
        stale_callback = bool(expected_ci_run and expected_ci_run != ci_run_id)
        merged, accumulator_state, duplicate_event, identity_conflict, check_conflict, resolved_commit = self.store.merge_ci_checks(
            candidate_id=candidate_id, ci_run_id=ci_run_id, revision=revision,
            actual_tested_commit=actual_tested_commit, checks=checks,
            required_checks=self.validator.policy.required_checks, final=final,
            config_id=config_id, backend=backend, evidence=evidence,
        )
        callback_evidence = dict(evidence or {})
        callback_evidence.update({"accumulator_state": accumulator_state, "duplicate_callback": duplicate_event})
        if stale_callback:
            callback_evidence.update({"ignored_current_stage": True, "reason": "callback belongs to a different CI run"})
        if identity_conflict:
            callback_evidence["identity_conflict"] = True
        if check_conflict:
            callback_evidence["callback_conflict"] = True
        self.store.record_trace(candidate.run_id, {"kind": "ci_callback", "candidate_id": candidate_id, "dispatch_id": dispatch_id, "ci_run_id": ci_run_id, "revision": revision, "actual_tested_commit": actual_tested_commit, "checks": checks, "state": accumulator_state})
        if current_stage in {Stage.CI_DISPATCHING.value, Stage.CI_DISPATCH_UNKNOWN.value} and not stale_callback:
            self.store.transition(candidate.run_id, Stage.CI_PENDING, payload_update={"ci_run_id": ci_run_id, "ci_dispatch_state": "ACCEPTED", "callback_recovered_submission": True})
            current_stage = Stage.CI_PENDING.value
        if accumulator_state != "FINAL":
            result = self.validator.classify(candidate=candidate, revision=revision, actual_tested_commit=resolved_commit, ci_run_id=ci_run_id, config_id=config_id, backend=backend, checks=merged, evidence=callback_evidence)
            return replace(result, state=ValidationState.PARTIAL)
        result = self.validator.classify(candidate=candidate, revision=revision, actual_tested_commit=resolved_commit, ci_run_id=ci_run_id, config_id=config_id, backend=backend, checks=merged, evidence=callback_evidence)
        if identity_conflict or check_conflict or stale_callback:
            result = replace(result, classification=ValidationClass.INCONCLUSIVE)
        if stale_callback:
            return self.store.save_validation(result)
        current_run = self.store.get_run(candidate.run_id) or {}
        can_update_stage = current_stage in {Stage.CI_PENDING.value, Stage.RETRY_INFRA.value, Stage.VERIFIED.value} or (
            current_stage == Stage.REVIEW_REQUIRED.value and current_run.get("failure_class") == "INCONCLUSIVE"
        )
        if not can_update_stage:
            return self.store.save_validation(result)
        validation_payload = {"callback_conflict": bool(identity_conflict or check_conflict), "validation_revision": revision}
        if result.classification == ValidationClass.CODE_FAIL:
            validation_payload["new_attempt_id"] = f"attempt-{uuid.uuid4().hex}"
        result, target_stage = self.store.save_validation_and_transition(result, run_id=candidate.run_id, payload_update=validation_payload)
        if target_stage == Stage.VERIFIED:
            self._cleanup_run_worktrees(candidate.run_id)
        return result

    @_run_lifecycle_guard
    def resume(self, run_id: str) -> dict[str, Any]:
        issues = self.store.reconcile(run_id)
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError(f"run not found: {run_id}")
        candidates = self.store.list_candidates(run_id)
        if not issues and candidates and run["stage"] == Stage.INTEGRATING.value:
            candidate = next((item for item in candidates if item.candidate_id == run.get("candidate_id")), candidates[-1])
            self._restore_candidate_workspace(candidate, run)
            self.store.transition(run_id, Stage.CANDIDATE_FROZEN, payload_update={"candidate_id": candidate.candidate_id, "tree_hash": candidate.tree_hash, "recovered": True})
            self.store.transition(run_id, Stage.HUMAN_REVIEW)
            run = self.store.get_run(run_id) or run
        elif run["stage"] == Stage.SUBMITTING.value:
            self.store.transition(run_id, Stage.SUBMISSION_UNKNOWN, payload_update={"recovered": True, "recovery_action": "reconcile before retry"})
            run = self.store.get_run(run_id) or run
        elif run["stage"] == Stage.CI_DISPATCHING.value:
            self.store.transition(run_id, Stage.CI_DISPATCH_UNKNOWN, payload_update={"recovered": True, "recovery_action": "retry with the same idempotency key only"})
            run = self.store.get_run(run_id) or run
        elif not issues and run["stage"] == Stage.REPAIRING.value:
            self.store.transition(
                run_id,
                Stage.REVIEW_REQUIRED,
                payload_update={"recovered": True, "recovery_action": "replan_required"},
                failure_class=run.get("failure_class") or "REPLAN_REQUIRED",
            )
            run = self.store.get_run(run_id) or run
        elif not issues and candidates and run["stage"] in {Stage.CANDIDATE_FROZEN.value, Stage.HUMAN_REVIEW.value, Stage.SUBMISSION_UNKNOWN.value, Stage.CI_DISPATCH_UNKNOWN.value, Stage.CI_PENDING.value, Stage.RETRY_INFRA.value}:
            candidate = next((item for item in candidates if item.candidate_id == run.get("candidate_id")), candidates[-1])
            self._restore_candidate_workspace(candidate, run)
            run = self.store.get_run(run_id) or run
        recoverable_validation_stage = run["stage"] == Stage.CI_PENDING.value or (
            run.get("validation_backend") != "local"
            and (
                run["stage"] in {Stage.RETRY_INFRA.value, Stage.VERIFIED.value}
                or (
                    run["stage"] == Stage.REVIEW_REQUIRED.value and run.get("failure_class") == "INCONCLUSIVE"
                )
            )
        )
        if not issues and recoverable_validation_stage:
            expected_ci_run = run.get("ci_run_id")
            candidate = next((item for item in candidates if item.candidate_id == run.get("candidate_id")), None)
            recorded = [item for item in self.store.list_validations(candidate.candidate_id) if item.ci_run_id == expected_ci_run and item.state == ValidationState.FINAL] if candidate and expected_ci_run else []
            accumulators = [item for item in self.store.list_ci_accumulators(candidate.candidate_id) if item["ci_run_id"] == expected_ci_run and item["state"] == "FINAL"] if candidate and expected_ci_run else []
            recovered_from_accumulator = bool(accumulators)
            if accumulators and candidate is not None:
                accumulator = max(accumulators, key=lambda item: item["updated_at"])
                matching_validation = [item for item in recorded if item.revision == accumulator["revision"]]
                if matching_validation:
                    validation = max(matching_validation, key=lambda item: item.created_at)
                else:
                    validation = self.validator.classify(
                        candidate=candidate,
                        revision=accumulator["revision"],
                        actual_tested_commit=accumulator["actual_tested_commit"],
                        ci_run_id=accumulator["ci_run_id"],
                        config_id=accumulator["config_id"] or "unknown",
                        backend=accumulator["backend"] or "unknown",
                        checks=accumulator["checks"],
                        evidence={**accumulator["evidence"], "recovered_from_ci_accumulator": True, "identity_conflict": accumulator["identity_conflict"]},
                    )
                    if accumulator["identity_conflict"] or any(str(value).upper() == "CONFLICT" for value in accumulator["checks"].values()):
                        validation = replace(validation, classification=ValidationClass.INCONCLUSIVE, evidence={**validation.evidence, "callback_conflict": True})
            elif recorded:
                validation = max(recorded, key=lambda item: item.created_at)
            else:
                validation = None
            if validation is not None:
                _, target_stage = self.store.save_validation_and_transition(validation, run_id=run_id, payload_update={"recovered_validation_transition": True}, require_accumulator=recovered_from_accumulator)
                if target_stage == Stage.VERIFIED:
                    self._cleanup_run_worktrees(run_id)
                run = self.store.get_run(run_id) or run
        if run["stage"] in {Stage.SUBMITTING.value, Stage.SUBMISSION_UNKNOWN.value}:
            action = "reconcile_external_side_effect"
        elif run["stage"] in {Stage.CI_DISPATCHING.value, Stage.CI_DISPATCH_UNKNOWN.value} or (run["stage"] == Stage.RETRY_INFRA.value and run.get("ci_dispatch_state") == "NOT_ACCEPTED"):
            action = "retry_ci_dispatch_with_same_idempotency_key"
        elif issues or run["stage"] == Stage.FAILED.value:
            action = "replan_required"
        elif run["stage"] == Stage.REVIEW_REQUIRED.value and run.get("failure_class") == "INCONCLUSIVE":
            action = "await_ci_evidence_or_human_review"
        elif run["stage"] == Stage.REVIEW_REQUIRED.value:
            action = "replan_required"
        elif run["stage"] in {Stage.REPAIRING.value, Stage.BATCH_REVIEW.value, Stage.INTEGRATING.value}:
            action = "replan_required"
        elif run["stage"] in {Stage.CANDIDATE_FROZEN.value, Stage.HUMAN_REVIEW.value}:
            action = "await_human_approval"
        elif run["stage"] == Stage.CI_PENDING.value:
            action = "await_ci_callback"
        elif run["stage"] == Stage.VERIFIED.value:
            action = "complete"
        elif run["stage"] == Stage.RETRY_INFRA.value:
            action = "await_infrastructure_recovery_or_human_review"
        elif run["stage"] in {Stage.SUBMITTING.value, Stage.CI_DISPATCHING.value}:
            action = "reconcile_external_side_effect"
        else:
            action = "manual_recovery_required"
        budget = run.get("task", {}).get("budget", {}) if isinstance(run.get("task"), Mapping) else {}
        used = run.get("budget_used", {})
        remaining = {
            "max_model_calls": max(0, int(budget.get("max_model_calls", self.config.budget.max_model_calls)) - int(used.get("model_attempts", 0))),
            "max_tool_calls": max(0, int(budget.get("max_tool_calls", self.config.budget.max_tool_calls)) - int(used.get("tool_calls", 0))),
            "max_tokens": max(0, int(budget.get("max_tokens", self.config.budget.max_tokens)) - int(used.get("tokens", 0))) if bool(used.get("token_usage_known", True)) else 0,
            "token_usage_known": bool(used.get("token_usage_known", True)),
            "max_edit_attempts": max(0, int(budget.get("max_edit_attempts", self.config.budget.max_edit_attempts)) - int(used.get("edit_attempts", 0))),
            "max_wall_seconds": max(0.0, float(budget.get("max_wall_seconds", self.config.budget.max_wall_seconds)) - float(used.get("wall_seconds", 0.0))),
        }
        return {"run": run, "recovery_issues": issues, "action": action, "budget_remaining": remaining, "submission_intents": [to_primitive(item) for candidate in candidates for item in self.store.list_submissions(candidate.candidate_id)]}

    def gc(self, *, older_than_hours: float = 168.0, apply: bool = False, include_pending_review: bool = False) -> dict[str, Any]:
        if older_than_hours < 1:
            raise OrchestratorError("GC age threshold must be at least one hour")
        manager = GitWorktreeManager(self.config.workspace_parent)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        worktrees: list[dict[str, str]] = []
        preserved: list[dict[str, str]] = []
        terminal = {Stage.VERIFIED.value}
        pending = {Stage.HUMAN_REVIEW.value, Stage.CANDIDATE_FROZEN.value}
        for entry in manager.entries():
            if entry.get("state") == "CLEANED":
                continue
            try:
                updated = datetime.fromisoformat(entry["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                preserved.append({"task_id": entry.get("task_id", "unknown"), "reason": "invalid lifecycle timestamp"})
                continue
            old_enough = updated <= cutoff
            run_id = entry.get("run_id", "")
            run = self.store.get_run(run_id) if run_id else None
            stage = run.get("stage") if run else None
            eligible_stage = stage in terminal or (include_pending_review and stage in pending)
            if old_enough and eligible_stage:
                if apply and run_id:
                    with self.store.lifecycle_guard(run_id):
                        current = self.store.get_run(run_id)
                        current_stage = current.get("stage") if current else None
                        still_eligible = current_stage in terminal or (include_pending_review and current_stage in pending)
                        if still_eligible:
                            try:
                                latest = next((item for item in manager.entries() if item.get("task_id") == entry["task_id"]), None)
                                latest_updated = datetime.fromisoformat(latest["updated_at"]) if latest else None
                                if latest_updated is not None and latest_updated.tzinfo is None:
                                    latest_updated = latest_updated.replace(tzinfo=timezone.utc)
                                if latest is None or latest_updated is None or latest_updated > cutoff:
                                    raise WorkspaceError("worktree was refreshed before deletion")
                                handle = manager.existing(task_id=latest["task_id"], base_commit=latest["base_commit"])
                                manager.remove(handle, expected_updated_at=latest["updated_at"])
                            except WorkspaceError:
                                preserved.append({"task_id": entry["task_id"], "reason": "worktree changed before deletion"})
                            except (KeyError, ValueError):
                                preserved.append({"task_id": entry["task_id"], "reason": "worktree lifecycle timestamp is invalid"})
                            else:
                                worktrees.append({**latest, "action": "removed"})
                        else:
                            preserved.append({"task_id": entry["task_id"], "reason": "run lifecycle changed before deletion"})
                else:
                    worktrees.append({**entry, "action": "would_remove"})
            else:
                preserved.append({"task_id": entry.get("task_id", "unknown"), "reason": "too recent, live review/CI stage, or missing run state"})
        age_seconds = older_than_hours * 3600
        temporary_files = (
            *cleanup_stale_temp_files((self.config.workspace_parent,), older_than_seconds=age_seconds, apply=apply, kind="worktree", recursive=False),
            *cleanup_stale_temp_files((self.config.artifact_root,), older_than_seconds=age_seconds, apply=apply, kind="artifact"),
            *cleanup_stale_temp_files((self.config.memory_root,), older_than_seconds=age_seconds, apply=apply, kind="memory"),
        )
        return {"mode": "applied" if apply else "dry-run", "worktrees": worktrees, "temporary_files": temporary_files, "preserved": preserved}

    @_candidate_lifecycle_guard
    def validate_local(self, candidate_id: str, *, commit: str, config_id: str, workspace: str | Path, commands: Mapping[str, Sequence[str]]) -> ValidationResult:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise OrchestratorError(f"candidate not found: {candidate_id}")
        self._assert_candidate_current(candidate)
        specs = {name: CommandSpec(tuple(argv), self.config.tools.command_timeout_seconds) for name, argv in commands.items()}
        result = self.local_validator.run(candidate=candidate, workspace=Path(workspace), commit=commit, config_id=config_id, commands=specs)
        validation_payload = {}
        if result.classification == ValidationClass.CODE_FAIL:
            validation_payload["new_attempt_id"] = f"attempt-{uuid.uuid4().hex}"
        result, _ = self.store.save_local_validation_and_transition(
            result,
            run_id=candidate.run_id,
            payload_update=validation_payload,
        )
        return result

    def _assert_candidate_current(self, candidate: Candidate) -> None:
        run = self.store.get_run(candidate.run_id) or {}
        workspace = run.get("integration_workspace")
        if not workspace:
            raise OrchestratorError("candidate integration workspace is not recorded")
        try:
            self.freezer.assert_current(candidate, Path(workspace))
            self.freezer.assert_commit_identity(candidate, Path(workspace))
        except (CandidateError, OSError, WorkspaceError) as exc:
            raise OrchestratorError(f"candidate invalidated by workspace change: {exc}") from exc

    def _restore_candidate_workspace(self, candidate: Candidate, run: Mapping[str, Any]) -> None:
        manager = GitWorktreeManager(self.config.workspace_parent)
        try:
            repo = self._resolved_run_source_repo(candidate, run, manager)
        except (OrchestratorError, WorkspaceError) as exc:
            if Stage(run["stage"]) != Stage.REVIEW_REQUIRED:
                self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"source_identity_error": str(exc)}, failure_class="SOURCE_IDENTITY_MISMATCH")
            raise OrchestratorError(f"candidate source repository identity could not be restored: {exc}") from exc
        configured_path = run.get("integration_workspace")
        if configured_path and Path(configured_path).is_dir():
            registered = next((entry for entry in manager.entries() if entry.get("run_id") == candidate.run_id and Path(entry.get("path", "")).resolve() == Path(configured_path).resolve()), None)
            if registered is None or manager.resolve_source_repo(registered["source_repo"]) != repo:
                if Stage(run["stage"]) != Stage.REVIEW_REQUIRED:
                    self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"source_identity_error": "integration workspace is not registered to the resolved source repository"}, failure_class="SOURCE_IDENTITY_MISMATCH")
                raise OrchestratorError("integration workspace is not registered to the resolved source repository")
            try:
                self.freezer.assert_current(candidate, Path(configured_path))
                self.freezer.assert_commit_identity(candidate, Path(configured_path))
            except (CandidateError, WorkspaceError) as exc:
                if Stage(run["stage"]) != Stage.REVIEW_REQUIRED:
                    self.store.transition(candidate.run_id, Stage.REVIEW_REQUIRED, payload_update={"identity_error": str(exc)}, failure_class="IDENTITY_MISMATCH")
                raise OrchestratorError(f"candidate workspace failed recovery identity checks: {exc}") from exc
            return
        handle = manager.create(task_id=f"{candidate.run_id}-integration", source_repo=repo, base_commit=candidate.candidate_commit, run_id=candidate.run_id, role="integration")
        try:
            self.freezer.assert_current(candidate, handle.path)
            self.freezer.assert_commit_identity(candidate, handle.path)
        except (CandidateError, WorkspaceError, OSError) as exc:
            manager.remove(handle)
            raise OrchestratorError(f"candidate commit could not restore its frozen workspace: {exc}") from exc
        manager.mark(handle, "RECOVERABLE")
        self.store.transition(candidate.run_id, Stage(run["stage"]), payload_update={"integration_workspace": str(handle.path), "restored_from_candidate_commit": True})
        self.store.record_trace(candidate.run_id, {"kind": "workspace_recovered", "candidate_id": candidate.candidate_id, "candidate_commit": candidate.candidate_commit})

    def _resolved_run_source_repo(self, candidate: Candidate, run: Mapping[str, Any], manager: GitWorktreeManager) -> Path:
        persisted = run.get("resolved_source_repo")
        if persisted:
            repo = Path(str(persisted)).expanduser().resolve()
            canonical = manager.resolve_source_repo(repo)
            if canonical != repo:
                raise OrchestratorError("persisted source repository is not a canonical Git root")
        else:
            # Older runs did not persist this identity. Recover only from this run's worktree registry.
            registered_paths = {
                manager.resolve_source_repo(entry["source_repo"])
                for entry in manager.entries()
                if entry.get("run_id") == candidate.run_id and entry.get("source_repo")
            }
            if len(registered_paths) != 1:
                raise OrchestratorError("legacy run has no unique persisted source repository; refusing to use current configuration")
            repo = next(iter(registered_paths))
        base_commit = str(run.get("resolved_base_commit") or candidate.base_commit)
        if manager.resolve_commit(repo, base_commit) != candidate.base_commit:
            raise OrchestratorError("resolved source repository/base commit does not match candidate identity")
        return repo

    def _cleanup_run_worktrees(self, run_id: str) -> None:
        manager = GitWorktreeManager(self.config.workspace_parent)
        for entry in manager.entries():
            if entry.get("run_id") != run_id or entry.get("state") == "CLEANED":
                continue
            try:
                handle = manager.existing(task_id=entry["task_id"], base_commit=entry["base_commit"])
                manager.mark(handle, "FINISHED")
                manager.remove(handle)
            except (KeyError, WorkspaceError) as exc:
                self.store.record_trace(run_id, {"kind": "worktree_cleanup_error", "task_id": entry.get("task_id"), "error": str(exc)})

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
        pending_validation = tuple(item for candidate in candidates for item in self.store.list_ci_accumulators(candidate.candidate_id) if item["state"] != "FINAL")
        approved_suppressions = []
        human_approvals = []
        for candidate in candidates:
            approval = self.store.get_approval(candidate.candidate_id)
            approval_current = bool(approval and approval.tree_hash == candidate.tree_hash and approval.git_tree_oid == candidate.git_tree_oid and approval.candidate_commit == candidate.candidate_commit)
            human_approvals.append({"candidate_id": candidate.candidate_id, "status": "APPROVED" if approval_current and approval.approved else "REJECTED" if approval_current else "STALE" if approval else "AWAITING_APPROVAL", "reviewer": approval.reviewer if approval else None, "reason": approval.reason if approval else None})
            if candidate.suppression_candidate_ids and approval_current and approval is not None and approval.approved:
                approved_suppressions.append({"candidate_id": candidate.candidate_id, "finding_ids": list(candidate.suppression_candidate_ids)})
        checks_not_run = []
        required_checks = self.validator.policy.required_checks
        for result in validations:
            missing = sorted(check for check in required_checks if str(result.checks.get(check, "MISSING")).upper() in {"MISSING", "UNKNOWN", "QUEUED", "PENDING", "RUNNING", "IN_PROGRESS", "PARTIAL"})
            if missing:
                checks_not_run.append({"candidate_id": result.candidate_id, "ci_run_id": result.ci_run_id, "checks": missing})
        for item in pending_validation:
            missing = sorted(check for check in required_checks if str(item["checks"].get(check, "MISSING")).upper() in {"MISSING", "UNKNOWN", "QUEUED", "PENDING", "RUNNING", "IN_PROGRESS", "PARTIAL"})
            if missing:
                checks_not_run.append({"candidate_id": item["candidate_id"], "ci_run_id": item["ci_run_id"], "checks": missing})
        payload = {
            "run_id": run_id,
            "stage": run.get("stage"),
            "candidate_id": run.get("candidate_id"),
            "budget_used": run.get("budget_used", {}),
            "candidate_patches": [{**to_primitive(candidate), "status": "CANDIDATE_PATCH_NOT_VERIFIED"} for candidate in candidates],
            "human_approvals": human_approvals,
            "validation_passed": [to_primitive(item) for item in validations if item.classification == ValidationClass.VALIDATION_PASS],
            "validation_code_fail": [to_primitive(item) for item in validations if item.classification == ValidationClass.CODE_FAIL],
            "validation_infrastructure": [to_primitive(item) for item in validations if item.classification == ValidationClass.INFRA_FAIL],
            "validation_inconclusive": [to_primitive(item) for item in validations if item.classification == ValidationClass.INCONCLUSIVE],
            "validation_pending": list(pending_validation),
            "checks_not_run": checks_not_run,
            "approved_suppressions": approved_suppressions,
            "unresolved": run.get("review_reasons", run.get("worker_review_reasons", run.get("integration_conflicts", []))),
            "not_executed": run.get("not_executed", []),
            "infrastructure": run.get("failure_class") or run.get("submission_response") or run.get("ci_request"),
            "ci_dispatch": {key: run[key] for key in ("ci_dispatch_id", "ci_dispatch_state", "ci_dispatch_backend", "ci_dispatch_missing_contract", "ci_run_id") if key in run},
            "semantic_warnings": run.get("semantic_warnings", []),
        }
        output_dir = Path(self.config.artifact_root) / run_id / "report"
        markdown, machine = self.report_writer.write(output_dir, payload)
        self.store.transition(run_id, Stage(run.get("stage", Stage.REVIEW_REQUIRED.value)), payload_update={"report_path": str(markdown), "report_json_path": str(machine)})
        return str(markdown), str(machine)
