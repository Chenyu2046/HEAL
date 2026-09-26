"""Heuristic standard-library C/C++ lexical symbol scanner.

Leader decision (zero third-party dependencies): no tree-sitter here. This is a
lexer-level outline, NOT semantic navigation. Known ceilings, documented on
purpose: macro expansion, preprocessor conditionals (``#if`` branches), template
partial specializations, typedef chains, raw string literals, operator
overloads, and declaration-spanning macros are unreliable or unsupported.
Line ranges come from brace pairing over cleaned text in which comments,
string/char literals, and preprocessor lines are blanked first, so braces
inside comments and strings never break the structure. Upgrade path: replace
this module with a tree-sitter or clangd/compile_commands backed extractor
behind the same ``SymbolDecl`` contract (方案 §4.3, Phase 2).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

SIGNATURE_MAX_CHARS = 160
# list_symbols 输出条数上限;工具层据此对超限结果给出 TRUNCATED(扫描器本身不截断)。
MAX_SYMBOL_DECLS = 512

_TYPE_KEYWORDS = frozenset({"namespace", "class", "struct"})
_CONTROL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "goto", "throw",
    "sizeof", "alignof", "alignas", "decltype", "new", "delete", "static_assert", "co_await", "co_return", "co_yield",
})
_QUALIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])(const|volatile|noexcept|override|final)(?![A-Za-z0-9_])")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class SymbolDecl:
    type: str
    name: str
    signature: str
    start_line: int
    end_line: int


def _blank_preprocessor(cleaned: str) -> str:
    """Blank ``#`` directive lines (plus backslash continuations) to spaces."""
    result: list[str] = []
    in_directive = False
    for line in cleaned.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        starts_directive = body.lstrip().startswith("#")
        if starts_directive or in_directive:
            result.append(" " * len(body) + eol)
            in_directive = body.endswith("\\")
        else:
            result.append(line)
    return "".join(result)


def _strip_comments_and_literals(text: str) -> str:
    """Blank comments and string/char literal bodies to spaces, newline-preserving.

    每个字符位置都映射回原行号,后续花括号配对与行号计算不受影响。
    """
    out = list(text)
    n = len(text)
    i = 0
    state = "code"
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line"
                out[i] = out[i + 1] = " "
                i += 2
            elif c == "/" and nxt == "*":
                state = "block"
                out[i] = out[i + 1] = " "
                i += 2
            elif c in {'"'}:
                state = "string"
                out[i] = " "
                i += 1
            elif c in {"'"}:
                state = "char"
                out[i] = " "
                i += 1
            else:
                i += 1
        elif state == "line":
            if c == "\n":
                state = "code"
            else:
                out[i] = " "
            i += 1
        elif state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = "code"
                i += 2
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
        else:  # string / char literal; a bare newline recovers unterminated literals
            if c == "\\" and nxt:
                out[i] = out[i + 1] = " "
                i += 2
            elif (state == "string" and c == '"') or (state == "char" and c == "'") or c == "\n":
                if c != "\n":
                    out[i] = " "
                state = "code"
                i += 1
            else:
                if c != "\n":
                    out[i] = " "
                i += 1
    return _blank_preprocessor("".join(out))


def _skip_ws(cleaned: str, i: int, end: int) -> int:
    while i < end and cleaned[i].isspace():
        i += 1
    return i


def _matching_brace(cleaned: str, open_idx: int, end: int) -> int:
    depth = 1
    i = open_idx + 1
    while i < end:
        c = cleaned[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return end - 1  # unbalanced (e.g. #if-injected braces): clamp to region end


def _matching_paren(cleaned: str, open_idx: int, end: int) -> int:
    depth = 1
    i = open_idx + 1
    while i < end:
        c = cleaned[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _matching_angle(cleaned: str, open_idx: int, end: int) -> int:
    depth = 1
    i = open_idx + 1
    while i < end:
        c = cleaned[i]
        if c in "<{;":
            if c == "<":
                depth += 1
            else:
                return -1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _find_body_open(cleaned: str, start: int, end: int, *, max_chars: int, max_newlines: int) -> int:
    """Bounded scan for the ``{`` that opens a body after ``:`` or ``->``."""
    i = start
    chars = 0
    newlines = 0
    while i < end:
        c = cleaned[i]
        if c == "{":
            return i
        if c == ";":
            return -1
        if c == "\n":
            newlines += 1
            if newlines > max_newlines:
                return -1
        chars += 1
        if chars > max_chars:
            return -1
        i += 1
    return -1


def _prev_word(cleaned: str, pos: int) -> str:
    i = pos - 1
    while i >= 0 and cleaned[i].isspace():
        i -= 1
    e = i
    while i >= 0 and (cleaned[i].isalnum() or cleaned[i] == "_"):
        i -= 1
    return cleaned[i + 1 : e + 1]


def _qualified_name_backward(cleaned: str, ident_start: int, ident: str) -> str:
    """Collect ``Class::method`` / ``~Class`` prefixes written before the identifier.

    模板参数(如 ``Foo<int>::bar``)处停止,只保留最后一段——词法层不可靠,刻意简化。
    """
    name = ident
    i = ident_start
    if i > 0 and cleaned[i - 1] == "~":
        name = "~" + name
        i -= 1
    while i >= 2 and cleaned[i - 1] == ":" and cleaned[i - 2] == ":":
        j = i - 2
        while j >= 1 and cleaned[j - 1].isspace():
            j -= 1
        e = j - 1
        if e < 0 or not (cleaned[e].isalnum() or cleaned[e] == "_"):
            break
        s = e
        while s > 0 and (cleaned[s - 1].isalnum() or cleaned[s - 1] == "_"):
            s -= 1
        name = f"{cleaned[s:e + 1]}::{name}"
        i = s
    return name


def _collapse_signature(cleaned: str, sig_from: int, body_open: int) -> str:
    raw = cleaned[sig_from:body_open]
    signature = " ".join(raw.split())
    if len(signature) > SIGNATURE_MAX_CHARS:
        return signature[:SIGNATURE_MAX_CHARS] + "…"
    return signature


def _make_decl(kind: str, name: str, cleaned: str, sig_from: int, body_open: int, body_close: int, line_starts: tuple[int, ...]) -> SymbolDecl:
    return SymbolDecl(
        type=kind,
        name=name,
        signature=_collapse_signature(cleaned, sig_from, body_open),
        start_line=_line_of(line_starts, sig_from),
        end_line=_line_of(line_starts, body_close),
    )


def _line_of(line_starts: tuple[int, ...], offset: int) -> int:
    return bisect_right(line_starts, offset)


def _try_type_decl(cleaned: str, kw_start: int, keyword: str, end: int, line_starts: tuple[int, ...]):
    """Match ``namespace/class/struct Name[: bases] {``; forward declarations return None."""
    kind = "enum" if _prev_word(cleaned, kw_start) == "enum" else keyword
    j = _skip_ws(cleaned, kw_start + len(keyword), end)
    # Skip attributes [[...]] and alignas(...) noise before the name.
    for _ in range(4):
        if cleaned.startswith("[[", j):
            close = cleaned.find("]]", j)
            if close < 0:
                return None
            j = _skip_ws(cleaned, close + 2, end)
            continue
        if cleaned.startswith("alignas", j) and _IDENT_RE.match(cleaned, j).group(0) == "alignas":
            paren = cleaned.find("(", j)
            if paren < 0:
                return None
            close = _matching_paren(cleaned, paren, end)
            if close < 0:
                return None
            j = _skip_ws(cleaned, close + 1, end)
            continue
        break
    name: str | None = None
    # 宏噪声容忍:``struct AUDIO_API Foo {`` 取最后一个关键字后的标识符为名字。
    for _ in range(6):
        m = _IDENT_RE.match(cleaned, j)
        if m is None:
            break
        name = m.group(0)
        j = _skip_ws(cleaned, m.end(), end)
        if j < end and cleaned[j] == "<":
            close = _matching_angle(cleaned, j, end)
            if close < 0:
                return None
            j = _skip_ws(cleaned, close + 1, end)
        if j < end and _QUALIFIER_RE.match(cleaned, j):
            j = _skip_ws(cleaned, _QUALIFIER_RE.match(cleaned, j).end(), end)
            continue
        if j < end and (cleaned[j].isalpha() or cleaned[j] == "_"):
            continue  # previous identifier was macro noise; take this one
        break
    k = j
    steps = 0
    while k < end and steps < 4000:
        steps += 1
        c = cleaned[k]
        if c == "{":
            body_close = _matching_brace(cleaned, k, end)
            sig_from = line_starts[_line_of(line_starts, kw_start) - 1]
            return k, body_close, _make_decl(kind, name or "<anonymous>", cleaned, sig_from, k, body_close, line_starts)
        if c == ";":
            return None  # forward declaration: no body to navigate
        k += 1
    return None


def _try_function(cleaned: str, ident_start: int, ident_end: int, ident: str, end: int, enclosing: str | None, line_starts: tuple[int, ...]):
    """Match ``[Qual] Name(args) [quals|: init|-> ret] {``; calls/prototypes return None."""
    if ident in _CONTROL_KEYWORDS:
        return None
    name = _qualified_name_backward(cleaned, ident_start, ident)
    j = _skip_ws(cleaned, ident_end, end)
    if j >= end or cleaned[j] != "(":
        return None
    close = _matching_paren(cleaned, j, end)
    if close < 0:
        return None
    k = _skip_ws(cleaned, close + 1, end)
    for _ in range(8):
        if k >= end:
            return None
        c = cleaned[k]
        if c == "{":
            body_close = _matching_brace(cleaned, k, end)
            sig_from = line_starts[_line_of(line_starts, ident_start) - 1]
            if "::" in name:
                kind = "member_function"
            elif enclosing:
                name = f"{enclosing}::{name}"
                kind = "member_function"
            else:
                kind = "function"
            return body_close, _make_decl(kind, name, cleaned, sig_from, k, body_close, line_starts)
        if c == ";":
            return None  # prototype only
        if c == ":" and not cleaned.startswith("::", k):
            k = _find_body_open(cleaned, k + 1, end, max_chars=512, max_newlines=4)
            if k < 0:
                return None
            continue
        if cleaned.startswith("->", k):
            k = _find_body_open(cleaned, k + 2, end, max_chars=512, max_newlines=4)
            if k < 0:
                return None
            continue
        qualifier = _QUALIFIER_RE.match(cleaned, k)
        if qualifier:
            k = _skip_ws(cleaned, qualifier.end(), end)
            if k < end and cleaned[k] == "(":
                paren_close = _matching_paren(cleaned, k, end)
                if paren_close < 0:
                    return None
                k = _skip_ws(cleaned, paren_close + 1, end)
            continue
        return None
    return None


def scan_symbols(text: str) -> tuple[SymbolDecl, ...]:
    """Return the lexical symbol outline of a C/C++ source text.

    类/结构体体内递归识别成员函数;类外 ``Class::method`` 定义按书写的限定名识别。
    未闭合的花括号按区域结尾截断(预处理器条件不可靠的天花板之一)。
    """
    cleaned = _strip_comments_and_literals(text)
    line_starts_list = [0]
    for index, ch in enumerate(cleaned):
        if ch == "\n":
            line_starts_list.append(index + 1)
    line_starts = tuple(line_starts_list)
    decls: list[SymbolDecl] = []
    _scan(cleaned, 0, len(cleaned), None, line_starts, decls)
    return tuple(decls)


def _scan(cleaned: str, start: int, end: int, enclosing: str | None, line_starts: tuple[int, ...], decls: list[SymbolDecl]) -> None:
    i = start
    while i < end:
        c = cleaned[i]
        destructor = c == "~"
        if destructor:
            m = _IDENT_RE.match(cleaned, i + 1)
        elif c.isalpha() or c == "_":
            m = _IDENT_RE.match(cleaned, i)
        else:
            i += 1
            continue
        if m is None:
            i += 1
            continue
        ident = m.group(0)
        ident_start = i + 1 if destructor else i
        if not destructor and ident in _TYPE_KEYWORDS:
            matched = _try_type_decl(cleaned, i, ident, end, line_starts)
            if matched is not None:
                body_open, body_close, decl = matched
                decls.append(decl)
                nested = decl.name if decl.type in {"class", "struct"} and not decl.name.startswith("<") else enclosing
                _scan(cleaned, body_open + 1, body_close, nested, line_starts, decls)
                i = body_close + 1
                continue
            i = m.end()
            continue
        matched = _try_function(cleaned, ident_start, m.end(), ident, end, enclosing, line_starts)
        if matched is not None:
            body_close, decl = matched
            decls.append(decl)
            i = body_close + 1
            continue
        i = m.end()
