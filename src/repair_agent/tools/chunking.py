"""Conservative read-only Action Chunking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..domain import Observation, ToolStatus


@dataclass(frozen=True)
class ChunkAction:
    tool: str
    arguments: dict[str, Any]
    action_id: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionChunk:
    chunk_id: str
    actions: tuple[ChunkAction, ...]


@dataclass(frozen=True)
class ChunkResult:
    chunk_id: str
    accepted: bool
    completed: tuple[Observation, ...] = ()
    not_executed: tuple[Observation, ...] = ()
    reason: str | None = None


class BoundaryDetector:
    READ_ONLY_TOOLS = frozenset({"read_file", "search_code", "find_definition", "find_references", "read_guideline", "memory_retrieve"})
    STOP_STATUSES = frozenset({ToolStatus.EMPTY, ToolStatus.AMBIGUOUS, ToolStatus.PARTIAL, ToolStatus.TRUNCATED, ToolStatus.VERSION_CHANGED, ToolStatus.UNSUPPORTED, ToolStatus.ERROR})

    def validate(self, chunk: ActionChunk, registry: Any) -> str | None:
        if not chunk.actions:
            return "chunk contains no actions"
        seen: set[str] = set()
        for action in chunk.actions:
            if action.action_id in seen:
                return f"duplicate action_id: {action.action_id}"
            seen.add(action.action_id)
            if action.depends_on:
                return f"dependent action is not eligible for chunking: {action.action_id}"
            if action.tool not in self.READ_ONLY_TOOLS:
                return f"non-read-only tool is not eligible for chunking: {action.tool}"
            spec = registry.get(action.tool)
            if spec is None or not spec.read_only:
                return f"tool is not registered as read-only: {action.tool}"
        return None


class ChunkExecutor:
    def __init__(self, boundary: BoundaryDetector | None = None) -> None:
        self.boundary = boundary or BoundaryDetector()

    def execute(self, chunk: ActionChunk, executor: Any, *, expected_workspace_revision: int) -> ChunkResult:
        reason = self.boundary.validate(chunk, executor.registry)
        if reason:
            return ChunkResult(
                chunk.chunk_id,
                False,
                not_executed=tuple(
                    Observation(tool_call_id=action.action_id, tool=action.tool, status=ToolStatus.NOT_EXECUTED, complete=False, error=reason)
                    for action in chunk.actions
                ),
                reason=reason,
            )
        completed: list[Observation] = []
        not_executed: list[Observation] = []
        for index, action in enumerate(chunk.actions):
            observation = executor.execute(action, expected_workspace_revision=expected_workspace_revision)
            completed.append(observation)
            if observation.status != ToolStatus.OK:
                for remaining in chunk.actions[index + 1 :]:
                    not_executed.append(
                        Observation(
                            tool_call_id=remaining.action_id,
                            tool=remaining.tool,
                            status=ToolStatus.NOT_EXECUTED,
                            workspace_revision=observation.workspace_revision,
                            complete=False,
                            error=f"chunk stopped after {action.action_id}: {observation.status.value}",
                        )
                    )
                return ChunkResult(chunk.chunk_id, False, tuple(completed), tuple(not_executed), observation.error or observation.status.value)
        return ChunkResult(chunk.chunk_id, True, tuple(completed), tuple(not_executed))
