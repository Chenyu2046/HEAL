"""Read-only source tools. Text search is deliberately not advertised as C++ semantics."""

from __future__ import annotations

import subprocess
import os
import time
from pathlib import Path
from typing import Any, Iterable

from ..context import ContextCache, FileContextEntry, SearchContextEntry
from ..domain import ToolStatus
from ..runtime.workspace import WorkspaceError, WorkspaceState, file_hash


class SourceTools:
    def __init__(self, workspace: WorkspaceState, *, max_file_bytes: int, max_output_chars: int, max_search_results: int, cache: ContextCache | None = None) -> None:
        self.workspace = workspace
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars
        self.max_search_results = max_search_results
        self.cache = cache

    def read_file(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        relative = str(arguments.get("path", ""))
        if self.cache is not None:
            hit = self.cache.files.get(relative, self.workspace.observed_hashes.get(relative))
            if hit is not None:
                return self._replay_read(arguments, relative, hit)
        path = self.workspace.resolve(relative)
        if not path.is_file():
            return ToolStatus.ERROR, None, (relative,), {}, False, f"file not found: {relative}"
        size = path.stat().st_size
        if size > self.max_file_bytes:
            return ToolStatus.TRUNCATED, None, (relative,), {}, False, f"file exceeds limit: {size} bytes"
        content_hash = file_hash(path)
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        start = max(1, int(arguments.get("start_line", 1)))
        end_arg = arguments.get("end_line")
        end = int(end_arg) if end_arg is not None else len(lines)
        selected = "".join(lines[start - 1 : end])
        max_chars = min(self.max_output_chars, max(1, int(arguments.get("max_chars", self.max_output_chars))))
        if self.cache is not None:
            # 刚读到的内容就是当前 hash;mark_read/refresh 会把它维护进
            # observed_hashes,后续命中按该基线做纯内存比较。
            self.cache.files.put(relative, text=text, content_hash=content_hash, observed_hash=content_hash)
        if len(selected) > max_chars:
            return ToolStatus.TRUNCATED, selected[:max_chars], (relative,), {relative: content_hash}, False, "output limit reached"
        self.workspace.mark_read((relative,))
        if not selected:
            return ToolStatus.EMPTY, {"path": relative, "start_line": start, "end_line": end, "text": "", "content_hash": content_hash}, (relative,), {relative: content_hash}, True, "selected range is empty"
        return ToolStatus.OK, {"path": relative, "start_line": start, "end_line": end, "text": selected, "content_hash": content_hash}, (relative,), {relative: content_hash}, True, None

    def _replay_read(self, arguments: dict[str, Any], relative: str, entry: FileContextEntry) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        """Replay a slice from the cached full text; byte-identical to a direct read.

        命中条件已保证 refresh 刚重新 hash 过该文件且 hash 未变,因此跳过
        resolve/size 检查、内容读取与 mark_read 不影响证据新鲜度与 observed
        状态(文件已在 observed_hashes)。
        """
        lines = entry.text.splitlines(keepends=True)
        start = max(1, int(arguments.get("start_line", 1)))
        end_arg = arguments.get("end_line")
        end = int(end_arg) if end_arg is not None else len(lines)
        selected = "".join(lines[start - 1 : end])
        max_chars = min(self.max_output_chars, max(1, int(arguments.get("max_chars", self.max_output_chars))))
        hashes = {relative: entry.content_hash}
        if len(selected) > max_chars:
            return ToolStatus.TRUNCATED, selected[:max_chars], (relative,), hashes, False, "output limit reached"
        if not selected:
            return ToolStatus.EMPTY, {"path": relative, "start_line": start, "end_line": end, "text": "", "content_hash": entry.content_hash}, (relative,), hashes, True, "selected range is empty"
        return ToolStatus.OK, {"path": relative, "start_line": start, "end_line": end, "text": selected, "content_hash": entry.content_hash}, (relative,), hashes, True, None

    def _effective_max_results(self, arguments: dict[str, Any]) -> int | None:
        """Mirror of the in-loop limit expression; do not change one side alone.

        ToolExecutor 路径已由 schema 校验为 integer>=1,except 分支只兜住直接
        handler 调用的非法参数;返回 None 表示该次调用不读写缓存。
        """
        raw = arguments.get("max_results", self.max_search_results)
        try:
            return min(self.max_search_results, int(raw))
        except (TypeError, ValueError):
            return None

    def search_code(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        query = str(arguments.get("query", ""))
        if not query:
            return ToolStatus.ERROR, None, (), {}, False, "query is required"
        deadline = float(arguments["_deadline"]) if arguments.get("_deadline") is not None else None
        raw_paths = arguments.get("paths") or ["."]
        scope = tuple(str(item) for item in raw_paths)
        limit = self._effective_max_results(arguments) if self.cache is not None else None
        if limit is not None:
            hit = self.cache.searches.get(query, scope, limit, self.workspace.observed_hashes)
            if hit is not None:
                return hit.status, [dict(item) for item in hit.results], hit.touched, dict(hit.file_hashes), True, hit.error
        try:
            candidates = self.workspace.iter_files(scope)
        except WorkspaceError as exc:
            return ToolStatus.ERROR, None, (), {}, False, str(exc)
        results: list[dict[str, Any]] = []
        touched: list[str] = []

        def timed_out() -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
            self.workspace.mark_read(touched)
            return ToolStatus.PARTIAL, results, tuple(sorted(set(touched))), self.workspace.hash_paths(touched), False, "search stopped at the workspace deadline"

        for relative, candidate in candidates:
            if deadline is not None and time.monotonic() >= deadline:
                return timed_out()
            if not candidate.is_file() or candidate.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                if deadline is not None and time.monotonic() >= deadline:
                    return timed_out()
                if query in line:
                    results.append({"path": relative, "line": line_number, "text": line})
                    touched.append(relative)
                    if len(results) >= min(self.max_search_results, int(arguments.get("max_results", self.max_search_results))):
                        self.workspace.mark_read(touched)
                        return ToolStatus.PARTIAL, results, tuple(sorted(set(touched))), self.workspace.hash_paths(touched), False, "search result limit reached"
        self.workspace.mark_read(touched)
        if not results:
            # EMPTY 与 PARTIAL 同等保守,不缓存:EMPTY 没有 touched 文件可做
            # hash 校验,edit_file 在 scope 内引入新匹配后回放会变成过期的
            # complete=True,违反 fail-closed。
            return ToolStatus.EMPTY, [], (), {}, True, "no textual matches; this does not prove semantic absence"
        hashes = self.workspace.hash_paths(touched)
        if limit is not None:
            self.cache.searches.put(query, scope, limit, SearchContextEntry(ToolStatus.OK, tuple(dict(item) for item in results), tuple(sorted(set(touched))), dict(hashes), None))
        return ToolStatus.OK, results, tuple(sorted(set(touched))), hashes, True, None

    def unsupported_navigation(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        return ToolStatus.UNSUPPORTED, None, (), {}, False, "clangd/compile_commands semantic navigation is not configured"

    def git_diff(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        deadline = float(arguments["_deadline"]) if arguments.get("_deadline") is not None else None
        def run_git(argv: list[str]) -> subprocess.CompletedProcess[str]:
            remaining = deadline - time.monotonic() if deadline is not None else 30.0
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
            return subprocess.run(argv, cwd=self.workspace.root, text=True, capture_output=True, check=False, timeout=min(30.0, remaining))
        try:
            result = run_git(["git", "diff", "--binary", "--no-ext-diff", "--no-color", "--"])
            status = run_git(["git", "status", "--porcelain", "--untracked-files=all", "--"])
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
                untracked_diff = run_git(["git", "diff", "--no-index", "--binary", "--no-color", "--", os.devnull, relative])
            except (OSError, subprocess.TimeoutExpired) as exc:
                return ToolStatus.ERROR, None, tuple(untracked), {}, False, f"untracked diff unavailable: {exc}"
            if untracked_diff.returncode not in (0, 1):
                return ToolStatus.ERROR, None, tuple(untracked), {}, False, untracked_diff.stderr.strip() or "untracked diff failed"
            diff_parts.append(untracked_diff.stdout)
        return ToolStatus.OK, {"diff": "\n".join(part for part in diff_parts if part), "untracked_files": untracked}, tuple(untracked), {}, True, None
