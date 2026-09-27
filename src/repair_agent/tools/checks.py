"""In-loop configured check execution; commands are config data, never model input.

Verdicts come from the exit code only — output text is never parsed. The source
tree identity is bracketed with tree_hash + git_tree_oid before and after the
checks (LocalValidator precedent, validation/local.py), so a check that mutates
tracked or untracked non-ignored state blocks the worker instead of passing.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from ..config import CheckSpec, ToolLimits
from ..domain import ToolStatus, canonical_json, redact_text
from ..runtime.workspace import WorkspaceError, WorkspaceState, git_tree_oid, tree_hash

_TRUNCATION_MARKER = "…[truncated]"


def _cap_stream(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit] + _TRUNCATION_MARKER, True


class CheckTools:
    """Execute configured checks by name; the verdict is the exit code, nothing else."""

    def __init__(self, workspace: WorkspaceState, specs: tuple[CheckSpec, ...], prefix: tuple[str, ...], limits: ToolLimits) -> None:
        self.workspace = workspace
        self.specs = specs
        self.prefix = prefix
        self.limits = limits

    def run_checks(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        if not self.specs:
            return ToolStatus.UNSUPPORTED, None, (), {}, False, "no checks are configured"
        names = arguments.get("names")
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            return ToolStatus.ERROR, None, (), {}, False, "names must be a list of check names"
        if not names:
            return ToolStatus.ERROR, None, (), {}, False, "names must not be empty"
        by_name = {spec.name: spec for spec in self.specs}
        unknown = sorted({name for name in names if name not in by_name})
        if unknown:
            return ToolStatus.ERROR, None, (), {}, False, "unknown checks: " + ", ".join(unknown)
        deadline = arguments.get("_deadline")
        try:
            before = tree_hash(self.workspace.root)
            before_oid = git_tree_oid(self.workspace.root)
        except (OSError, WorkspaceError) as exc:
            return ToolStatus.ERROR, None, (), {}, False, f"identity unavailable: {exc}"
        entries: list[dict[str, Any]] = []
        streams: list[tuple[str, str]] = []
        for name in names:  # deterministic: in the order the caller listed the names
            entry, raw_streams = self._run_one(by_name[name], deadline)
            entries.append(entry)
            streams.append(raw_streams)
        tail_limit = max(256, (self.limits.max_output_chars - 4096) // (2 * len(entries)))
        content = self._build_content(entries, streams, tail_limit, before, before_oid, prefix_applied=bool(self.prefix))
        try:
            after = tree_hash(self.workspace.root)
            after_oid = git_tree_oid(self.workspace.root)
        except (OSError, WorkspaceError) as exc:
            return ToolStatus.ERROR, content, (), {}, False, f"could not verify source tree after checks: {exc}"
        if before != after or before_oid != after_oid:
            # tracked change or untracked non-ignored write: verdicts stay in content, worker is blocked
            content["integrity"] = {"before_tree_hash": before, "after_tree_hash": after, "before_git_tree_oid": before_oid, "after_git_tree_oid": after_oid, "verified": False}
            return ToolStatus.ERROR, content, (), {}, False, (
                f"check integrity mismatch: source tree changed during run_checks (before={before[:8]}… after={after[:8]}…)"
            )
        content["integrity"] = {"before_tree_hash": before, "after_tree_hash": after, "before_git_tree_oid": before_oid, "after_git_tree_oid": after_oid, "verified": True}
        return ToolStatus.OK, content, (), {}, True, None

    def _build_content(self, entries: list[dict[str, Any]], streams: list[tuple[str, str]], tail_limit: int, before: str, before_oid: str, *, prefix_applied: bool) -> dict[str, Any]:
        integrity = {"before_tree_hash": before, "after_tree_hash": "", "before_git_tree_oid": before_oid, "after_git_tree_oid": "", "verified": False}
        while True:
            checks = []
            for entry, raw in zip(entries, streams):
                item = dict(entry)
                stdout, stdout_cut = _cap_stream(raw[0], tail_limit)
                stderr, stderr_cut = _cap_stream(raw[1], tail_limit)
                item["stdout_tail"] = stdout
                item["stderr_tail"] = stderr
                item["truncated_streams"] = stdout_cut or stderr_cut
                checks.append(item)
            content = {"checks": checks, "integrity": integrity, "command_prefix_applied": prefix_applied}
            if tail_limit <= 0 or len(canonical_json(content)) < self.limits.max_output_chars:
                return content
            tail_limit //= 2

    def _run_one(self, spec: CheckSpec, deadline: Any) -> tuple[dict[str, Any], tuple[str, str]]:
        started = time.monotonic()
        remaining: float | None = None
        if deadline is not None:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                # never silently skipped: recorded as INFRA_FAIL before execution
                return ({"name": spec.name, "verdict": "INFRA_FAIL", "returncode": None, "elapsed_ms": 0, "timeout_seconds": spec.timeout_seconds, "error": "deadline exhausted before execution"}, ("", ""))
        effective_timeout = spec.timeout_seconds if remaining is None else min(spec.timeout_seconds, remaining)
        argv = [*self.prefix, *spec.argv]
        try:
            result = subprocess.run(argv, cwd=self.workspace.root, capture_output=True, text=True, shell=False, check=False, timeout=effective_timeout)
        except subprocess.TimeoutExpired as exc:
            return ({"name": spec.name, "verdict": "INFRA_FAIL", "returncode": None, "elapsed_ms": int((time.monotonic() - started) * 1000), "timeout_seconds": spec.timeout_seconds, "error": "timeout"},
                    (redact_text(str(exc.stdout or "")), redact_text(str(exc.stderr or ""))))
        except OSError as exc:
            return ({"name": spec.name, "verdict": "INFRA_FAIL", "returncode": None, "elapsed_ms": int((time.monotonic() - started) * 1000), "timeout_seconds": spec.timeout_seconds, "error": redact_text(str(exc))},
                    ("", ""))
        verdict = "PASS" if result.returncode == 0 else "FAIL"
        return ({"name": spec.name, "verdict": verdict, "returncode": result.returncode, "elapsed_ms": int((time.monotonic() - started) * 1000), "timeout_seconds": spec.timeout_seconds, "error": None},
                (redact_text(result.stdout or ""), redact_text(result.stderr or "")))
