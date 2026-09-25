"""Single Repair Agent loop with bounded budgets and optional read-only chunks."""

from __future__ import annotations

import time
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .domain import (
    ActionKind,
    BatchProposal,
    Budget,
    Finding,
    Issue,
    Observation,
    RepairTask,
    RiskClass,
    ToolStatus,
    canonical_json,
    issue_id,
    sha256_text,
    to_primitive,
)
from .config import ToolLimits
from .memory import BatchCache, TaskStateMemory
from .models import ModelAdapter, ModelDeadlineExceeded, ModelDecision, ModelError, ModelProtocolError, ToolCall
from .skills import SkillContextError, SkillRouter
from .retry import RetryPolicy
from .tools.chunking import ActionChunk, ChunkAction, ChunkExecutor
from .tools.executor import ToolExecutor
from .validation.scope import PatchScopeGuard


TraceCallback = Callable[[Observation], None]
UsageCallback = Callable[["AgentUsage"], None]


def _safe_review_reason(reason: str) -> str:
    """Keep durable review reasons useful without copying arbitrary response/error text."""
    budgets = {
        "model call budget exhausted", "tool call budget exhausted", "token budget exhausted",
        "model token usage unavailable; stopped to preserve budget",
        "edit attempt budget exhausted", "wall-clock budget exhausted",
        "tool call budget exhausted during action chunk", "wall-clock budget exhausted during action chunk",
        "model request attempt budget exhausted",
        "skill context limit exceeded",
        "skill context deadline exceeded",
    }
    parts = [item.strip() for item in reason.split(";")]
    if parts and all(item in budgets for item in parts):
        return "; ".join(parts)
    if reason.startswith("tool observation incomplete: "):
        detail = reason.removeprefix("tool observation incomplete: ")
        if all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}/[A-Z_]{1,32}", item.strip()) for item in detail.split(",")):
            return reason
        return "tool observation incomplete; human review required"
    if reason.startswith("MODEL_HTTP:"):
        return "MODEL_HTTP: model request failed"
    if reason.startswith("MODEL_TRANSPORT:"):
        return "MODEL_TRANSPORT: model request failed"
    if reason.startswith("NOT_CONFIGURED:"):
        return "NOT_CONFIGURED: model provider is unavailable"
    if reason.startswith("unexpected model failure:"):
        error_type = reason.partition(":")[2].strip()
        if error_type.isidentifier():
            return f"unexpected model failure: {error_type}"
    if reason.startswith("patch scope requires review"):
        return "patch scope requires review"
    if reason.startswith("invalid action chunk"):
        return "invalid action chunk"
    if reason in {
        "invalid model protocol", "model requested human review", "unsupported model decision",
        "action chunk incomplete", "action chunk rejected because chunking is disabled",
        "worker requires review", "could not obtain a complete real Git diff",
    }:
        return reason
    return "worker requires review; inspect outcome metadata"


@dataclass
class AgentUsage:
    model_calls: int = 0
    model_attempts: int = 0
    model_retries: int = 0
    tool_calls: int = 0
    tokens: int = 0
    token_usage_known: bool = True
    edit_attempts: int = 0
    chunk_actions: int = 0
    changed_files: int = 0
    diff_lines: int = 0
    elapsed_seconds: float = 0.0
    started_at: float = 0.0
    retry_events: list[dict[str, Any]] = field(default_factory=list)

    def within(self, budget: Budget) -> bool:
        return (
            self.model_calls < budget.max_model_calls
            and self.model_attempts < budget.max_model_calls
            and self.tool_calls < budget.max_tool_calls
            and self.token_usage_known
            and self.tokens < budget.max_tokens
            and self.edit_attempts < budget.max_edit_attempts
            and time.monotonic() - self.started_at < budget.max_wall_seconds
        )


@dataclass(frozen=True)
class AgentResult:
    batch_id: str
    worker_id: str
    proposal: BatchProposal | None
    review_required: bool
    reason: str | None
    usage: AgentUsage
    observations: tuple[Observation, ...]


class AgentLoop:
    def __init__(
        self,
        task: RepairTask,
        *,
        worker_id: str,
        model: ModelAdapter,
        executor: ToolExecutor,
        skill_router: SkillRouter | None = None,
        chunking_enabled: bool = False,
        retry_policy: RetryPolicy | None = None,
        tool_limits: ToolLimits | None = None,
        max_recent_observations: int = 10,
        max_observation_chars: int = 4_000,
        max_skill_context_chars: int = 12_000,
        max_skill_scan_bytes: int = 512_000,
        trace_callback: TraceCallback | None = None,
        usage_callback: UsageCallback | None = None,
        budget_started_at: float | None = None,
    ) -> None:
        self.task = task
        self.worker_id = worker_id
        self.model = model
        self.executor = executor
        self.skill_router = skill_router
        self.chunking_enabled = chunking_enabled
        self.retry_policy = retry_policy or RetryPolicy(0)
        self.tool_limits = tool_limits or ToolLimits()
        self.max_recent_observations = max_recent_observations
        self.max_observation_chars = max_observation_chars
        self.max_skill_context_chars = max_skill_context_chars
        self.max_skill_scan_bytes = max_skill_scan_bytes
        self.scope_guard = PatchScopeGuard(self.tool_limits)
        self.proposal_error: str | None = None
        self._blocking_tool_failures: list[tuple[str, str]] = []
        self.trace_callback = trace_callback
        self.usage_callback = usage_callback
        self.budget_started_at = budget_started_at
        self.chunk_executor = ChunkExecutor()
        self.cache = BatchCache()

    def run(self, batch_id: str, issues: Sequence[Issue]) -> AgentResult:
        self._blocking_tool_failures = []
        usage = AgentUsage(started_at=self.budget_started_at or time.monotonic())
        self._persist_usage(usage)
        memory = TaskStateMemory(task_id=self.task.task_id, worker_id=self.worker_id)
        observations: list[Observation] = []
        if len({issue_id(issue) for issue in issues}) != len(issues):
            return self._review(batch_id, usage, observations, "worker requires review")
        try:
            skill_injection = self._skills(issues, deadline=usage.started_at + self.task.budget.max_wall_seconds)
        except SkillContextError as exc:
            reason = "skill context deadline exceeded" if "deadline" in str(exc) else "skill context limit exceeded"
            return self._review(batch_id, usage, observations, reason)
        state: dict[str, Any] = {
            "worker_id": self.worker_id,
            "batch_id": batch_id,
            "workspace_revision": self.executor.workspace.revision,
            "skill_injection": skill_injection,
            "issue_ids": [issue_id(issue) for issue in issues],
        }

        while usage.within(self.task.budget):
            usage.model_calls += 1
            self._persist_usage(usage)
            try:
                decision = self.retry_policy.run(
                    lambda: self.model.decide_with_deadline(
                        self.task, state, self._prompt_observations(observations), self.executor.registry.schemas(),
                        deadline=usage.started_at + self.task.budget.max_wall_seconds,
                        token_limit=self.task.budget.max_tokens - usage.tokens,
                    ),
                    on_attempt=lambda: self._record_attempt(usage),
                    on_retry=lambda error, delay: self._record_retry(usage, error, delay),
                    before_attempt=lambda: "wall-clock budget exhausted before model attempt" if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds else None,
                    can_wait=lambda delay: (
                        usage.model_attempts < self.task.budget.max_model_calls
                        and time.monotonic() - usage.started_at + delay < self.task.budget.max_wall_seconds
                    ),
                )
                if not decision.usage.reported:
                    usage.token_usage_known = False
                    self._persist_usage(usage)
                    return self._review(batch_id, usage, observations, "model token usage unavailable; stopped to preserve budget")
                usage.tokens += decision.usage.total_tokens
                usage.token_usage_known = True
                self._persist_usage(usage)
            except ModelDeadlineExceeded:
                usage.token_usage_known = False
                self._persist_usage(usage)
                return self._review(batch_id, usage, observations, "wall-clock budget exhausted")
            except ModelProtocolError as exc:
                usage.token_usage_known = False
                self._persist_usage(usage)
                return self._review(batch_id, usage, observations, "invalid model protocol")
            except ModelError as exc:
                usage.token_usage_known = not exc.usage_unknown
                self._persist_usage(usage)
                if exc.category == "TOKEN_BUDGET":
                    return self._review(batch_id, usage, observations, "token budget exhausted")
                if exc.category == "BUDGET_EXHAUSTED":
                    return self._review(batch_id, usage, observations, "wall-clock budget exhausted")
                if exc.retryable and usage.model_attempts >= self.task.budget.max_model_calls:
                    return self._review(batch_id, usage, observations, "model request attempt budget exhausted")
                category = exc.category if exc.category in {"NOT_CONFIGURED", "UNSUPPORTED", "TOKEN_BUDGET", "MODEL_HTTP", "MODEL_TRANSPORT", "MODEL_PROTOCOL"} else "MODEL_ERROR"
                return self._review(batch_id, usage, observations, f"{category}: model request failed")
            except Exception as exc:
                usage.token_usage_known = False
                self._persist_usage(usage)
                return self._review(batch_id, usage, observations, f"unexpected model failure: {type(exc).__name__}")

            if usage.tokens > self.task.budget.max_tokens:
                return self._review(batch_id, usage, observations, "token budget exhausted")

            if decision.kind == "tool_call" and decision.tool_call is not None:
                blocked = self._tool_budget_error(usage, decision.tool_call.name)
                if blocked:
                    return self._review(batch_id, usage, observations, blocked)
                usage.tool_calls += 1
                if decision.tool_call.name == "edit_file":
                    usage.edit_attempts += 1
                self._persist_usage(usage)
                observation = self.executor.execute(decision.tool_call, expected_workspace_revision=self.executor.workspace.revision, deadline=usage.started_at + self.task.budget.max_wall_seconds)
                self._record(observation, observations, memory)
                if observation.status == ToolStatus.OK and decision.tool_call.name == "edit_file":
                    self.cache.invalidate(set(observation.source_paths))
                state["workspace_revision"] = self.executor.workspace.revision
                continue

            if decision.kind == "action_chunk":
                if not self.chunking_enabled:
                    return self._review(batch_id, usage, observations, "action chunk rejected because chunking is disabled")
                try:
                    chunk = self._parse_chunk(decision.action_chunk)
                except (TypeError, ValueError) as exc:
                    return self._review(batch_id, usage, observations, "invalid action chunk")
                remaining = self.task.budget.max_tool_calls - usage.tool_calls
                action_limit = min(self.task.budget.max_chunk_actions, remaining)
                result = self.chunk_executor.execute(
                    chunk, self.executor,
                    expected_workspace_revision=self.executor.workspace.revision,
                    max_actions=action_limit,
                    before_action=lambda _action: self._chunk_budget_error(usage),
                    deadline=usage.started_at + self.task.budget.max_wall_seconds,
                )
                usage.chunk_actions += len(result.completed)
                self._persist_usage(usage)
                for observation in (*result.completed, *result.not_executed):
                    self._record(observation, observations, memory)
                state["workspace_revision"] = self.executor.workspace.revision
                if not result.accepted:
                    return self._review(batch_id, usage, observations, "action chunk incomplete")
                continue

            if decision.kind == "batch_ready":
                if self._blocking_tool_failures:
                    details = ", ".join(f"{name}/{status}" for name, status in self._blocking_tool_failures[:8])
                    return self._review(batch_id, usage, observations, f"tool observation incomplete: {details}")
                blocked = self._tool_budget_error(usage, "git_diff")
                if blocked:
                    return self._review(batch_id, usage, observations, blocked)
                proposal = self._proposal(batch_id, issues, decision, usage, observations)
                if proposal is None:
                    return self._review(batch_id, usage, observations, self.proposal_error or "could not obtain a complete real Git diff")
                return AgentResult(batch_id, self.worker_id, proposal, False, None, usage, tuple(observations))

            if decision.kind == "review_required":
                return self._review(batch_id, usage, observations, "model requested human review")

            return self._review(batch_id, usage, observations, "unsupported model decision")

        reasons = []
        if usage.model_calls >= self.task.budget.max_model_calls:
            reasons.append("model call budget exhausted")
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            reasons.append("tool call budget exhausted")
        if usage.tokens >= self.task.budget.max_tokens:
            reasons.append("token budget exhausted")
        if not usage.token_usage_known:
            reasons.append("model token usage unavailable; stopped to preserve budget")
        if usage.edit_attempts >= self.task.budget.max_edit_attempts:
            reasons.append("edit attempt budget exhausted")
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            reasons.append("wall-clock budget exhausted")
        return self._review(batch_id, usage, observations, "; ".join(reasons) or "budget exhausted")

    def _record(self, observation: Observation, observations: list[Observation], memory: TaskStateMemory) -> None:
        observations.append(observation)
        memory.add(observation)
        if not observation.complete or observation.status in {
            ToolStatus.ERROR, ToolStatus.PARTIAL, ToolStatus.TRUNCATED, ToolStatus.VERSION_CHANGED,
            ToolStatus.AMBIGUOUS, ToolStatus.UNSUPPORTED, ToolStatus.NOT_EXECUTED,
        }:
            self._blocking_tool_failures.append((observation.tool, observation.status.value))
        if self.trace_callback:
            self.trace_callback(observation)

    def _record_attempt(self, usage: AgentUsage) -> None:
        usage.model_attempts += 1
        # Persist uncertainty before crossing the provider boundary. A crash
        # after remote acceptance but before usage persistence then fails closed.
        usage.token_usage_known = False
        self._persist_usage(usage)

    def _persist_usage(self, usage: AgentUsage) -> None:
        usage.elapsed_seconds = max(0.0, time.monotonic() - usage.started_at) if usage.started_at else 0.0
        if self.usage_callback:
            self.usage_callback(usage)

    def _record_retry(self, usage: AgentUsage, error: ModelError, delay: float) -> None:
        usage.model_retries += 1
        usage.retry_events.append({"category": error.category, "attempt": usage.model_attempts, "delay_seconds": round(delay, 3)})
        self._persist_usage(usage)

    def _tool_budget_error(self, usage: AgentUsage, name: str) -> str | None:
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            return "tool call budget exhausted"
        if name == "edit_file" and usage.edit_attempts >= self.task.budget.max_edit_attempts:
            return "edit attempt budget exhausted"
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            return "wall-clock budget exhausted"
        return None

    def _chunk_budget_error(self, usage: AgentUsage) -> str | None:
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            return "tool call budget exhausted during action chunk"
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            return "wall-clock budget exhausted during action chunk"
        usage.tool_calls += 1
        self._persist_usage(usage)
        return None

    def _prompt_observations(self, observations: Sequence[Observation]) -> list[dict[str, Any]]:
        cutoff = max(0, len(observations) - self.max_recent_observations)
        older = observations[:cutoff]
        counts: dict[str, int] = {}
        for item in older:
            key = f"{item.tool}:{item.status.value}"
            counts[key] = counts.get(key, 0) + 1
        result: list[dict[str, Any]] = []
        if older:
            errors = [item.error[:self.max_observation_chars] for item in older if item.error][-5:]
            result.append({"historical_summary": {"count": len(older), "tool_status_counts": counts, "source_paths": sorted({path for item in older for path in item.source_paths})[:50], "errors": errors}})
        pinned = [
            {"tool": item.tool, "source_paths": list(item.source_paths), "file_hashes": dict(item.file_hashes)}
            for item in observations if item.status == ToolStatus.OK and item.file_hashes
        ][-8:]
        if pinned:
            result.append({"pinned_evidence": pinned})
        for item in observations[cutoff:]:
            primitive = to_primitive(item)
            content = primitive.get("content")
            content_text = canonical_json(content) if content is not None else ""
            if len(content_text) > self.max_observation_chars:
                primitive["content"] = content_text[: self.max_observation_chars] + "…[truncated]"
                primitive["complete"] = False
            result.append(primitive)
        return result

    def _review(self, batch_id: str, usage: AgentUsage, observations: list[Observation], reason: str) -> AgentResult:
        usage.elapsed_seconds = max(0.0, time.monotonic() - usage.started_at) if usage.started_at else 0.0
        self._persist_usage(usage)
        return AgentResult(batch_id, self.worker_id, None, True, _safe_review_reason(reason), usage, tuple(observations))

    def _skills(self, issues: Sequence[Issue], *, deadline: float | None = None) -> str:
        if self.skill_router is None:
            return "No Skill store configured."
        records = self.skill_router.route_many(
            tuple(issues),
            max_skill_bytes=self.tool_limits.max_file_bytes,
            max_scan_bytes=self.max_skill_scan_bytes,
            max_context_chars=self.max_skill_context_chars,
            deadline=deadline,
        )
        if not records:
            return "No matching versioned skill is configured. Current source evidence remains authoritative."
        return "\n\n".join(f"[Skill {skill.skill_id} v{skill.version} from {skill.source}]\n{skill.content}" for skill in records)

    def _parse_chunk(self, value: Any) -> ActionChunk:
        if not isinstance(value, Mapping):
            raise ValueError("action_chunk must be an object")
        raw_actions = value.get("actions", ())
        if not isinstance(raw_actions, list):
            raise ValueError("action_chunk.actions must be a list")
        actions: list[ChunkAction] = []
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, Mapping) or not raw.get("tool"):
                raise ValueError(f"invalid action at index {index}")
            arguments = raw.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"arguments must be an object at index {index}")
            actions.append(ChunkAction(str(raw["tool"]), arguments, str(raw.get("action_id", f"action-{index}")), tuple(str(item) for item in raw.get("depends_on", ()))))
        return ActionChunk(str(value.get("chunk_id", "chunk")), tuple(actions))

    def _proposal(self, batch_id: str, issues: Sequence[Issue], decision: ModelDecision, usage: AgentUsage, observations: list[Observation]) -> BatchProposal | None:
        diff_call = ToolCall("git_diff", {}, f"diff-{batch_id}")
        usage.tool_calls += 1
        self._persist_usage(usage)
        diff_observation = self.executor.execute(diff_call, expected_workspace_revision=self.executor.workspace.revision, deadline=usage.started_at + self.task.budget.max_wall_seconds)
        self._record(diff_observation, observations, TaskStateMemory(self.task.task_id, self.worker_id))
        # The proposal must never use a model-written diff. Only the Git tool output is authoritative.
        if diff_observation.status not in {ToolStatus.OK, ToolStatus.EMPTY}:
            return None
        payload = diff_observation.content or {}
        diff = str(payload.get("diff", "")) if isinstance(payload, Mapping) else ""
        untracked = list(payload.get("untracked_files", ())) if isinstance(payload, Mapping) else []
        changed_files = set(untracked)
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                changed_files.add(line[6:])
            elif line.startswith("--- a/"):
                changed_files.add(line[6:])
        scope = self.scope_guard.inspect(diff, changed_files)
        usage.changed_files = scope.changed_files
        usage.diff_lines = scope.diff_lines
        self._persist_usage(usage)
        if scope.violations:
            self.proposal_error = "patch scope requires review: " + "; ".join(scope.violations)
            return None
        action_map: dict[str, ActionKind] = {}
        for issue in issues:
            raw = decision.action_map.get(issue_id(issue), ActionKind.UNRESOLVED.value)
            try:
                action = ActionKind(raw)
            except ValueError:
                action = ActionKind.REVIEW_REQUIRED
            if action == ActionKind.SUPPRESSION_CANDIDATE and not any("evidence" in str(item.content).lower() for item in observations):
                action = ActionKind.REVIEW_REQUIRED
            if action == ActionKind.SUPPRESSION_CANDIDATE and "evidence" not in (decision.reason or "").lower():
                action = ActionKind.REVIEW_REQUIRED
            action_map[issue_id(issue)] = action
        if any(action == ActionKind.FIX_CANDIDATE for action in action_map.values()) and not diff and not changed_files:
            for identifier, action in list(action_map.items()):
                if action == ActionKind.FIX_CANDIDATE:
                    action_map[identifier] = ActionKind.REVIEW_REQUIRED
        unresolved = tuple(identifier for identifier, action in action_map.items() if action in {ActionKind.UNRESOLVED, ActionKind.REVIEW_REQUIRED})
        return BatchProposal(
            batch_id=batch_id,
            worker_id=self.worker_id,
            base_commit=self.task.base_commit,
            workspace_revision=self.executor.workspace.revision,
            action_map=action_map,
            changed_files=tuple(sorted(changed_files)),
            diff=diff,
            diff_hash=sha256_text(diff),
            risk=max((getattr(issue, "risk", RiskClass.UNKNOWN) for issue in issues), key=lambda item: {RiskClass.UNKNOWN: 0, RiskClass.LOW: 1, RiskClass.MEDIUM: 2, RiskClass.HIGH: 3}[item]),
            unresolved=unresolved,
            complete=not unresolved,
            review_notes=("Proposal only; it is not a frozen or verified candidate.", f"Patch scope: {scope.changed_files} files, {scope.diff_lines} changed lines ({scope.added_lines} added, {scope.deleted_lines} deleted)."),
        )
