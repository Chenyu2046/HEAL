"""Model/provider contracts and one explicit OpenAI-compatible client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .domain import RepairTask, ToolStatus, redact_text


class ModelError(RuntimeError):
    def __init__(self, message: str, *, category: str = "MODEL_ERROR", retryable: bool = False) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class ModelProtocolError(ModelError):
    """The provider response violates the model contract."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelDecision:
    kind: str
    tool_call: ToolCall | None = None
    action_chunk: Any | None = None
    reason: str | None = None
    action_map: Mapping[str, str] = field(default_factory=dict)
    usage: ModelUsage = field(default_factory=ModelUsage)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelDecision":
        kind = str(value.get("kind", "")).lower()
        if kind == "tool_call":
            raw = value.get("tool_call", value)
            if not isinstance(raw, Mapping) or not raw.get("name"):
                raise ModelProtocolError("tool_call requires a tool name")
            arguments = raw.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ModelProtocolError("tool_call arguments are not valid JSON") from exc
            if not isinstance(arguments, dict):
                raise ModelProtocolError("tool_call arguments must be an object")
            return cls(kind="tool_call", tool_call=ToolCall(str(raw["name"]), arguments, str(raw.get("call_id", uuid.uuid4().hex))))
        if kind == "action_chunk":
            # Parsing is intentionally delegated to AgentLoop so a disabled chunk
            # cannot accidentally become a write action.
            return cls(kind="action_chunk", action_chunk=value.get("action_chunk", value))
        if kind in {"batch_ready", "review_required"}:
            action_map = value.get("action_map", {})
            if not isinstance(action_map, Mapping):
                raise ModelProtocolError("action_map must be an object")
            return cls(kind=kind, reason=str(value.get("reason", "")) or None, action_map={str(k): str(v) for k, v in action_map.items()})
        raise ModelProtocolError(f"unsupported model decision kind: {kind!r}")


class ModelAdapter(ABC):
    @abstractmethod
    def decide(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> ModelDecision:
        raise RuntimeError("ModelAdapter.decide must be implemented by a provider")


class ScriptedModel(ModelAdapter):
    """Deterministic protocol model for later offline verification, not a success fake."""

    def __init__(self, decisions: Sequence[Mapping[str, Any] | ModelDecision], model_id: str = "scripted") -> None:
        self.model_id = model_id
        self._decisions = list(decisions)
        self.calls = 0

    def decide(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> ModelDecision:
        self.calls += 1
        if not self._decisions:
            return ModelDecision(kind="review_required", reason="scripted model has no more decisions")
        decision = self._decisions.pop(0)
        if isinstance(decision, ModelDecision):
            return decision
        return ModelDecision.from_mapping(decision)


class OpenAICompatibleModel(ModelAdapter):
    """A concrete JSON tool-calling client with explicit timeout/error mapping."""

    def __init__(self, *, endpoint: str | None, model_id: str, api_key_env: str | None, timeout_seconds: float = 60.0) -> None:
        if not endpoint:
            raise ModelError("model endpoint is not configured", category="NOT_CONFIGURED")
        self.endpoint = endpoint
        self.model_id = model_id
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds

    def decide(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> ModelDecision:
        api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not api_key:
            raise ModelError(f"missing credential environment variable: {self.api_key_env}", category="NOT_CONFIGURED")
        payload = {
            "model": self.model_id,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return exactly one JSON decision: tool_call, action_chunk, batch_ready, or review_required. Never claim verification."},
                {"role": "user", "content": json.dumps({"task": task, "state": state, "observations": observations}, default=str, ensure_ascii=False)},
            ],
            "tools": list(tools),
            "tool_choice": "auto",
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = redact_text(exc.read().decode("utf-8", errors="replace")[:500])
            raise ModelError(f"model HTTP {exc.code}: {detail}", category="MODEL_HTTP", retryable=exc.code >= 500 or exc.code == 429) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelError(f"model transport failure: {redact_text(str(exc))}", category="MODEL_TRANSPORT", retryable=True) from exc
        try:
            response_payload = json.loads(raw.decode("utf-8"))
            choice = response_payload["choices"][0]["message"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError("model response is not a supported chat response") from exc
        usage_payload = response_payload.get("usage", {})
        usage = ModelUsage(int(usage_payload.get("prompt_tokens", 0)), int(usage_payload.get("completion_tokens", 0)))
        tool_calls = choice.get("tool_calls") or []
        if tool_calls:
            if len(tool_calls) != 1:
                raise ModelProtocolError("multiple tool calls are not supported in one decision")
            function = tool_calls[0].get("function", {})
            try:
                args = json.loads(function.get("arguments", "{}"))
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("model tool arguments are not valid JSON") from exc
            if not isinstance(args, dict):
                raise ModelProtocolError("model tool arguments must be an object")
            return ModelDecision("tool_call", ToolCall(str(function.get("name", "")), args, str(tool_calls[0].get("id", uuid.uuid4().hex))), usage=usage)
        content = choice.get("content")
        if not isinstance(content, str):
            raise ModelProtocolError("model returned neither a tool call nor JSON content")
        try:
            decision = ModelDecision.from_mapping(json.loads(content))
        except (json.JSONDecodeError, ModelError) as exc:
            if isinstance(exc, ModelError):
                raise
            raise ModelProtocolError("model content is not a JSON decision") from exc
        return ModelDecision(decision.kind, decision.tool_call, decision.action_chunk, decision.reason, decision.action_map, usage)
