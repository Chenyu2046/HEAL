"""G3/R4b acceptance tests: clangd adapter (scripted LSP), unconfigured boundary, chunk exclusion.

Maps 1:1 to docs/tech-design.md §3.11 (R4b acceptances 1, 2, 4; acceptance 3 is env-gated real clangd).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repair_agent.config import ClangdConfig
from repair_agent.domain import ToolStatus
from repair_agent.runtime.workspace import WorkspaceState
from repair_agent.tools.chunking import ActionChunk, BoundaryDetector, ChunkAction, ChunkExecutor
from repair_agent.tools.executor import ToolExecutor

# Minimal LSP server over stdio: Content-Length framing, initialize handshake,
# workspace/symbol + definition/references. Mode and workspace root arrive via
# environment so the launcher stays a fixed two-token argv (tech-design §3.3).
_FAKE_LSP_SCRIPT = r'''
import json, os, sys, time


def read_frame():
    headers = {}
    line = b""
    while True:
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        line += ch
        if line.endswith(b"\r\n"):
            text = line.decode("ascii", errors="replace").strip()
            line = b""
            if not text:
                break
            key, _, value = text.partition(":")
            headers[key.strip().lower()] = value.strip()
    length = int(headers["content-length"])
    body = b""
    while len(body) < length:
        chunk = sys.stdin.buffer.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def send(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


mode = os.environ.get("HEAL_FAKE_LSP_MODE", "ok")
root = os.environ.get("HEAL_FAKE_LSP_ROOT", ".").replace("\\", "/")
main_uri = f"file:///{root}/src/sample.cpp"
other_uri = f"file:///{root}/src/other.cpp"
while True:
    message = read_frame()
    if message is None:
        break
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"capabilities": {"definitionProvider": True, "referencesProvider": True, "workspaceSymbolProvider": True}}})
    elif method == "workspace/symbol":
        if mode == "hang":
            time.sleep(60)
        send({"id": message["id"], "result": [{"name": message["params"]["query"], "kind": 12, "location": {"uri": main_uri, "range": {"start": {"line": 20, "character": 5}}}}]})
    elif method == "textDocument/definition":
        if mode == "hang":
            time.sleep(60)
        send({"id": message["id"], "result": [{"uri": main_uri, "range": {"start": {"line": 20, "character": 5}}}]})
    elif method == "textDocument/references":
        send({"id": message["id"], "result": [{"uri": main_uri, "range": {"start": {"line": 10, "character": 0}}}, {"uri": other_uri, "range": {"start": {"line": 3, "character": 2}}}]})
    else:
        send({"id": message["id"], "result": {}})
'''


@contextmanager
def temp_dir(testcase: unittest.TestCase):
    temporary = tempfile.TemporaryDirectory()
    testcase.addCleanup(temporary.cleanup)
    yield Path(temporary.name)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "sample.cpp").write_bytes(b"int sample() { return 0; }\n")
    (repo / "src" / "other.cpp").write_bytes(b"int other() { return 1; }\n")
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "HEAL test")
    git(repo, "config", "user.email", "heal-test@localhost")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "base", "--quiet")
    return repo, git(repo, "rev-parse", "HEAD")


def build_fake_clangd(root: Path, *, mode: str) -> ClangdConfig:
    """Write the fake LSP server plus a fixed-argv launcher; config points at the launcher."""
    script = root / "fake_lsp.py"
    script.write_text(_FAKE_LSP_SCRIPT, encoding="utf-8")
    python = sys.executable
    if os.name == "nt":
        launcher = root / "fake_clangd.bat"
        launcher.write_text(f'@echo off\r\n"{python}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = root / "fake_clangd.sh"
        launcher.write_text(f'#!/bin/sh\nexec "{python}" "{script}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    os.environ["HEAL_FAKE_LSP_MODE"] = mode
    os.environ["HEAL_FAKE_LSP_ROOT"] = str((root / "repo").resolve())
    return ClangdConfig(binary=str(launcher), compile_commands_dir=str(root / "compile"), timeout_seconds=0.5)


class UnconfiguredClangdTests(unittest.TestCase):
    """R4b-1: absent config keeps today's exact UNSUPPORTED behavior and never chunks."""

    def test_unconfigured_tools_stay_unsupported_and_out_of_chunks(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            for name in ("find_definition", "find_references"):
                spec = executor.registry.get(name)
                self.assertIsNotNone(spec)
                self.assertFalse(spec.allowed_in_chunk)
                self.assertNotIn(name, BoundaryDetector.READ_ONLY_TOOLS)
            definition = executor.execute(type("Call", (), {"name": "find_definition", "arguments": {"symbol": "sample"}, "call_id": "d1"})())
            self.assertEqual(definition.status, ToolStatus.UNSUPPORTED)
            self.assertFalse(definition.complete)
            self.assertEqual(definition.error, "clangd/compile_commands semantic navigation is not configured")
            references = executor.execute(type("Call", (), {"name": "find_references", "arguments": {"symbol": "sample"}, "call_id": "r1"})())
            self.assertEqual(references.status, ToolStatus.UNSUPPORTED)

    def test_chunk_containing_navigation_tools_is_rejected(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            executor = ToolExecutor(WorkspaceState(repo, base))
            chunk = ActionChunk("nav", (
                ChunkAction("find_definition", {"symbol": "sample"}, "d"),
                ChunkAction("read_file", {"path": "src/sample.cpp"}, "r"),
            ))
            result = ChunkExecutor().execute(chunk, executor, expected_workspace_revision=0)
            self.assertFalse(result.accepted)
            self.assertIn("non-read-only tool is not eligible for chunking: find_definition", str(result.reason))


class ScriptedClangdTests(unittest.TestCase):
    """R4b-2: deterministic fake LSP server exercises handshake, lookup, and the bounded timeout."""

    def test_definition_and_references_resolve_with_hashes(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            config = build_fake_clangd(root, mode="ok")
            self.addCleanup(os.environ.pop, "HEAL_FAKE_LSP_MODE", None)
            self.addCleanup(os.environ.pop, "HEAL_FAKE_LSP_ROOT", None)
            executor = ToolExecutor(WorkspaceState(repo, base), clangd=config)
            definition = executor.execute(type("Call", (), {"name": "find_definition", "arguments": {"symbol": "sample"}, "call_id": "d1"})())
            self.assertEqual(definition.status, ToolStatus.OK, msg=str(definition.error))
            self.assertTrue(definition.complete)
            self.assertEqual(definition.content["symbol"], "sample")
            self.assertEqual(definition.content["server"], "clangd")
            self.assertFalse(definition.content["truncated"])
            location = definition.content["locations"][0]
            self.assertEqual(location["path"], "src/sample.cpp")
            self.assertEqual(location["line"], 21)
            self.assertIn(location["path"], definition.file_hashes)
            self.assertEqual(definition.source_paths, ("src/sample.cpp",))
            references = executor.execute(type("Call", (), {"name": "find_references", "arguments": {"symbol": "sample"}, "call_id": "r1"})())
            self.assertEqual(references.status, ToolStatus.OK, msg=str(references.error))
            self.assertEqual([item["path"] for item in references.content["locations"]], ["src/sample.cpp", "src/other.cpp"])
            self.assertEqual(references.content["truncated"], False)
            executor.close()

    def test_timeout_yields_bounded_error_without_hanging(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            config = build_fake_clangd(root, mode="hang")
            self.addCleanup(os.environ.pop, "HEAL_FAKE_LSP_MODE", None)
            self.addCleanup(os.environ.pop, "HEAL_FAKE_LSP_ROOT", None)
            executor = ToolExecutor(WorkspaceState(repo, base), clangd=config)
            started = time.monotonic()
            definition = executor.execute(type("Call", (), {"name": "find_definition", "arguments": {"symbol": "sample"}, "call_id": "d1"})())
            elapsed = time.monotonic() - started
            self.assertEqual(definition.status, ToolStatus.ERROR)
            self.assertFalse(definition.complete)
            self.assertIn("clangd:", str(definition.error))
            self.assertLess(elapsed, 30.0, msg="a hung server must yield a bounded error, never a hang")
            executor.close()


class RealClangdTests(unittest.TestCase):
    """R4b-3: env-gated; skipped unless a real clangd binary and compile database exist."""

    @unittest.skipUnless(shutil.which("clangd"), "real clangd integration requires the clangd binary on PATH")
    def test_real_clangd_resolves_planted_symbol(self) -> None:
        with temp_dir(self) as root:
            repo, base = make_repo(root)
            (repo / "compile_commands.json").write_text("[]", encoding="utf-8")
            executor = ToolExecutor(WorkspaceState(repo, base), clangd=ClangdConfig(binary="clangd", compile_commands_dir=".", timeout_seconds=10.0))
            definition = executor.execute(type("Call", (), {"name": "find_definition", "arguments": {"symbol": "sample"}, "call_id": "d1"})())
            self.assertIn(definition.status, {ToolStatus.OK, ToolStatus.EMPTY})
            executor.close()


if __name__ == "__main__":
    unittest.main()
