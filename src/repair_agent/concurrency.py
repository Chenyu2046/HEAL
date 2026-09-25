"""Bounded homogeneous workers and conservative serial integration."""

from __future__ import annotations

import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .planning import WorkingBatch, known_conflict


@dataclass(frozen=True)
class WorkerEnvelope:
    batch: WorkingBatch
    worker_id: str
    result: object


class WorkerPool:
    def __init__(self, max_workers: int) -> None:
        self.max_workers = max(1, max_workers)

    def run(self, slot: Sequence[WorkingBatch], worker: Callable[[WorkingBatch, str], object]) -> tuple[WorkerEnvelope, ...]:
        results: list[WorkerEnvelope] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(slot) or 1), thread_name_prefix="repair-worker") as pool:
            futures = {pool.submit(worker, batch, f"worker-{index + 1}"): (batch, f"worker-{index + 1}") for index, batch in enumerate(slot)}
            for future in as_completed(futures):
                batch, worker_id = futures[future]
                results.append(WorkerEnvelope(batch, worker_id, future.result()))
        return tuple(sorted(results, key=lambda item: item.batch.batch_id))


@dataclass(frozen=True)
class IntegrationResult:
    applied: bool
    conflicts: tuple[str, ...] = ()
    semantic_warnings: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    message: str = ""
    conflicting_batches: tuple[str, ...] = ()


class IntegrationEngine:
    """Apply real worker diffs serially; different files do not imply semantic safety."""

    def integrate(self, workspace: Path, proposals: Sequence[object]) -> IntegrationResult:
        seen_files: dict[str, str] = {}
        conflicts: list[str] = []
        conflicting_batches: set[str] = set()
        semantic_warnings: list[str] = []
        all_files: set[str] = set()
        for proposal in proposals:
            for path in getattr(proposal, "changed_files", ()):
                all_files.add(path)
                if path in seen_files:
                    conflicts.append(f"file overlap: {path} ({seen_files[path]} and {proposal.batch_id})")
                    conflicting_batches.update((seen_files[path], proposal.batch_id))
                seen_files[path] = proposal.batch_id
                if path.endswith((".h", ".hpp", ".hh", ".inl")):
                    semantic_warnings.append(f"public/header dependency requires semantic review: {path}")
            diff = str(getattr(proposal, "diff", ""))
            if any(marker in diff.lower() for marker in ("mutex", "atomic", "lifecycle", "shared_state")):
                semantic_warnings.append(f"semantic shared-state review required for batch {proposal.batch_id}")
        if conflicts:
            return IntegrationResult(False, tuple(sorted(set(conflicts))), tuple(sorted(set(semantic_warnings))), tuple(sorted(all_files)), "known conflict blocks serial integration", tuple(sorted(conflicting_batches)))
        for proposal in proposals:
            diff = str(getattr(proposal, "diff", ""))
            if not diff:
                continue
            try:
                result = subprocess.run(
                    ["git", "apply", "--binary", "--whitespace=nowarn", "--"],
                    cwd=workspace,
                    input=diff.encode("utf-8"),
                    capture_output=True,
                    check=False,
                    timeout=120,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return IntegrationResult(False, (f"integration tool failure: {exc}",), tuple(sorted(set(semantic_warnings))), tuple(sorted(all_files)), "git apply failed")
            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="replace").strip() or "git apply rejected worker diff"
                return IntegrationResult(False, (error,), tuple(sorted(set(semantic_warnings))), tuple(sorted(all_files)), "worker diff could not be applied", (str(getattr(proposal, "batch_id", "")),))
        return IntegrationResult(True, (), tuple(sorted(set(semantic_warnings))), tuple(sorted(all_files)), "integrated serially; semantic independence remains a review obligation")


def requeue_for_expanded_scope(batch: WorkingBatch, changed_files: Iterable[str]) -> bool:
    """A worker that edits outside its known scope must be re-scheduled."""

    return not set(changed_files).issubset(batch.known_files)
