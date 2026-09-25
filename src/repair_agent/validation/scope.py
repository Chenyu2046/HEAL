"""Hard gates for integrated patch size before candidate freezing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..config import ToolLimits


@dataclass(frozen=True)
class PatchScope:
    changed_files: int
    diff_lines: int
    added_lines: int
    deleted_lines: int
    violations: tuple[str, ...]


class PatchScopeGuard:
    def __init__(self, limits: ToolLimits) -> None:
        self.limits = limits

    def inspect(self, diff: str, files: Iterable[str]) -> PatchScope:
        changed_files = len(set(files))
        added = sum(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines())
        deleted = sum(line.startswith("-") and not line.startswith("---") for line in diff.splitlines())
        diff_lines = added + deleted
        violations = []
        if changed_files > self.limits.max_changed_files:
            violations.append(f"changed files {changed_files} exceed limit {self.limits.max_changed_files}")
        if diff_lines > self.limits.max_diff_lines:
            violations.append(f"diff lines {diff_lines} exceed limit {self.limits.max_diff_lines}")
        return PatchScope(changed_files, diff_lines, added, deleted, tuple(violations))
