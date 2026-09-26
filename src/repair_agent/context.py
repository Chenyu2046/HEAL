"""Phase-1 Context Memory: version-checked reuse of file reads and complete search scans.

命中路径省掉的是证据读取(文件内容、搜索扫描);版本校验本身仍由 executor 每次
调用前的 workspace.refresh() 完成(重新 hash 已观察文件),本模块不改变
refresh/hash 的任何行为。前提假设沿用仓库现有约束:worker 运行期间工作区只经
工具修改,因此运行期间外部新建/删除的文件对缓存校验不可见(见
SearchResultCache 的天花板注释)。缓存按 worker 实例注入,不跨 worker 共享;
单个 AgentLoop 线程内顺序使用,不加锁。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping

from .domain import ToolStatus

DEFAULT_CAPACITY = 64


@dataclass(frozen=True)
class FileContextEntry:
    text: str
    content_hash: str
    observed_hash: str


class FileContextCache:
    """整文件文本缓存,键为 path;命中 = 记录的文件 hash 与当前 observed hash 一致。

    纯内存比较,文件 hash 变化即失效,无需显式失效调用:edit_file 成功即
    mark_edit 更新 observed_hashes,按路径校验自然只失效受影响文件、无关文件
    继续复用。超过 max_file_bytes 的文件在 SourceTools 读到内容之前就已返回,
    结构上进不了缓存。
    """

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("context cache capacity must be positive")
        self._capacity = capacity
        self._entries: OrderedDict[str, FileContextEntry] = OrderedDict()

    def get(self, path: str, observed_hash: str | None) -> FileContextEntry | None:
        entry = self._entries.get(path)
        if entry is None or observed_hash is None or observed_hash != entry.observed_hash:
            return None
        self._entries.move_to_end(path)
        return entry

    def put(self, path: str, *, text: str, content_hash: str, observed_hash: str) -> None:
        self._entries[path] = FileContextEntry(text, content_hash, observed_hash)
        self._entries.move_to_end(path)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class SearchContextEntry:
    status: ToolStatus
    results: tuple[dict[str, Any], ...]
    touched: tuple[str, ...]
    file_hashes: Mapping[str, str]
    error: str | None


class SearchResultCache:
    """完整搜索扫描缓存,键 = (query, scope 元组, 有效 max_results) 精确匹配。

    只缓存 complete=True 且非空的完整扫描(OK),命中原样回放
    results/touched/hash/status。两类 PARTIAL 第一版都不缓存:deadline 中断的
    截断点取决于墙钟时刻,回放不可靠也不确定;limit 截断的未扫余量无法用
    touched 文件 hash 校验,回放等于把一次不完整扫描伪装成可复用证据。EMPTY
    第一版同样不缓存:它没有 touched 文件可做 hash 校验,edit_file 在 scope
    内引入新匹配后回放会变成过期的 complete=True,违反 fail-closed。升级
    路径:EMPTY 记录 scope 全量文件 hash 并纳入 refresh 维护后回放;PARTIAL
    记录已扫描游标做确定性增量续扫。
    天花板:OK 命中校验只覆盖 touched 文件的 hash(方案 §5 的失效粒度),
    把新匹配引入"原扫描未触碰文件"的修改不会被检测到——包括 worker 自己的
    edit_file,不只外部修改;未触碰文件的行号偏移同样不可见。升级路径:
    edit 成功后按 scope 前缀失效 search 区,或对 scope 全量文件做 hash 校验。
    """

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("context cache capacity must be positive")
        self._capacity = capacity
        self._entries: OrderedDict[tuple[str, tuple[str, ...], int], SearchContextEntry] = OrderedDict()

    def get(self, query: str, scope: tuple[str, ...], max_results: int, observed_hashes: Mapping[str, str]) -> SearchContextEntry | None:
        key = (query, scope, max_results)
        entry = self._entries.get(key)
        if entry is None:
            return None
        for path, digest in entry.file_hashes.items():
            if observed_hashes.get(path) != digest:
                return None
        self._entries.move_to_end(key)
        return entry

    def put(self, query: str, scope: tuple[str, ...], max_results: int, entry: SearchContextEntry) -> None:
        self._entries[(query, scope, max_results)] = entry
        self._entries.move_to_end((query, scope, max_results))
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class ContextCache:
    """门面:聚合 FileContextCache 与 SearchResultCache,两个区各自独立 LRU 容量。"""

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self.files = FileContextCache(capacity=capacity)
        self.searches = SearchResultCache(capacity=capacity)
