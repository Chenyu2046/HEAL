"""Model/provider contracts and one explicit OpenAI-compatible client."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .domain import RepairTask, to_primitive


class ModelError(RuntimeError):
    """Provider failure; uncertain token accounting is non-retryable by default."""

    def __init__(self, message: str, *, category: str = "MODEL_ERROR", retryable: bool = False, usage_unknown: bool = True) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.usage_unknown = usage_unknown


class ModelProtocolError(ModelError):
    """The provider response violates the model contract."""


class ModelDeadlineExceeded(ModelError):
    def __init__(self) -> None:
        super().__init__("model call exceeded the task deadline", category="MODEL_DEADLINE", retryable=False, usage_unknown=True)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reported: bool = False

    def __post_init__(self) -> None:
        if type(self.input_tokens) is not int or self.input_tokens < 0 or type(self.output_tokens) is not int or self.output_tokens < 0:
            raise ValueError("model token counts must be non-negative integers")
        if type(self.reported) is not bool:
            raise ValueError("model usage reported flag must be boolean")

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
        raw_usage = value.get("usage")
        usage = ModelUsage()
        if raw_usage is not None:
            if not isinstance(raw_usage, Mapping):
                raise ModelProtocolError("model usage must be an object")
            input_tokens = raw_usage.get("input_tokens")
            output_tokens = raw_usage.get("output_tokens")
            if type(input_tokens) is not int or input_tokens < 0 or type(output_tokens) is not int or output_tokens < 0:
                raise ModelProtocolError("model usage requires non-negative input_tokens and output_tokens")
            usage = ModelUsage(input_tokens, output_tokens, reported=True)
        if kind == "tool_call":
            raw = value.get("tool_call", value)
            if not isinstance(raw, Mapping) or not raw.get("name"):
                raise ModelProtocolError("tool_call requires a tool name")
            arguments = raw.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (ValueError, RecursionError) as exc:
                    raise ModelProtocolError("tool_call arguments are not valid JSON") from exc
            if not isinstance(arguments, dict):
                raise ModelProtocolError("tool_call arguments must be an object")
            return cls(kind="tool_call", tool_call=ToolCall(str(raw["name"]), arguments, str(raw.get("call_id", uuid.uuid4().hex))), usage=usage)
        if kind == "action_chunk":
            # Parsing is intentionally delegated to AgentLoop so a disabled chunk
            # cannot accidentally become a write action.
            return cls(kind="action_chunk", action_chunk=value.get("action_chunk", value), usage=usage)
        if kind in {"batch_ready", "review_required"}:
            action_map = value.get("action_map", {})
            if not isinstance(action_map, Mapping):
                raise ModelProtocolError("action_map must be an object")
            return cls(kind=kind, reason=str(value.get("reason", "")) or None, action_map={str(k): str(v) for k, v in action_map.items()}, usage=usage)
        raise ModelProtocolError(f"unsupported model decision kind: {kind!r}")


class ModelAdapter(ABC):
    @abstractmethod
    def decide(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> ModelDecision:
        raise RuntimeError("ModelAdapter.decide must be implemented by a provider")

    def decide_with_deadline(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, deadline: float, token_limit: int) -> ModelDecision:
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()
        raise ModelError("provider must implement deadline- and token-bounded requests", category="UNSUPPORTED", usage_unknown=False)


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

    def decide_with_deadline(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, deadline: float, token_limit: int) -> ModelDecision:
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()
        if token_limit <= 0:
            raise ModelError("no model token budget remains", category="TOKEN_BUDGET", usage_unknown=False)
        try:
            decision = self.decide(task, state, observations, tools)
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise ModelDeadlineExceeded() from exc
            raise
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()
        if not decision.usage.reported:
            decision = replace(decision, usage=ModelUsage(reported=True))
        return decision


class OpenAICompatibleModel(ModelAdapter):
    """A concrete JSON tool-calling client with explicit timeout/error mapping."""

    def __init__(self, *, endpoint: str | None, model_id: str, api_key_env: str | None, timeout_seconds: float = 60.0) -> None:
        if not endpoint:
            raise ModelError("model endpoint is not configured", category="NOT_CONFIGURED", usage_unknown=False)
        self.endpoint = endpoint
        self.model_id = model_id
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds

    def decide(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> ModelDecision:
        return self._decide(task, state, observations, tools, deadline=None, token_limit=None)

    def decide_with_deadline(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, deadline: float, token_limit: int) -> ModelDecision:
        return self._decide(task, state, observations, tools, deadline=deadline, token_limit=token_limit)

    def _decide(self, task: RepairTask, state: Mapping[str, Any], observations: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, deadline: float | None, token_limit: int | None) -> ModelDecision:
        api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if self.api_key_env and not api_key:
            raise ModelError(f"missing credential environment variable: {self.api_key_env}", category="NOT_CONFIGURED", usage_unknown=False)
        payload = {
            "model": self.model_id,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return exactly one JSON decision: tool_call, action_chunk, batch_ready, or review_required. Never claim verification."},
                {"role": "user", "content": json.dumps({"task": to_primitive(task), "state": to_primitive(state), "observations": to_primitive(observations)}, ensure_ascii=False)},
            ],
            "tools": list(tools),
            "tool_choice": "auto",
        }
        if token_limit is not None:
            self._check_deadline(deadline)
            if token_limit <= 0:
                raise ModelError("no model token budget remains", category="TOKEN_BUDGET", usage_unknown=False)
            prompt_reserve = self._prompt_token_reserve(payload["messages"], payload["tools"])
            completion_limit = token_limit - prompt_reserve
            if completion_limit < 1:
                raise ModelError("prompt reserve exceeds the remaining model token budget", category="TOKEN_BUDGET", usage_unknown=False)
            payload["max_tokens"] = completion_limit
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            timeout = self.timeout_seconds
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelDeadlineExceeded()
                timeout = min(timeout, remaining)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = self._read_response(response, deadline=deadline)
        except ModelDeadlineExceeded:
            raise
        except urllib.error.HTTPError as exc:
            self._check_deadline(deadline)
            # A standard 429 rejects the request before generation; other HTTP
            # failures may happen after usage has accrued and therefore fail closed.
            raise ModelError(
                f"model HTTP {exc.code} response",
                category="MODEL_HTTP",
                retryable=exc.code >= 500 or exc.code == 429,
                usage_unknown=exc.code != 429,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise ModelDeadlineExceeded() from exc
            raise ModelError("model transport failure", category="MODEL_TRANSPORT", retryable=True, usage_unknown=True) from exc
        try:
            response_payload = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            self._check_deadline(deadline)
            raise ModelProtocolError("model response is not valid UTF-8 JSON") from exc
        if not isinstance(response_payload, Mapping):
            self._raise_protocol_error(deadline, "model response must be a JSON object")
        choices = response_payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            self._raise_protocol_error(deadline, "model response must contain a chat choice object")
        choice = choices[0].get("message")
        if not isinstance(choice, Mapping):
            self._raise_protocol_error(deadline, "model chat choice must contain a message object")

        usage_payload = response_payload.get("usage")
        if not isinstance(usage_payload, Mapping):
            self._raise_protocol_error(deadline, "model usage must be an object")
        token_counts: list[int] = []
        for field_name in ("prompt_tokens", "completion_tokens"):
            count = usage_payload.get(field_name)
            if type(count) is not int or count < 0:
                self._raise_protocol_error(deadline, f"model usage must include non-negative integer {field_name}")
            token_counts.append(count)
        usage = ModelUsage(*token_counts, reported=True)
        self._check_deadline(deadline)

        tool_calls = choice.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            self._raise_protocol_error(deadline, "model tool_calls must be an array")
        if tool_calls:
            if len(tool_calls) != 1:
                self._raise_protocol_error(deadline, "multiple tool calls are not supported in one decision")
            tool = tool_calls[0]
            if not isinstance(tool, Mapping) or not isinstance(tool.get("function"), Mapping):
                self._raise_protocol_error(deadline, "model tool call must contain a function object")
            function = tool["function"]
            name = function.get("name")
            if not isinstance(name, str) or not name:
                self._raise_protocol_error(deadline, "model tool call requires a function name")
            raw_arguments = function.get("arguments", "{}")
            if not isinstance(raw_arguments, str):
                self._raise_protocol_error(deadline, "model tool arguments must be a JSON string")
            try:
                args = json.loads(raw_arguments)
            except (ValueError, RecursionError) as exc:
                self._check_deadline(deadline)
                raise ModelProtocolError("model tool arguments are not valid JSON") from exc
            if not isinstance(args, dict):
                self._raise_protocol_error(deadline, "model tool arguments must be an object")
            call_id = tool.get("id", uuid.uuid4().hex)
            if not isinstance(call_id, str):
                self._raise_protocol_error(deadline, "model tool call id must be a string")
            self._check_deadline(deadline)
            return ModelDecision("tool_call", ToolCall(name, args, call_id), usage=usage)
        content = choice.get("content")
        if not isinstance(content, str):
            self._raise_protocol_error(deadline, "model returned neither a tool call nor JSON content")
        try:
            raw_decision = json.loads(content)
        except (ValueError, RecursionError) as exc:
            self._check_deadline(deadline)
            raise ModelProtocolError("model content is not a JSON decision") from exc
        if not isinstance(raw_decision, Mapping):
            self._raise_protocol_error(deadline, "model decision content must be a JSON object")
        try:
            decision = ModelDecision.from_mapping(raw_decision)
        except ModelError:
            self._check_deadline(deadline)
            raise
        self._check_deadline(deadline)
        return ModelDecision(decision.kind, decision.tool_call, decision.action_chunk, decision.reason, decision.action_map, usage)

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()

    @classmethod
    def _raise_protocol_error(cls, deadline: float | None, message: str) -> None:
        cls._check_deadline(deadline)
        raise ModelProtocolError(message)

    @staticmethod
    def _prompt_token_reserve(messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]) -> int:
        """Use UTF-8 byte length plus framing allowance as a conservative preflight reserve."""
        encoded = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return len(encoded) + 16 * len(messages) + 16 * len(tools) + 32

    def _read_response(self, response, *, deadline: float | None) -> bytes:
        chunks: list[bytes] = []
        total = 0
        limit = 8_000_000
        while total <= limit:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelDeadlineExceeded()
                response_file = getattr(response, "fp", None)
                raw_socket = None
                for socket_file in (response_file, getattr(response_file, "fp", None)):
                    raw_socket = getattr(getattr(socket_file, "raw", None), "_sock", None)
                    if raw_socket is not None:
                        break
                if raw_socket is not None:
                    raw_socket.settimeout(min(self.timeout_seconds, remaining))
            read_one = getattr(response, "read1", None)
            block = read_one(min(64_000, limit + 1 - total)) if callable(read_one) else response.read(min(64_000, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
        if deadline is not None and time.monotonic() >= deadline:
            raise ModelDeadlineExceeded()
        if total > limit:
            raise ModelProtocolError("model response exceeds the 8 MB protocol limit")
        return b"".join(chunks)
