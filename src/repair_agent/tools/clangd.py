"""clangd-over-stdio semantic navigation adapter (R4b, tech-design §3.3).

One clangd per worker workspace, spawned lazily with a fixed argv; every
handshake step and request is individually deadline-bounded through a reader
thread and a timeout-bounded queue, so a hung server yields a bounded ERROR
observation, never a hang. Convenience, not containment: this adapter assumes a
trusted clangd binary from configuration — the worktree remains an edit
isolation mechanism, not a security sandbox.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..config import ClangdConfig
from ..domain import ToolStatus, redact_text
from ..runtime.workspace import WorkspaceError, WorkspaceState

_TRIPLET = tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]


class ClangdNavigation:
    """LSP-over-stdio client; handlers return the standard handler 6-tuple."""

    def __init__(self, workspace: WorkspaceState, config: ClangdConfig, *, max_output_chars: int, max_locations: int = 50) -> None:
        self.workspace = workspace
        self.config = config
        self.max_output_chars = max_output_chars
        self.max_locations = max_locations
        self._process: subprocess.Popen | None = None
        self._responses: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1

    # -- process lifecycle -------------------------------------------------

    def _spawn(self, deadline: float | None) -> str | None:
        """Lazily spawn the server and complete the initialize handshake; None on success."""
        if self._process is not None and self._process.poll() is None:
            return None
        binary = self.config.binary
        if not Path(binary).exists() and shutil.which(binary) is None:
            return f"clangd binary not found: {redact_text(binary)}"
        argv = [binary, f"--compile-commands-dir={self.config.compile_commands_dir}"]
        try:
            self._process = subprocess.Popen(
                argv, cwd=self.workspace.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False,
            )
        except OSError as exc:
            self._process = None
            return f"could not start clangd: {redact_text(str(exc))}"
        self._responses = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        capabilities = {"textDocument/definition": {"scheme": "file"}}
        error = self._request("initialize", {"processId": None, "rootUri": self._file_uri(self.workspace.root), "capabilities": capabilities}, deadline, expect_result=True)
        if error is not None:
            self.close()
            return error
        self._notify("initialized", {})
        return None

    def _read_loop(self) -> None:
        process = self._process
        stream = process.stdout
        try:
            while process.poll() is None:
                length = None
                line = b""
                while True:
                    char = stream.read(1)
                    if not char:
                        self._responses.put(None)
                        return
                    line += char
                    if line.endswith(b"\r\n"):
                        text = line.decode("ascii", errors="replace").strip()
                        line = b""
                        if not text:
                            break
                        key, _, value = text.partition(":")
                        if key.strip().lower() == "content-length":
                            length = int(value.strip())
                if length is None:
                    self._responses.put(None)
                    return
                body = stream.read(length)
                if len(body) < length:
                    self._responses.put(None)
                    return
                try:
                    self._responses.put(json.loads(body.decode("utf-8")))
                except ValueError:
                    self._responses.put(None)
                    return
        except (OSError, ValueError):
            # OSError: pipe broken; ValueError: read raced close() during teardown
            self._responses.put(None)

    def _remaining(self, deadline: float | None) -> float:
        if deadline is None:
            return self.config.timeout_seconds
        return min(self.config.timeout_seconds, deadline - time.monotonic())

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(frame)
        self._process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any], deadline: float | None, *, expect_result: bool) -> str | None:
        """Send a request and consume responses until the matching id arrives."""
        request_id = self._next_id
        self._next_id += 1
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except (OSError, ValueError) as exc:
            return f"clangd: write failed: {redact_text(str(exc))}"
        timeout_budget = self._remaining(deadline)
        if timeout_budget <= 0:
            return "clangd: deadline exhausted before the request"
        while timeout_budget > 0:
            try:
                message = self._responses.get(timeout=timeout_budget)
            except queue.Empty:
                return f"clangd: timed out after {self.config.timeout_seconds:g}s waiting for {method}"
            if message is None or not isinstance(message, dict):
                return f"clangd: server closed or sent a malformed frame during {method}"
            if message.get("id") != request_id:
                timeout_budget = self._remaining(deadline)
                continue
            if expect_result and "result" not in message:
                return f"clangd: {method} returned an error response"
            return None
        return f"clangd: timed out after {self.config.timeout_seconds:g}s waiting for {method}"

    def _call(self, method: str, params: dict[str, Any], deadline: float | None) -> tuple[Any, str | None]:
        """Send a request and return (result payload, error)."""
        request_id = self._next_id
        self._next_id += 1
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except (OSError, ValueError) as exc:
            return None, f"clangd: write failed: {redact_text(str(exc))}"
        while True:
            timeout_budget = self._remaining(deadline)
            if timeout_budget <= 0:
                return None, f"clangd: deadline exhausted waiting for {method}"
            try:
                message = self._responses.get(timeout=timeout_budget)
            except queue.Empty:
                return None, f"clangd: timed out after {self.config.timeout_seconds:g}s waiting for {method}"
            if message is None or not isinstance(message, dict):
                return None, f"clangd: server closed or sent a malformed frame during {method}"
            if message.get("id") != request_id:
                continue
            if "error" in message:
                return None, f"clangd: {method} returned an error response"
            return message.get("result"), None

    # -- navigation handlers -----------------------------------------------

    def find_definition(self, symbol: str, *, deadline: float | None = None) -> _TRIPLET:
        return self._navigate(symbol, deadline, want_references=False)

    def find_references(self, symbol: str, *, deadline: float | None = None) -> _TRIPLET:
        return self._navigate(symbol, deadline, want_references=True)

    def _navigate(self, symbol: str, deadline: float | None, *, want_references: bool) -> _TRIPLET:
        if not symbol.strip():
            return ToolStatus.ERROR, None, (), {}, False, "clangd: symbol must be a non-empty string"
        error = self._spawn(deadline)
        if error is not None:
            return ToolStatus.ERROR, None, (), {}, False, error
        # LSP resolves names through workspace/symbol; the first match fixes the position.
        matches, error = self._call("workspace/symbol", {"query": symbol}, deadline)
        if error is not None:
            return ToolStatus.ERROR, None, (), {}, False, error
        if not isinstance(matches, list) or not matches:
            return ToolStatus.EMPTY, {"symbol": symbol, "locations": [], "truncated": False, "server": "clangd"}, (), {}, True, "clangd: no symbol matched the query"
        anchor = self._location_of(matches[0])
        if anchor is None:
            return ToolStatus.ERROR, None, (), {}, False, "clangd: workspace/symbol returned an unusable location"
        method = "textDocument/references" if want_references else "textDocument/definition"
        params = {
            "textDocument": {"uri": anchor[0]},
            "position": anchor[1],
        }
        if want_references:
            params["context"] = {"includeDeclaration": False}
        result, error = self._call(method, params, deadline)
        if error is not None:
            return ToolStatus.ERROR, None, (), {}, False, error
        raw_locations = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
        return self._payload(symbol, raw_locations)

    @staticmethod
    def _location_of(match: Any) -> tuple[str, dict[str, int]] | None:
        if not isinstance(match, dict):
            return None
        location = match.get("location")
        if not isinstance(location, dict):
            return None
        start = location.get("range", {}).get("start") if isinstance(location.get("range"), dict) else None
        if not isinstance(start, dict) or not isinstance(location.get("uri"), str):
            return None
        return location["uri"], {"line": int(start.get("line", 0)), "character": int(start.get("character", 0))}

    def _payload(self, symbol: str, raw_locations: list[Any]) -> _TRIPLET:
        locations: list[dict[str, Any]] = []
        paths: list[str] = []
        truncated = False
        for raw in raw_locations:
            if not isinstance(raw, dict) or not isinstance(raw.get("uri"), str):
                continue
            relative = self._relative_of(raw["uri"])
            if relative is None:
                continue  # escaping/protected paths are dropped, not followed
            start = raw.get("range", {}).get("start", {}) if isinstance(raw.get("range"), dict) else {}
            paths.append(relative)
            locations.append({
                "path": relative,
                "line": int(start.get("line", 0)) + 1,  # LSP lines are 0-based
                "character": int(start.get("character", 0)),
            })
            if len(locations) >= self.max_locations:
                truncated = len(raw_locations) > self.max_locations
                break
        hashes = self.workspace.hash_paths(paths)
        for location in locations:
            location["content_hash"] = hashes.get(location["path"], "")
        content = {"symbol": redact_text(symbol), "locations": locations, "truncated": truncated, "server": "clangd"}
        if not locations:
            return ToolStatus.EMPTY, content, (), {}, True, "clangd: no usable in-workspace locations"
        return ToolStatus.OK, content, tuple(sorted(set(paths))), hashes, True, None

    def _relative_of(self, uri: str) -> str | None:
        try:
            absolute = Path(url2pathname(urlparse(uri).path)).resolve()
            relative = absolute.relative_to(self.workspace.root).as_posix()
        except (ValueError, OSError, RuntimeError):
            return None
        try:
            self.workspace.resolve(relative)
        except WorkspaceError:
            return None
        return relative

    @staticmethod
    def _file_uri(path: Path) -> str:
        return path.resolve().as_uri()

    def close(self) -> None:
        """Reap the server process tree and close owned pipes; idempotent."""
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            if os.name == "nt":
                # a .bat launcher forks a grandchild; terminate the whole tree so no
                # orphan keeps the workspace as its current directory
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        for stream in (process.stdout,):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
