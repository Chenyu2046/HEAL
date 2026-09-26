"""Workspace versioning, safe paths, Git worktrees, and tree hashing."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..domain import has_protected_name, normalize_repo_path, sha256_bytes


class WorkspaceError(RuntimeError):
    """Workspace or Git isolation cannot satisfy the requested contract."""


def _lock_windows_byte(handle) -> None:
    import msvcrt

    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            time.sleep(0.05)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_hash(root: Path, excluded: Iterable[str] = (".git", ".repair-agent")) -> str:
    """Hash working contents Git would consider part of the source tree, not ignored build output."""
    root = root.resolve()
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"cannot enumerate Git source tree: {exc}") from exc
    if listed.returncode != 0:
        raise WorkspaceError(f"cannot enumerate Git source tree: {listed.stderr.decode(errors='replace').strip()}")
    digest = hashlib.sha256()
    excluded_set = {item.lower() for item in excluded}
    files = sorted({item.decode("utf-8", errors="surrogateescape") for item in listed.stdout.split(b"\0") if item})
    for relative in files:
        if any(part.lower() in excluded_set for part in Path(relative).parts):
            continue
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
            digest.update(b"symlink:")
            digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        elif path.is_file():
            digest.update(file_hash(path).encode("ascii"))
        else:
            digest.update(b"<deleted-or-submodule>")
        digest.update(b"\n")
    return digest.hexdigest()


def git_tree_oid(root: Path, *, stage_changes: bool = False) -> str:
    root = root.resolve()
    if stage_changes:
        try:
            added = subprocess.run(["git", "add", "--all", "--", "."], cwd=root, capture_output=True, text=True, check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"cannot stage candidate tree: {exc}") from exc
        if added.returncode != 0:
            raise WorkspaceError(f"cannot stage candidate tree: {added.stderr.strip()}")
    try:
        result = subprocess.run(["git", "write-tree"], cwd=root, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"cannot resolve Git tree identity: {exc}") from exc
    if result.returncode != 0:
        raise WorkspaceError(f"cannot resolve Git tree identity: {result.stderr.strip()}")
    return result.stdout.strip()


def cleanup_stale_temp_files(roots: Iterable[str | Path], *, older_than_seconds: float, apply: bool = False, kind: str = "artifact", recursive: bool = True) -> tuple[dict[str, str], ...]:
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
    generated_names = {
        "worktree": re.compile(r"^\.heal-worktrees\.[A-Za-z0-9_-]{6,}$"),
        "artifact": re.compile(r"^\.(?:cand-[0-9a-f]{32}-(?:diff|report)|worker-result-[0-9a-f]{32}|report\.(?:json|md))\.[A-Za-z0-9_-]{6,}$"),
        "memory": re.compile(r"^\.[A-Za-z0-9_.-]+\.json\.[A-Za-z0-9_-]{6,}$"),
    }
    generated_name = generated_names.get(kind)
    if generated_name is None:
        raise ValueError(f"unsupported temporary-file root kind: {kind}")
    found: list[dict[str, str]] = []
    for root_value in roots:
        root = Path(root_value).resolve()
        if not root.is_dir():
            continue
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if recursive and not (Path(current) / name).is_symlink()]
            for name in filenames:
                path = Path(current) / name
                if not generated_name.fullmatch(name) or path.is_symlink() or not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                    found.append({"path": str(path), "action": "would_remove" if not apply else "removed"})
                    if apply:
                        path.unlink()
                except OSError:
                    continue
    return tuple(found)


@dataclass(frozen=True)
class WorkspacePolicy:
    protected_paths: tuple[str, ...] = (".git", ".repair-agent", "third_party", "vendor", "dependencies", "ci", ".github")

    def is_protected(self, relative: str) -> bool:
        parts = tuple(part.lower() for part in relative.replace("\\", "/").split("/"))
        protected = {item.lower() for item in self.protected_paths}
        if any(part in protected or part.endswith(".lock") or part == "lockfile" for part in parts):
            return True
        name = parts[-1] if parts else ""
        return bool(
            name == ".env" or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12"))
            or re.match(r"^(credentials?|secrets?)([._-]|$)", name)
        )


@dataclass
class WorkspaceState:
    root: Path
    base_commit: str
    revision: int = 0
    observed_hashes: dict[str, str] = field(default_factory=dict)
    protected_paths: tuple[str, ...] = (".git", ".repair-agent", "third_party", "vendor", "dependencies", "ci", ".github")
    policy: WorkspacePolicy = field(init=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace does not exist: {self.root}")
        self.policy = WorkspacePolicy(self.protected_paths)

    def resolve(self, relative: str, *, write: bool = False, allow_protected: bool = False) -> Path:
        normalized = normalize_repo_path(relative)
        if not allow_protected and (has_protected_name(normalized) or self.policy.is_protected(normalized)):
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

    def iter_files(self, roots: Iterable[str]) -> Iterable[tuple[str, Path]]:
        for relative_root in roots:
            target = self.resolve(relative_root)
            if target.is_file():
                yield normalize_repo_path(relative_root), target
                continue
            for current, directories, filenames in os.walk(target, followlinks=False):
                current_path = Path(current)
                directories[:] = [
                    name for name in directories
                    if not (current_path / name).is_symlink()
                    and not self.policy.is_protected((current_path / name).relative_to(self.root).as_posix())
                ]
                for name in filenames:
                    path = current_path / name
                    relative = path.relative_to(self.root).as_posix()
                    if path.is_symlink() or self.policy.is_protected(relative):
                        continue
                    try:
                        yield relative, self.resolve(relative)
                    except (WorkspaceError, ValueError):
                        continue

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
    run_id: str = ""
    role: str = "worker"


class GitWorktreeManager:
    """Create isolated worktrees using fixed argv; never touches the caller worktree."""

    _registry_locks: dict[str, threading.RLock] = {}
    _registry_locks_guard = threading.Lock()
    _task_locks = tuple(threading.RLock() for _ in range(64))

    def __init__(self, parent: str | Path) -> None:
        self.parent = Path(parent).resolve()
        self.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.parent / ".heal-worktrees.json"
        with self._registry_locks_guard:
            key = os.path.normcase(str(self.registry_path))
            self._registry_lock = self._registry_locks.setdefault(key, threading.RLock())

    @contextmanager
    def _registry_guard(self):
        with self._registry_lock:
            lock_path = self.parent / ".heal-worktrees.lock"
            with lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    _lock_windows_byte(handle)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _task_guard(self, task_id: str):
        lock_id = hashlib.sha256(os.path.normcase(task_id).encode("utf-8")).hexdigest()
        lock_path = self.parent / f".heal-worktree-{lock_id}.lock"
        thread_lock = self._task_locks[int(lock_id[:8], 16) % len(self._task_locks)]
        with thread_lock, lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                _lock_windows_byte(handle)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> dict[str, dict[str, str]]:
        if not self.registry_path.exists():
            return {}
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"worktree registry is unreadable: {exc}") from exc
        if not isinstance(value, dict) or not all(isinstance(item, dict) for item in value.values()):
            raise WorkspaceError("worktree registry has an invalid schema")
        return value

    def _write_registry(self, value: dict[str, dict[str, str]]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".heal-worktrees.", dir=self.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.registry_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def entries(self) -> tuple[dict[str, str], ...]:
        with self._registry_guard():
            return tuple(self._read_registry().values())

    def mark(self, handle: WorktreeHandle, state: str) -> None:
        if state not in {"ACTIVE", "RECOVERABLE", "GC_READY", "FINISHED", "CLEANED"}:
            raise ValueError(f"unknown worktree lifecycle state: {state}")
        with self._task_guard(handle.task_id):
            with self._registry_guard():
                entries = self._read_registry()
                record = entries.get(handle.task_id)
                if record is None or Path(record["path"]).resolve() != handle.path.resolve():
                    raise WorkspaceError("worktree is not registered to this manager")
                record["state"] = state
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._write_registry(entries)

    def _git(self, args: list[str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args], cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError(f"git unavailable or timed out: {exc}") from exc

    def resolve_source_repo(self, source_repo: str | Path) -> Path:
        repo = Path(source_repo).expanduser().resolve()
        if not repo.is_dir():
            raise WorkspaceError(f"source repository is not configured: {repo}")
        result = self._git(["rev-parse", "--show-toplevel"], repo)
        if result.returncode != 0:
            raise WorkspaceError(f"source is not a Git repository: {repo}")
        try:
            canonical = Path(result.stdout.strip()).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"cannot resolve Git repository root: {repo}") from exc
        if not canonical.is_dir():
            raise WorkspaceError(f"Git repository root is not a directory: {canonical}")
        return canonical

    def resolve_commit(self, source_repo: str | Path, revision: str) -> str:
        repo = self.resolve_source_repo(source_repo)
        result = self._git(["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], repo)
        commit = result.stdout.strip().lower()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise WorkspaceError(f"base revision is not an available Git commit: {revision}")
        return commit

    def create(self, *, task_id: str, source_repo: str | Path, base_commit: str, run_id: str = "", role: str = "worker") -> WorktreeHandle:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}", task_id):
            raise WorkspaceError("worktree task_id must be a safe path component")
        with self._task_guard(task_id):
            return self._create_locked(task_id=task_id, source_repo=source_repo, base_commit=base_commit, run_id=run_id, role=role)

    def _create_locked(self, *, task_id: str, source_repo: str | Path, base_commit: str, run_id: str, role: str) -> WorktreeHandle:
        repo = self.resolve_source_repo(source_repo)
        target = (self.parent / task_id).resolve()
        target.relative_to(self.parent)
        if target.exists():
            raise WorkspaceError(f"managed worktree already exists: {target}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}", task_id):
            raise WorkspaceError("worktree task_id must be a safe path component")
        resolved_commit = self.resolve_commit(repo, base_commit)
        result = self._git(["worktree", "add", "--detach", str(target), resolved_commit], repo, timeout=120.0)
        if result.returncode != 0:
            raise WorkspaceError(f"git worktree creation failed: {result.stderr.strip()}")
        handle = WorktreeHandle(task_id, target, resolved_commit, self.parent, repo, run_id, role)
        now = datetime.now(timezone.utc).isoformat()
        with self._registry_guard():
            entries = self._read_registry()
            entries[task_id] = {"task_id": task_id, "path": str(target), "base_commit": resolved_commit, "source_repo": str(repo), "run_id": run_id, "role": role, "state": "ACTIVE", "created_at": now, "updated_at": now}
            try:
                self._write_registry(entries)
            except OSError:
                self._git(["worktree", "remove", "--force", str(target)], repo, timeout=120.0)
                raise
        return handle

    def existing(self, *, task_id: str, base_commit: str) -> WorktreeHandle:
        target = (self.parent / task_id).resolve()
        target.relative_to(self.parent)
        if not target.is_dir():
            raise WorkspaceError(f"managed worktree missing: {target}")
        with self._registry_guard():
            record = self._read_registry().get(task_id, {})
        source_repo = Path(record.get("source_repo", str(self.parent))).resolve()
        return WorktreeHandle(task_id, target, base_commit, self.parent, source_repo, record.get("run_id", ""), record.get("role", "worker"))

    def remove(self, handle: WorktreeHandle, *, expected_updated_at: str | None = None) -> None:
        target = handle.path.resolve()
        target.relative_to(self.parent)
        if target == self.parent:
            raise WorkspaceError("refusing to remove the managed worktree root")
        with self._task_guard(handle.task_id):
            with self._registry_guard():
                record = self._read_registry().get(handle.task_id)
                if record is None or Path(record.get("path", "")).resolve() != target or Path(record.get("source_repo", "")).resolve() != handle.source_repo.resolve():
                    raise WorkspaceError("refusing to remove a worktree not registered to this manager")
                if expected_updated_at is not None and record.get("updated_at") != expected_updated_at:
                    raise WorkspaceError("worktree lifecycle changed before removal")
            result = self._git(["worktree", "remove", "--force", str(target)], handle.source_repo, timeout=120.0)
            if result.returncode != 0:
                raise WorkspaceError(f"worktree removal failed: {result.stderr.strip()}")
            with self._registry_guard():
                entries = self._read_registry()
                record = entries.get(handle.task_id)
                if record and Path(record["path"]).resolve() == target:
                    record["state"] = "CLEANED"
                    record["updated_at"] = datetime.now(timezone.utc).isoformat()
                    self._write_registry(entries)

    def gc(self, *, eligible_task_ids: set[str], apply: bool = False) -> tuple[dict[str, str], ...]:
        candidates = [entry for entry in self.entries() if entry["task_id"] in eligible_task_ids and entry.get("state") != "CLEANED" and Path(entry.get("path", "")).is_dir()]
        if not apply:
            return tuple({**entry, "action": "would_remove"} for entry in candidates)
        removed = []
        for entry in candidates:
            handle = WorktreeHandle(entry["task_id"], Path(entry["path"]), entry["base_commit"], self.parent, Path(entry["source_repo"]), entry.get("run_id", ""), entry.get("role", "worker"))
            self.remove(handle)
            removed.append({**entry, "action": "removed"})
        return tuple(removed)
