"""SQLite RunStore with atomic artifacts and identity-bound result records."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from ..domain import Candidate, HumanApproval, RunRecord, Stage, SubmissionIntent, ValidationResult, canonical_json, to_primitive, utc_now
from .trace import sanitize


class StoreError(RuntimeError):
    """Persistent state or stage transition is invalid."""


_TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.RECEIVED: {Stage.REPAIRING, Stage.FAILED},
    Stage.REPAIRING: {Stage.BATCH_REVIEW, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.BATCH_REVIEW: {Stage.INTEGRATING, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.INTEGRATING: {Stage.CANDIDATE_FROZEN, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.CANDIDATE_FROZEN: {Stage.HUMAN_REVIEW, Stage.REVIEW_REQUIRED},
    Stage.HUMAN_REVIEW: {Stage.SUBMITTING, Stage.REPAIRING, Stage.REVIEW_REQUIRED},
    Stage.SUBMITTING: {Stage.CI_PENDING, Stage.SUBMISSION_UNKNOWN, Stage.FAILED},
    Stage.SUBMISSION_UNKNOWN: {Stage.SUBMITTING, Stage.CI_PENDING, Stage.REVIEW_REQUIRED},
    Stage.CI_PENDING: {Stage.VERIFIED, Stage.REPAIRING, Stage.RETRY_INFRA, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.RETRY_INFRA: {Stage.CI_PENDING, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.VERIFIED: set(),
    Stage.REVIEW_REQUIRED: {Stage.REPAIRING, Stage.HUMAN_REVIEW, Stage.FAILED},
    Stage.FAILED: {Stage.REPAIRING, Stage.REVIEW_REQUIRED},
}


class RunStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.root / "artifacts"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "runs.sqlite3"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, stage TEXT NOT NULL,
                    payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    candidate_id TEXT PRIMARY KEY, tree_hash TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS validations (
                    validation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, ci_run_id TEXT NOT NULL,
                    revision TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(candidate_id, ci_run_id, revision)
                );
                CREATE INDEX IF NOT EXISTS traces_by_run ON traces(run_id, sequence_no);
                CREATE INDEX IF NOT EXISTS artifacts_by_run ON artifacts(run_id);
                """
            )

    def create_run(self, record: RunRecord, payload: Mapping[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO runs(run_id, task_id, stage, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)", (record.run_id, record.task_id, record.stage.value, canonical_json(payload), utc_now()))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        payload.update({"run_id": row["run_id"], "task_id": row["task_id"], "stage": row["stage"], "updated_at": row["updated_at"]})
        return payload

    def transition(self, run_id: str, stage: Stage, *, payload_update: Mapping[str, Any] | None = None, failure_class: str | None = None) -> None:
        with self._lock:
            current = self.get_run(run_id)
            if current is None:
                raise StoreError(f"run not found: {run_id}")
            old = Stage(current["stage"])
            if stage != old and stage not in _TRANSITIONS.get(old, set()):
                raise StoreError(f"invalid stage transition {old.value} -> {stage.value}")
            payload = dict(current)
            payload.pop("run_id", None)
            payload.pop("task_id", None)
            payload.pop("stage", None)
            payload.pop("updated_at", None)
            payload.update(payload_update or {})
            if failure_class:
                payload["failure_class"] = failure_class
            with self._db:
                self._db.execute("UPDATE runs SET stage = ?, payload_json = ?, updated_at = ? WHERE run_id = ?", (stage.value, canonical_json(payload), utc_now(), run_id))

    def record_trace(self, run_id: str, payload: Any) -> int:
        sanitized = sanitize(payload)
        with self._lock, self._db:
            row = self._db.execute("SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM traces WHERE run_id = ?", (run_id,)).fetchone()
            sequence = int(row[0])
            cursor = self._db.execute("INSERT INTO traces(run_id, sequence_no, payload_json, created_at) VALUES (?, ?, ?, ?)", (run_id, sequence, canonical_json(sanitized), utc_now()))
            return int(cursor.lastrowid)

    def save_artifact(self, run_id: str, artifact_id: str, kind: str, content: str | bytes, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        run_dir = self.artifact_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(data).hexdigest()
        target = (run_dir / f"{artifact_id}.bin").resolve()
        target.relative_to(run_dir.resolve())
        fd, temp_name = tempfile.mkstemp(prefix=f".{artifact_id}.", dir=run_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        record = {"artifact_id": artifact_id, "run_id": run_id, "kind": kind, "path": str(target), "sha256": digest, "metadata": dict(metadata or {})}
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO artifacts(artifact_id, run_id, kind, path, sha256, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (artifact_id, run_id, kind, str(target), digest, canonical_json(metadata or {}), utc_now()))
        return record

    def save_checkpoint(self, run_id: str, checkpoint_id: str, stage: Stage, artifact_ids: list[str], payload: Mapping[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO checkpoints(checkpoint_id, run_id, stage, artifact_ids_json, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (checkpoint_id, run_id, stage.value, canonical_json(artifact_ids), canonical_json(payload), utc_now()))

    def save_candidate(self, candidate: Candidate) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO candidates(candidate_id, run_id, payload_json) VALUES (?, ?, ?)", (candidate.candidate_id, candidate.run_id, canonical_json(candidate)))

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        row = self._db.execute("SELECT payload_json FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            return None
        return Candidate(**json.loads(row[0]))

    def list_candidates(self, run_id: str) -> tuple[Candidate, ...]:
        rows = self._db.execute("SELECT payload_json FROM candidates WHERE run_id = ? ORDER BY candidate_id", (run_id,)).fetchall()
        return tuple(Candidate(**json.loads(row[0])) for row in rows)

    def list_validations(self, candidate_id: str) -> tuple[ValidationResult, ...]:
        rows = self._db.execute("SELECT payload_json FROM validations WHERE candidate_id = ? ORDER BY validation_id", (candidate_id,)).fetchall()
        values = []
        for row in rows:
            payload = json.loads(row[0])
            from ..domain import ValidationClass
            payload["classification"] = ValidationClass(payload["classification"])
            values.append(ValidationResult(**payload))
        return tuple(values)

    def save_approval(self, approval: HumanApproval) -> None:
        candidate = self.get_candidate(approval.candidate_id)
        if candidate is None or candidate.tree_hash != approval.tree_hash:
            raise StoreError("approval is not bound to the current candidate tree")
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO approvals(candidate_id, tree_hash, payload_json) VALUES (?, ?, ?)", (approval.candidate_id, approval.tree_hash, canonical_json(approval)))

    def get_approval(self, candidate_id: str) -> HumanApproval | None:
        row = self._db.execute("SELECT payload_json FROM approvals WHERE candidate_id = ?", (candidate_id,)).fetchone()
        return HumanApproval(**json.loads(row[0])) if row else None

    def save_submission(self, intent: SubmissionIntent) -> None:
        candidate = self.get_candidate(intent.candidate_id)
        if candidate is None or candidate.tree_hash != intent.candidate_tree_hash:
            raise StoreError("submission intent does not match candidate tree")
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO submissions(submission_id, candidate_id, status, payload_json) VALUES (?, ?, ?, ?)", (intent.submission_id, intent.candidate_id, intent.status.value, canonical_json(intent)))

    def save_validation(self, result: ValidationResult) -> ValidationResult:
        with self._lock:
            existing = self._db.execute("SELECT payload_json FROM validations WHERE candidate_id = ? AND ci_run_id = ? AND revision = ?", (result.candidate_id, result.ci_run_id, result.revision)).fetchone()
            if existing:
                payload = json.loads(existing[0])
                from ..domain import ValidationClass
                payload["classification"] = ValidationClass(payload["classification"])
                return ValidationResult(**{**payload, "duplicate": True})
            with self._db:
                self._db.execute("INSERT INTO validations(validation_id, candidate_id, ci_run_id, revision, payload_json) VALUES (?, ?, ?, ?, ?)", (result.validation_id, result.candidate_id, result.ci_run_id, result.revision, canonical_json(result)))
            return result

    def reconcile(self, run_id: str) -> list[str]:
        issues: list[str] = []
        rows = self._db.execute("SELECT artifact_id, path, sha256 FROM artifacts WHERE run_id = ?", (run_id,)).fetchall()
        for row in rows:
            path = Path(row["path"])
            if not path.is_file():
                issues.append(f"missing artifact: {row['artifact_id']}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != row["sha256"]:
                issues.append(f"artifact hash mismatch: {row['artifact_id']}")
        if issues:
            current = self.get_run(run_id)
            if current and Stage(current["stage"]) not in {Stage.VERIFIED, Stage.FAILED}:
                self.transition(run_id, Stage.REVIEW_REQUIRED, payload_update={"recovery_issues": issues})
        return issues
