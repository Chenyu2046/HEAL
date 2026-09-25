"""Workspace versioning, safe paths, Git worktrees, and tree hashing."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..domain import has_protected_name, normalize_repo_path, sha256_bytes


class WorkspaceError(RuntimeError):
    """Workspace or Git isolation cannot satisfy the requested contract."""


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root: Path, excluded: Iterable[str] = (".git", ".repair-agent")) -> str:
    digest = hashlib.sha256()
    excluded_set = {item.lower() for item in excluded}
    files = [
        path for path in root.rglob("*")
        if path.is_file() and not any(part.lower() in excluded_set for part in path.relative_to(root).parts)
    ]
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass
class WorkspaceState:
    root: Path
    base_commit: str
    revision: int = 0
    observed_hashes: dict[str, str] = field(default_factory=dict)
    protected_paths: tuple[str, ...] = (".git", ".repair-agent", "third_party", "vendor", "dependencies", "ci", ".github")

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace does not exist: {self.root}")

    def resolve(self, relative: str, *, write: bool = False, allow_protected: bool = False) -> Path:
        normalized = normalize_repo_path(relative)
        protected = {item.lower() for item in self.protected_paths}
        parts = normalized.lower().split("/")
        if not allow_protected and (has_protected_name(normalized) or any(part in protected for part in parts)):
            raise WorkspaceError(f"protected path: {normalized}")
        raw_candidate = self.root / normalized
        try:
            raw_candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"path escapes workspace: {relative}") from exc
        cursor = raw_candidate
        while cursor != self.root:
            if cursor.is_symlink():
                raise WorkspaceError(f"symbolic-link path is not allowed: {normalized}")
            cursor = cursor.parent
        candidate = raw_candidate.resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"path escapes workspace: {relative}") from exc
        if write and candidate.exists() and not candidate.is_file():
            raise WorkspaceError(f"only regular files can be edited: {normalized}")
        return candidate

    def hash_paths(self, paths: Iterable[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for relative in sorted(set(paths)):
            path = self.resolve(relative)
            if path.is_file():
                hashes[relative] = file_hash(path)
        return hashes

    def refresh(self, paths: Iterable[str] = ()) -> bool:
        requested = set(paths) | set(self.observed_hashes)
        current = self.hash_paths(requested)
        changed = current != self.observed_hashes
        if changed:
            self.revision += 1
            self.observed_hashes = current
        return changed

    def mark_read(self, paths: Iterable[str]) -> None:
        self.observed_hashes.update(self.hash_paths(paths))

    def mark_edit(self, paths: Iterable[str]) -> None:
        self.revision += 1
        self.observed_hashes.update(self.hash_paths(paths))


@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    path: Path
    base_commit: str
    managed_root: Path
    source_repo: Path


class GitWorktreeManager:
    """Create isolated worktrees using fixed argv; never touches the caller worktree."""

    def __init__(self, parent: str | Path) -> None:
        self.parent = Path(parent).resolve()
        self.parent.mkdir(parents=True, exist_ok=True)

    def _git(self, args: list[str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args], cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"git unavailable or timed out: {exc}") from exc

    def create(self, *, task_id: str, source_repo: str | Path, base_commit: str) -> WorktreeHandle:
        repo = Path(source_repo).resolve()
        if not repo.is_dir():
            raise WorkspaceError(f"source repository is not configured: {repo}")
        root = self._git(["rev-parse", "--show-toplevel"], repo)
        if root.returncode != 0:
            raise WorkspaceError(f"source is not a Git repository: {repo}")
        target = (self.parent / task_id).resolve()
        target.relative_to(self.parent)
        if target.exists():
            raise WorkspaceError(f"managed worktree already exists: {target}")
        result = self._git(["worktree", "add", "--detach", str(target), base_commit], repo, timeout=120.0)
        if result.returncode != 0:
            raise WorkspaceError(f"git worktree creation failed: {result.stderr.strip()}")
        return WorktreeHandle(task_id, target, base_commit, self.parent, repo)

    def existing(self, *, task_id: str, base_commit: str) -> WorktreeHandle:
        target = (self.parent / task_id).resolve()
        target.relative_to(self.parent)
        if not target.is_dir():
            raise WorkspaceError(f"managed worktree missing: {target}")
        return WorktreeHandle(task_id, target, base_commit, self.parent, self.parent)

    def remove(self, handle: WorktreeHandle) -> None:
        target = handle.path.resolve()
        target.relative_to(self.parent)
        result = self._git(["worktree", "remove", "--force", str(target)], handle.source_repo, timeout=120.0)
        if result.returncode != 0:
            raise WorkspaceError(f"worktree removal failed: {result.stderr.strip()}")
