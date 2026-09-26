"""Tool registry and the single permission/version boundary for all actions."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..config import ToolLimits
from ..context import ContextCache
from ..domain import Observation, ToolStatus, canonical_json
from ..memory import EpisodeStore
from ..models import ToolCall
from ..runtime.workspace import WorkspaceState
from ..skills import SkillStore
from .edit import EditTool
from .source import SourceTools


Handler = Callable[[dict[str, Any]], tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    read_only: bool
    required_args: tuple[str, ...] = ()
    allowed_in_chunk: bool = False
    properties: Mapping[str, Any] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": {"type": "object", "properties": dict(self.properties or {}), "additionalProperties": False, "required": list(self.required_args)}}}

    def validation_error(self, arguments: Mapping[str, Any]) -> str | None:
        missing = [key for key in self.required_args if key not in arguments]
        if missing:
            return f"missing arguments: {', '.join(missing)}"
        unknown = set(arguments) - set(self.properties)
        if unknown:
            return f"unsupported arguments: {', '.join(sorted(unknown))}"
        for key, value in arguments.items():
            schema = self.properties[key]
            expected = schema.get("type")
            valid = {
                "string": lambda item: isinstance(item, str),
                "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
                "array": lambda item: isinstance(item, list) and all(isinstance(element, str) for element in item),
            }.get(expected, lambda item: True)(value)
            if not valid or ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
                return f"invalid argument: {key}"
            if "minLength" in schema and not value.strip():
                return f"invalid argument: {key}"
        return None


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._specs[name].schema() for name in sorted(self._specs))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


class ToolExecutor:
    def __init__(
        self,
        workspace: WorkspaceState,
        *,
        limits: ToolLimits | None = None,
        skill_store: SkillStore | None = None,
        episode_store: EpisodeStore | None = None,
        cache: ContextCache | None = None,
    ) -> None:
        self.workspace = workspace
        self.limits = limits or ToolLimits()
        self.skill_store = skill_store
        self.episode_store = episode_store
        self.cache = cache
        self.registry = ToolRegistry()
        self._write_lock = threading.Lock()
        self._handlers: dict[str, Handler] = {}
        source = SourceTools(workspace, max_file_bytes=self.limits.max_file_bytes, max_output_chars=self.limits.max_output_chars, max_search_results=self.limits.max_search_results, cache=cache)
        edit = EditTool(workspace, max_file_bytes=self.limits.max_file_bytes)
        self._register(ToolSpec("read_file", "Read a bounded UTF-8 source range.", True, ("path",), True, {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "max_chars": {"type": "integer", "minimum": 1}}), source.read_file)
        self._register(ToolSpec("search_code", "Text search only; not complete C++ semantic navigation.", True, ("query",), True, {"query": {"type": "string", "minLength": 1}, "paths": {"type": "array", "items": {"type": "string"}}, "max_results": {"type": "integer", "minimum": 1}}), source.search_code)
        self._register(ToolSpec("find_definition", "Semantic definition lookup when clangd is configured.", True, ("symbol",), True, {"symbol": {"type": "string"}}), source.unsupported_navigation)
        self._register(ToolSpec("find_references", "Semantic reference lookup when clangd is configured.", True, ("symbol",), True, {"symbol": {"type": "string"}}), source.unsupported_navigation)
        self._register(ToolSpec("edit_file", "Replace one uniquely matched old text after hash validation.", False, ("path", "expected_hash", "old_text", "new_text"), False, {"path": {"type": "string"}, "expected_hash": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}), edit.edit_file)
        self._register(ToolSpec("git_diff", "Read the actual Git working-tree diff.", True), source.git_diff)
        self._register(ToolSpec("read_guideline", "Read a versioned Skill guideline.", True, ("skill_id",), True, {"skill_id": {"type": "string"}}), self._read_guideline)
        self._register(ToolSpec("memory_retrieve", "Retrieve provenance-bound historical episodes.", True, (), False, {"repo": {"type": "string"}, "module": {"type": "string"}, "rule": {"type": "string"}, "keywords": {"type": "array", "items": {"type": "string"}}, "source_commit": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}), self._memory_retrieve)

    def _register(self, spec: ToolSpec, handler: Handler) -> None:
        self.registry.register(spec)
        self._handlers[spec.name] = handler

    def execute(self, call: ToolCall | Any, *, expected_workspace_revision: int | None = None, deadline: float | None = None) -> Observation:
        start = time.monotonic()
        call_id = str(getattr(call, "call_id", getattr(call, "action_id", "unknown")))
        name = str(getattr(call, "name", getattr(call, "tool", "")))
        arguments = getattr(call, "arguments", {})
        if not isinstance(arguments, dict):
            return self._observation(call_id, name, ToolStatus.ERROR, None, (), {}, False, start, "arguments must be an object")
        current_changed = self.workspace.refresh()
        if expected_workspace_revision is not None and self.workspace.revision != expected_workspace_revision:
            return self._observation(call_id, name, ToolStatus.VERSION_CHANGED, None, (), dict(self.workspace.observed_hashes), False, start, "workspace revision changed")
        spec = self.registry.get(name)
        if spec is None:
            return self._observation(call_id, name, ToolStatus.ERROR, None, (), {}, False, start, f"unknown tool: {name}")
        invalid = spec.validation_error(arguments)
        if invalid:
            return self._observation(call_id, name, ToolStatus.ERROR, None, (), {}, False, start, invalid)
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            return self._observation(call_id, name, ToolStatus.ERROR, None, (), {}, False, start, "wall-clock budget exhausted before tool execution")
        try:
            lock = self._write_lock if not spec.read_only else _NullLock()
            with lock:
                handler_args = dict(arguments)
                if deadline is not None:
                    handler_args["_deadline"] = deadline
                status, content, paths, hashes, complete, error = self._handlers[name](handler_args)
        except Exception as exc:  # tool failures become observations, never silent success
            status, content, paths, hashes, complete, error = ToolStatus.ERROR, None, (), {}, False, f"{type(exc).__name__}: {exc}"
        if content is not None and len(canonical_json(content)) > self.limits.max_output_chars:
            content = {"preview": canonical_json(content)[: self.limits.max_output_chars], "truncated": True}
            status = ToolStatus.TRUNCATED
            complete = False
            error = error or "tool output limit reached"
        return self._observation(call_id, name, status, content, paths, hashes, complete, start, error)

    def _observation(self, call_id: str, name: str, status: ToolStatus, content: Any, paths: tuple[str, ...], hashes: Mapping[str, str], complete: bool, start: float, error: str | None) -> Observation:
        return Observation(call_id, name, status, content, source_paths=paths, workspace_revision=self.workspace.revision, file_hashes=dict(hashes), complete=complete, elapsed_ms=int((time.monotonic() - start) * 1000), error=error)

    def _read_guideline(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        if self.skill_store is None:
            return ToolStatus.UNSUPPORTED, None, (), {}, False, "Skill store is not configured"
        try:
            skill = self.skill_store.load(str(arguments["skill_id"]), max_bytes=self.limits.max_file_bytes, deadline=arguments.get("_deadline"))
        except (OSError, TimeoutError, ValueError) as exc:
            return ToolStatus.ERROR, None, (), {}, False, str(exc)
        return ToolStatus.OK, {"skill_id": skill.skill_id, "version": skill.version, "source": skill.source, "content_hash": skill.content_hash, "content": skill.content}, (), {}, True, None

    def _memory_retrieve(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        if self.episode_store is None:
            return ToolStatus.UNSUPPORTED, None, (), {}, False, "episode memory is not configured"
        episodes = self.episode_store.retrieve(
            repo=str(arguments.get("repo", "")),
            module=str(arguments.get("module")) if arguments.get("module") else None,
            rule=str(arguments.get("rule")) if arguments.get("rule") else None,
            keywords=tuple(str(item) for item in arguments.get("keywords", ())),
            source_commit=str(arguments.get("source_commit", "")),
            limit=min(10, int(arguments.get("limit", 5))),
            deadline=float(arguments["_deadline"]) if arguments.get("_deadline") is not None else None,
        )
        if not episodes:
            return ToolStatus.EMPTY, [], (), {}, True, "no matching historical episode"
        return ToolStatus.OK, [episode.__dict__ for episode in episodes], (), {}, True, None


class _NullLock:
    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None
