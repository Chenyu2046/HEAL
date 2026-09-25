"""Single Repair Agent loop with bounded budgets and optional read-only chunks."""

from __future__ import annotations

import time
from dataclasses import dataclass
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
from .memory import BatchCache, TaskStateMemory
from .models import ModelAdapter, ModelDecision, ModelError, ModelProtocolError, ToolCall
from .skills import SkillRouter
from .tools.chunking import ActionChunk, ChunkAction, ChunkExecutor
from .tools.executor import ToolExecutor


TraceCallback = Callable[[Observation], None]


@dataclass
class AgentUsage:
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    edit_attempts: int = 0
    started_at: float = 0.0

    def within(self, budget: Budget) -> bool:
        return (
            self.model_calls < budget.max_model_calls
            and self.tool_calls < budget.max_tool_calls
            and self.tokens <= budget.max_tokens
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
        trace_callback: TraceCallback | None = None,
    ) -> None:
        self.task = task
        self.worker_id = worker_id
        self.model = model
        self.executor = executor
        self.skill_router = skill_router
        self.chunking_enabled = chunking_enabled
        self.trace_callback = trace_callback
        self.chunk_executor = ChunkExecutor()
        self.cache = BatchCache()

    def run(self, batch_id: str, issues: Sequence[Issue]) -> AgentResult:
        usage = AgentUsage(started_at=time.monotonic())
        memory = TaskStateMemory(task_id=self.task.task_id, worker_id=self.worker_id)
        observations: list[Observation] = []
        state: dict[str, Any] = {
            "worker_id": self.worker_id,
            "batch_id": batch_id,
            "workspace_revision": self.executor.workspace.revision,
            "skill_injection": self._skills(issues),
            "issue_ids": [issue_id(issue) for issue in issues],
        }

        while usage.within(self.task.budget):
            try:
                decision = self.model.decide(
                    self.task,
                    state,
                    [to_primitive(observation) for observation in observations],
                    self.executor.registry.schemas(),
                )
                usage.model_calls += 1
                usage.tokens += decision.usage.total_tokens
            except ModelProtocolError as exc:
                return self._review(batch_id, usage, observations, f"invalid model protocol: {exc}")
            except ModelError as exc:
                return self._review(batch_id, usage, observations, f"{exc.category}: {exc}")
            except Exception as exc:
                return self._review(batch_id, usage, observations, f"unexpected model failure: {type(exc).__name__}: {exc}")

            if decision.kind == "tool_call" and decision.tool_call is not None:
                if decision.tool_call.name == "edit_file":
                    usage.edit_attempts += 1
                usage.tool_calls += 1
                observation = self.executor.execute(decision.tool_call, expected_workspace_revision=self.executor.workspace.revision)
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
                    return self._review(batch_id, usage, observations, f"invalid action chunk: {exc}")
                result = self.chunk_executor.execute(chunk, self.executor, expected_workspace_revision=self.executor.workspace.revision)
                for observation in (*result.completed, *result.not_executed):
                    usage.tool_calls += 1
                    self._record(observation, observations, memory)
                state["workspace_revision"] = self.executor.workspace.revision
                if not result.accepted:
                    return self._review(batch_id, usage, observations, result.reason or "action chunk stopped")
                continue

            if decision.kind == "batch_ready":
                if usage.tool_calls >= self.task.budget.max_tool_calls:
                    return self._review(batch_id, usage, observations, "tool call budget exhausted before collecting the real diff")
                proposal = self._proposal(batch_id, issues, decision, usage, observations)
                if proposal is None:
                    return self._review(batch_id, usage, observations, "could not obtain a complete real Git diff")
                return AgentResult(batch_id, self.worker_id, proposal, False, None, usage, tuple(observations))

            if decision.kind == "review_required":
                return self._review(batch_id, usage, observations, decision.reason or "model requested review")

            return self._review(batch_id, usage, observations, f"unsupported decision: {decision.kind}")

        reasons = []
        if usage.model_calls >= self.task.budget.max_model_calls:
            reasons.append("model call budget exhausted")
        if usage.tool_calls >= self.task.budget.max_tool_calls:
            reasons.append("tool call budget exhausted")
        if usage.tokens > self.task.budget.max_tokens:
            reasons.append("token budget exhausted")
        if usage.edit_attempts >= self.task.budget.max_edit_attempts:
            reasons.append("edit attempt budget exhausted")
        if time.monotonic() - usage.started_at >= self.task.budget.max_wall_seconds:
            reasons.append("wall-clock budget exhausted")
        return self._review(batch_id, usage, observations, "; ".join(reasons) or "budget exhausted")

    def _record(self, observation: Observation, observations: list[Observation], memory: TaskStateMemory) -> None:
        observations.append(observation)
        memory.add(observation)
        if self.trace_callback:
            self.trace_callback(observation)

    def _review(self, batch_id: str, usage: AgentUsage, observations: list[Observation], reason: str) -> AgentResult:
        return AgentResult(batch_id, self.worker_id, None, True, reason, usage, tuple(observations))

    def _skills(self, issues: Sequence[Issue]) -> str:
        if self.skill_router is None:
            return "No Skill store configured."
        return "\n\n".join(self.skill_router.inject(issue) for issue in issues)

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
        diff_observation = self.executor.execute(diff_call, expected_workspace_revision=self.executor.workspace.revision)
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
            review_notes=("Proposal only; it is not a frozen or verified candidate.",),
        )
