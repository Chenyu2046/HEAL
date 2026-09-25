"""Read-only source tools. Text search is deliberately not advertised as C++ semantics."""

from __future__ import annotations

import subprocess
import os
from pathlib import Path
from typing import Any, Iterable

from ..domain import ToolStatus
from ..runtime.workspace import WorkspaceError, WorkspaceState, file_hash


class SourceTools:
    def __init__(self, workspace: WorkspaceState, *, max_file_bytes: int, max_output_chars: int, max_search_results: int) -> None:
        self.workspace = workspace
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars
        self.max_search_results = max_search_results

    def read_file(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        relative = str(arguments.get("path", ""))
        path = self.workspace.resolve(relative)
        if not path.is_file():
            return ToolStatus.ERROR, None, (relative,), {}, False, f"file not found: {relative}"
        size = path.stat().st_size
        content_hash = file_hash(path)
        raw = path.read_bytes()
        if size > self.max_file_bytes:
            return ToolStatus.TRUNCATED, None, (relative,), {relative: content_hash}, False, f"file exceeds limit: {size} bytes"
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        start = max(1, int(arguments.get("start_line", 1)))
        end_arg = arguments.get("end_line")
        end = int(end_arg) if end_arg is not None else len(lines)
        selected = "".join(lines[start - 1 : end])
        max_chars = min(self.max_output_chars, max(1, int(arguments.get("max_chars", self.max_output_chars))))
        if len(selected) > max_chars:
            return ToolStatus.TRUNCATED, selected[:max_chars], (relative,), {relative: content_hash}, False, "output limit reached"
        self.workspace.mark_read((relative,))
        return ToolStatus.OK, {"path": relative, "start_line": start, "end_line": end, "text": selected, "content_hash": content_hash}, (relative,), {relative: content_hash}, True, None

    def search_code(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        query = str(arguments.get("query", ""))
        if not query:
            return ToolStatus.ERROR, None, (), {}, False, "query is required"
        raw_paths = arguments.get("paths") or ["."]
        try:
            paths = [self.workspace.resolve(str(item)) for item in raw_paths]
        except WorkspaceError as exc:
            return ToolStatus.ERROR, None, (), {}, False, str(exc)
        results: list[dict[str, Any]] = []
        touched: list[str] = []
        for target in paths:
            candidates = [target] if target.is_file() else sorted(target.rglob("*"))
            for candidate in candidates:
                if not candidate.is_file() or candidate.is_symlink() or ".git" in candidate.parts:
                    continue
                relative = candidate.relative_to(self.workspace.root).as_posix()
                if candidate.stat().st_size > self.max_file_bytes:
                    continue
                try:
                    lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line_number, line in enumerate(lines, 1):
                    if query in line:
                        results.append({"path": relative, "line": line_number, "text": line})
                        touched.append(relative)
                        if len(results) >= min(self.max_search_results, int(arguments.get("max_results", self.max_search_results))):
                            self.workspace.mark_read(touched)
                            return ToolStatus.PARTIAL, results, tuple(sorted(set(touched))), self.workspace.hash_paths(touched), False, "search result limit reached"
        self.workspace.mark_read(touched)
        if not results:
            return ToolStatus.EMPTY, [], (), {}, True, "no textual matches; this does not prove semantic absence"
        return ToolStatus.OK, results, tuple(sorted(set(touched))), self.workspace.hash_paths(touched), True, None

    def unsupported_navigation(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        return ToolStatus.UNSUPPORTED, None, (), {}, False, "clangd/compile_commands semantic navigation is not configured"

    def git_diff(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        try:
            result = subprocess.run(
                ["git", "diff", "--binary", "--no-ext-diff", "--no-color", "--"],
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all", "--"],
                cwd=self.workspace.root,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolStatus.ERROR, None, (), {}, False, f"git diff unavailable: {exc}"
        if result.returncode != 0 or status.returncode != 0:
            return ToolStatus.ERROR, None, (), {}, False, (result.stderr or status.stderr).strip() or "git diff failed"
        untracked = [line[3:] for line in status.stdout.splitlines() if line.startswith("?? ")]
        if not result.stdout and not untracked:
            return ToolStatus.EMPTY, {"diff": "", "untracked_files": []}, (), {}, True, None
        diff_parts = [result.stdout]
        for relative in untracked:
            try:
                untracked_diff = subprocess.run(
                    ["git", "diff", "--no-index", "--binary", "--no-color", "--", os.devnull, relative],
                    cwd=self.workspace.root,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return ToolStatus.ERROR, None, tuple(untracked), {}, False, f"untracked diff unavailable: {exc}"
            if untracked_diff.returncode not in (0, 1):
                return ToolStatus.ERROR, None, tuple(untracked), {}, False, untracked_diff.stderr.strip() or "untracked diff failed"
            diff_parts.append(untracked_diff.stdout)
        return ToolStatus.OK, {"diff": "\n".join(part for part in diff_parts if part), "untracked_files": untracked}, tuple(untracked), {}, True, None
