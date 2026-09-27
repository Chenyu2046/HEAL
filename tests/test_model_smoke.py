"""G3/R5b model smoke tests: stdlib stub endpoint (always runnable) + env-gated real smoke.

Maps 1:1 to docs/tech-design.md §3.11 (R5b acceptances 1-3). The stub drives
OpenAICompatibleModel.decide through success, HTTP-error, and malformed-response
paths without any external service; the real smoke is skipped unless
HEAL_SMOKE_* environment is configured and is documentation, never a gate.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import skipUnless

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.domain import Finding, RepairTask, Severity
from repair_agent.models import ModelError, ModelProtocolError, ModelUsage, OpenAICompatibleModel, ToolCall


def stub_task() -> RepairTask:
    return RepairTask(task_id="task-1", run_id="run-1", repo="repo", base_commit="base", issues=(Finding("finding-1", "R001", Severity.LOW, "src/a.c", 1, "m"),))


def tool_call_response() -> bytes:
    return json.dumps({
        "choices": [{"message": {"tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{\"path\": \"src/a.c\"}"}}]}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }).encode("utf-8")


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "HEALStub/1"

    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        status, body, content_type = self.server.next_response()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def finish(self) -> None:
        super().finish()
        try:
            # explicit close so no connection socket outlives the request thread
            self.connection.close()
        except OSError:
            pass

    def log_message(self, *args) -> None:  # keep test output clean
        pass


class _StubEndpoint:
    """Ephemeral localhost HTTP endpoint returning scripted responses per call."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.requests: list[dict] = []

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1/chat/completions"

    def respond_with(self, *, status: int, body: bytes, content_type: str = "application/json") -> None:
        self._server.next_response = lambda: (status, body, content_type)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class ModelSmokeStubTests(unittest.TestCase):
    """R5b-2: success / HTTP-error / malformed-response protocol paths, fully deterministic."""

    def setUp(self) -> None:
        self.endpoint = _StubEndpoint()
        self.addCleanup(self.endpoint.close)
        self.model = OpenAICompatibleModel(endpoint=self.endpoint.url, model_id="stub-model", api_key_env=None, timeout_seconds=5.0)

    def test_success_parses_tool_call_and_reported_usage(self) -> None:
        self.endpoint.respond_with(status=200, body=tool_call_response())
        decision = self.model.decide(stub_task(), {}, [], [])
        self.assertEqual(decision.kind, "tool_call")
        self.assertIsInstance(decision.tool_call, ToolCall)
        self.assertEqual(decision.tool_call.name, "read_file")
        self.assertEqual(decision.tool_call.arguments, {"path": "src/a.c"})
        self.assertEqual(decision.usage, ModelUsage(12, 5, reported=True))

    def test_http_500_maps_to_retryable_model_http_with_unknown_usage(self) -> None:
        self.endpoint.respond_with(status=500, body=b'{"error": "boom"}')
        with self.assertRaises(ModelError) as caught:
            self.model.decide(stub_task(), {}, [], [])
        self.assertEqual(caught.exception.category, "MODEL_HTTP")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.usage_unknown)

    def test_non_json_body_is_a_protocol_error(self) -> None:
        self.endpoint.respond_with(status=200, body=b"<html>not json</html>", content_type="text/html")
        with self.assertRaises(ModelProtocolError) as caught:
            self.model.decide(stub_task(), {}, [], [])
        self.assertEqual(str(caught.exception), "model response is not valid UTF-8 JSON")

    def test_non_object_body_is_a_protocol_error(self) -> None:
        self.endpoint.respond_with(status=200, body=b"[1, 2, 3]")
        with self.assertRaises(ModelProtocolError) as caught:
            self.model.decide(stub_task(), {}, [], [])
        self.assertEqual(str(caught.exception), "model response must be a JSON object")

    def test_missing_usage_is_a_protocol_error(self) -> None:
        body = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
        self.endpoint.respond_with(status=200, body=body)
        with self.assertRaises(ModelProtocolError) as caught:
            self.model.decide(stub_task(), {}, [], [])
        self.assertEqual(str(caught.exception), "model usage must be an object")


@skipUnless(
    os.environ.get("HEAL_SMOKE_ENDPOINT") and os.environ.get("HEAL_SMOKE_API_KEY_ENV") and os.environ.get("HEAL_SMOKE_MODEL_ID"),
    "real-endpoint smoke test requires HEAL_SMOKE_* environment",
)
class RealEndpointSmokeTests(unittest.TestCase):
    """R5b-1/3: one minimal decide_with_deadline; the credential is read only via env indirection."""

    def test_minimal_decide_with_deadline(self) -> None:
        model = OpenAICompatibleModel(
            endpoint=os.environ["HEAL_SMOKE_ENDPOINT"],
            model_id=os.environ["HEAL_SMOKE_MODEL_ID"],
            api_key_env=os.environ["HEAL_SMOKE_API_KEY_ENV"],
            timeout_seconds=30.0,
        )
        try:
            decision = model.decide_with_deadline(stub_task(), {}, [], [], deadline=float("inf"), token_limit=100_000)
        except ModelError as exc:
            # A reachable endpoint that fails the contract is still a documented smoke result,
            # never a silent pass; the surfaced message is provider text, redacted defensively.
            self.fail(f"smoke decision failed: category={exc.category} usage_unknown={exc.usage_unknown}")
        self.assertIn(decision.kind, {"tool_call", "action_chunk", "batch_ready", "review_required"})
        self.assertTrue(decision.usage.reported)


if __name__ == "__main__":
    unittest.main()
