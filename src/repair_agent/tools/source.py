"""Read-only source tools. Text search is deliberately not advertised as C++ semantics."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

from ..context import ContextCache, FileContextEntry, SearchContextEntry, SymbolContextEntry
from ..domain import ToolStatus, canonical_json, issue_file
from ..runtime.workspace import WorkspaceError, WorkspaceState, file_hash
from .symbols import SymbolDecl, MAX_SYMBOL_DECLS, scan_symbols

SYMBOL_SAMPLE_LINE_CHARS = 240


@lru_cache(maxsize=512)
def _exact_identifier_pattern(token: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])")


def _contains_identifier(token: str, text: str) -> bool:
    if not token:
        return False
    return bool(_exact_identifier_pattern(token).search(text))


@dataclass(frozen=True)
class SearchRankingContext:
    """Heuristic ranking hints from the current batch's issues (方案 §7).

    All fields are optional; an empty context simply adds no bonus. The score is
    a lexical heuristic (identifier/module/trace-token hits), NOT semantic
    relevance. Computed over match lines only so the ripgrep backend never has
    to read whole files for scoring.
    """

    files: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    trace_tokens: tuple[str, ...] = ()

    @classmethod
    def from_issues(cls, issues: Iterable[Any]) -> "SearchRankingContext":
        files: set[str] = set()
        modules: set[str] = set()
        symbols: set[str] = set()
        tokens: list[str] = []
        for issue in issues:
            file_value = issue_file(issue)
            if file_value:
                files.add(file_value)
                modules.add(file_value.replace("\\", "/").split("/")[0])
            module = getattr(issue, "module", None)
            if module:
                modules.add(str(module))
            symbol = getattr(issue, "symbol", None)
            if symbol:
                symbols.add(str(symbol))
            for entry in getattr(issue, "analysis_trace", ()) or ():
                tokens.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", str(entry)))
        # trace 词元只做 +1 弱加分,截断到 32 个保持评分成本有界。
        return cls(
            files=tuple(sorted(files)),
            modules=tuple(sorted(modules)),
            symbols=tuple(sorted(symbols)),
            trace_tokens=tuple(dict.fromkeys(tokens))[:32],
        )


class SourceTools:
    def __init__(
        self,
        workspace: WorkspaceState,
        *,
        max_file_bytes: int,
        max_output_chars: int,
        max_search_results: int,
        cache: ContextCache | None = None,
        ranking: SearchRankingContext | None = None,
        search_backend: str = "auto",
    ) -> None:
        self.workspace = workspace
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars
        self.max_search_results = max_search_results
        self.cache = cache
        self.ranking = ranking or SearchRankingContext()
        if search_backend not in {"auto", "rg", "python"}:
            raise ValueError("search_backend must be one of: auto, rg, python")
        self.search_backend = search_backend
        # ripgrep 检测每实例一次(方案 §7)。"rg" 强制后端在二进制缺失时直接失败,
        # 不做静默回退;"auto" 在缺失时回退 Python 扫描器。
        self._rg_path = shutil.which("rg") if search_backend in {"auto", "rg"} else None
        if search_backend == "rg" and self._rg_path is None:
            raise ValueError("search_backend='rg' requested but the ripgrep binary is not available")

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

    def list_symbols(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        """Heuristic lexical C/C++ symbol outline for one file (方案 §4.3/§8).

        词法启发式,不是 clangd 语义导航;命中 SymbolCache 时零内容读取
        (不读文件、不 hash、不重新扫描),机制与 read_file 命中一致。
        """
        relative = str(arguments.get("path", ""))
        if self.cache is not None:
            hit = self.cache.symbols.get(relative, self.workspace.observed_hashes.get(relative))
            if hit is not None:
                return self._replay_symbols(relative, hit)
        path = self.workspace.resolve(relative)
        if not path.is_file():
            return ToolStatus.ERROR, None, (relative,), {}, False, f"file not found: {relative}"
        size = path.stat().st_size
        if size > self.max_file_bytes:
            return ToolStatus.TRUNCATED, None, (relative,), {}, False, f"file exceeds limit: {size} bytes"
        content_hash = file_hash(path)
        text = path.read_bytes().decode("utf-8", errors="replace")
        symbols = scan_symbols(text)
        if self.cache is not None:
            # 与 read_file 相同:刚扫描的符号对应当前 hash,refresh/mark_read
            # 之后按该基线做纯内存比较。
            self.cache.symbols.put(relative, symbols=symbols, content_hash=content_hash, observed_hash=content_hash)
        self.workspace.mark_read((relative,))
        return self._symbol_payload(relative, symbols, content_hash)

    def _replay_symbols(self, relative: str, entry: SymbolContextEntry) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        # 命中条件保证 hash 未变,跳过 resolve/读取/扫描/mark_read 不影响证据新鲜度。
        return self._symbol_payload(relative, entry.symbols, entry.content_hash)

    def _symbol_payload(self, relative: str, symbols: tuple[SymbolDecl, ...], content_hash: str) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        """Bound the symbol output: declaration count cap plus max_output_chars truncation."""
        decls = [
            {"type": item.type, "name": item.name, "signature": item.signature, "start_line": item.start_line, "end_line": item.end_line, "overload_index": item.overload_index}
            for item in symbols
        ]
        truncated = False
        if len(decls) > MAX_SYMBOL_DECLS:
            decls = decls[:MAX_SYMBOL_DECLS]
            truncated = True
        base = {"path": relative, "content_hash": content_hash, "symbols": []}
        # 条数与字符数有界:字符预算按每条序列化长度做一次算术预裁剪,再兜底校验。
        budget = self.max_output_chars - len(canonical_json(base))
        total = 0
        keep = len(decls)
        for index, decl in enumerate(decls):
            total += len(canonical_json(decl)) + 1
            if total > budget:
                keep = index
                break
        if keep < len(decls):
            decls = decls[:keep]
            truncated = True
        content = {"path": relative, "content_hash": content_hash, "symbols": decls}
        while decls and len(canonical_json(content)) > self.max_output_chars:
            decls.pop()
            content["symbols"] = decls
            truncated = True
        hashes = {relative: content_hash}
        if truncated:
            return ToolStatus.TRUNCATED, content, (relative,), hashes, False, "symbol list output limit reached"
        if not decls:
            return ToolStatus.EMPTY, content, (relative,), hashes, True, "no lexical symbols; heuristic lexical structure, not semantic navigation"
        return ToolStatus.OK, content, (relative,), hashes, True, None

    def _effective_max_results(self, arguments: dict[str, Any]) -> int | None:
        """Single source of truth for the search cap: cache key 的 limit 与收集期 cap 同源。

        ToolExecutor 路径已由 schema 校验为 integer>=1,except 分支只兜住直接
        handler 调用的非法参数:返回 None,search_code 据此返回有界 ERROR,
        不抛异常也不猜默认值。
        """
        raw = arguments.get("max_results", self.max_search_results)
        try:
            return min(self.max_search_results, int(raw))
        except (TypeError, ValueError):
            return None

    def search_code(self, arguments: dict[str, Any]) -> tuple[ToolStatus, Any, tuple[str, ...], dict[str, str], bool, str | None]:
        """Identifier-aware lexical search, file-aggregated (方案 §7/§8).

        后端:优先 ripgrep(--json,固定 argv,shell=False,子进程超时=剩余
        deadline);rg 不可用时回退 Python rglob+read_text 扫描器。口径差异
        (刻意保留并在此注明):rg 默认尊重 .gitignore 且跳过隐藏/二进制文件,
        与 tree_hash 的 ``git ls-files --exclude-standard`` 口径一致、以前者为
        准;Python 回退扫描不尊重 .gitignore、也不跳过隐藏文件,两者结果可能
        不同。排序公式是词法启发式(精确标识符/同文件/同模块/符号/trace 词元),
        非语义相关度。
        """
        query = str(arguments.get("query", ""))
        if not query:
            return ToolStatus.ERROR, None, (), {}, False, "query is required"
        deadline = float(arguments["_deadline"]) if arguments.get("_deadline") is not None else None
        raw_paths = arguments.get("paths") or ["."]
        scope = tuple(str(item) for item in raw_paths)
        cap = self._effective_max_results(arguments)
        if cap is None:
            # 直接 handler 调用的非法 max_results:有界 ERROR,fail-closed
            # (ToolExecutor 路径已由 schema 校验挡掉,不会走到这里)。
            return ToolStatus.ERROR, None, (), {}, False, "invalid argument: max_results"
        # 已知权衡:cap 在收集期先到先得生效(按扫描顺序),排序(_score_file)只作用于
        # 已收集命中——遍历序靠后的高相关文件可能整体缺席。升级路径:先做文件级计数
        # 再取样,或 rg 侧用 --max-count 按文件限量(方案 §7)。
        limit = cap if self.cache is not None else None
        if limit is not None:
            hit = self.cache.searches.get(query, scope, limit, self.workspace.observed_hashes)
            if hit is not None:
                content = dict(hit.results[0]) if hit.results else []
                return hit.status, content, hit.touched, dict(hit.file_hashes), True, hit.error
        try:
            if self._rg_path is not None:
                hits, stop, rg_error = self._collect_rg(query, scope, cap, deadline)
            else:
                hits, stop = self._collect_python(query, scope, cap, deadline)
                rg_error = None
        except WorkspaceError as exc:
            return ToolStatus.ERROR, None, (), {}, False, str(exc)
        if stop is None and len(hits) >= cap:
            stop = "limit"
        touched = tuple(sorted({item[0] for item in hits}))
        if stop == "deadline":
            self.workspace.mark_read(touched)
            return ToolStatus.PARTIAL, self._aggregate_results(query, hits), touched, self.workspace.hash_paths(touched), False, "search stopped at the workspace deadline"
        if rg_error is not None:
            if hits:
                self.workspace.mark_read(touched)
                return ToolStatus.PARTIAL, self._aggregate_results(query, hits), touched, self.workspace.hash_paths(touched), False, rg_error
            return ToolStatus.ERROR, None, (), {}, False, rg_error
        self.workspace.mark_read(touched)
        if stop == "limit":
            return ToolStatus.PARTIAL, self._aggregate_results(query, hits), touched, self.workspace.hash_paths(touched), False, "search result limit reached"
        if not hits:
            # EMPTY 与 PARTIAL 同等保守,不缓存:EMPTY 没有 touched 文件可做
            # hash 校验,edit_file 在 scope 内引入新匹配后回放会变成过期的
            # complete=True,违反 fail-closed。
            return ToolStatus.EMPTY, [], (), {}, True, "no textual matches; this does not prove semantic absence"
        hashes = self.workspace.hash_paths(touched)
        content = self._aggregate_results(query, hits)
        if limit is not None:
            self.cache.searches.put(query, scope, limit, SearchContextEntry(ToolStatus.OK, (dict(content),), touched, dict(hashes), None))
        return ToolStatus.OK, content, touched, hashes, True, None

    def _collect_python(self, query: str, scope: tuple[str, ...], cap: int, deadline: float | None) -> tuple[list[tuple[str, int, str]], str | None]:
        hits: list[tuple[str, int, str]] = []
        stop: str | None = None
        for relative, candidate in self.workspace.iter_files(scope):
            if deadline is not None and time.monotonic() >= deadline:
                stop = "deadline"
                break
            if not candidate.is_file() or candidate.stat().st_size > self.max_file_bytes:
                continue
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                if deadline is not None and time.monotonic() >= deadline:
                    stop = "deadline"
                    break
                if query in line:
                    hits.append((relative, line_number, line))
                    if len(hits) >= cap:
                        stop = "limit"
                        break
            if stop is not None:
                break
        return hits, stop

    def _collect_rg(self, query: str, scope: tuple[str, ...], cap: int, deadline: float | None) -> tuple[list[tuple[str, int, str]], str | None, str | None]:
        """Run ripgrep with fixed argv; returns (hits, stop_reason, rg_error)."""
        if deadline is not None and time.monotonic() >= deadline:
            return [], "deadline", None
        scope_paths: list[str] = []
        for entry in scope:
            self.workspace.resolve(entry)  # protected/escaping scope fails like the Python path
            if not (self.workspace.root / entry).exists():
                # 与 Python 回退口径一致:缺失的 scope 不产生匹配,不算错误。
                continue
            scope_paths.append(entry)
        if not scope_paths:
            return [], None, None
        # flags verified against `rg --help` (ripgrep 14.1.1): --json, --fixed-strings,
        # --max-filesize (plain number = bytes), --glob. `--` keeps the query and every
        # scope path strictly positional, so a model-supplied scope starting with "-"
        # can never be parsed as a flag (e.g. --pre).
        argv = [self._rg_path, "--json", "--fixed-strings", "--max-filesize", str(self.max_file_bytes)]
        for protected in self.workspace.protected_paths:
            argv.extend(["--glob", f"!{protected}/**"])
        argv.extend(["--", query, *scope_paths])
        timeout = None
        if deadline is not None:
            timeout = max(0.001, deadline - time.monotonic())
        try:
            completed = subprocess.run(argv, cwd=self.workspace.root, capture_output=True, check=False, timeout=timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            hits = self._parse_rg_output(exc.stdout or b"", cap)
            return hits, "deadline", None
        except OSError as exc:
            return [], None, f"ripgrep unavailable: {exc}"
        hits = self._parse_rg_output(completed.stdout, cap)
        if completed.returncode not in (0, 1):
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            detail = f"ripgrep reported errors (exit {completed.returncode}): {stderr[:200]}".rstrip()
            return hits, None, detail
        return hits, None, None

    def _parse_rg_output(self, payload: bytes, cap: int) -> list[tuple[str, int, str]]:
        """Parse rg --json events line by line; bad lines are skipped."""
        hits: list[tuple[str, int, str]] = []
        for raw_line in payload.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "match":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            path_value = data.get("path")
            lines_value = data.get("lines")
            line_number = data.get("line_number")
            if not isinstance(path_value, dict) or not isinstance(lines_value, dict):
                continue
            path_text = path_value.get("text")
            text = lines_value.get("text")
            if not isinstance(path_text, str) or not isinstance(text, str) or not isinstance(line_number, int):
                continue
            relative = path_text.replace("\\", "/")
            if relative.startswith("./"):
                relative = relative[2:]
            try:
                resolved = self.workspace.resolve(relative)
            except WorkspaceError:
                continue  # 文件名级保护(.env/credentials 等)与逃逸路径一律丢弃
            if not resolved.is_file() or resolved.stat().st_size > self.max_file_bytes:
                continue
            hits.append((relative, line_number, text.rstrip("\r\n")))
            if len(hits) >= cap:
                break
        return hits

    def _score_file(self, query: str, path: str, line_hits: list[tuple[int, str]]) -> int:
        """启发式排序公式(方案 §7),非语义相关度:精确标识符 + 同文件 + 同模块 + 符号 + trace 词元。"""
        score = 0
        texts = [text for _, text in line_hits]
        if any(_contains_identifier(query, text) for text in texts):
            score += 4  # ExactIdentifierMatch:查询作为独立标识符出现
        if path in self.ranking.files:
            score += 3  # SameFile:警告所在文件
        if path.replace("\\", "/").split("/")[0] in self.ranking.modules:
            score += 2  # SameModule:顶层目录(模块近似)
        if any(_contains_identifier(symbol, text) for symbol in self.ranking.symbols for text in texts):
            score += 2  # SameSymbol:警告符号在命中行内作为独立标识符出现
        if any(token in text for token in self.ranking.trace_tokens for text in texts):
            score += 1  # WarningTraceHit:analysis_trace 词元子串命中(弱加分)
        return score

    def _aggregate_results(self, query: str, hits: list[tuple[str, int, str]]) -> dict[str, Any]:
        """File-level aggregation: per-file hit count + up to 3 sample lines."""
        grouped: dict[str, list[tuple[int, str]]] = {}
        for path, line_number, text in hits:
            grouped.setdefault(path, []).append((line_number, text))
        scored = []
        for path, line_hits in grouped.items():
            line_hits.sort(key=lambda item: item[0])
            scored.append((self._score_file(query, path, line_hits), len(line_hits), path, line_hits))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        files = [
            {
                "path": path,
                "hits": hit_count,
                "sample_lines": [{"line": number, "text": text[:SYMBOL_SAMPLE_LINE_CHARS]} for number, text in line_hits[:3]],
            }
            for _, hit_count, path, line_hits in scored
        ]
        return {"files": files, "total_hits": len(hits)}

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
