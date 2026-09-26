"""SQLite RunStore with atomic artifacts and identity-bound result records."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ..domain import Candidate, HumanApproval, RunRecord, Stage, SubmissionIntent, SubmissionStatus, ValidationClass, ValidationResult, ValidationState, canonical_json, sha256_text, to_primitive, utc_now
from .trace import sanitize


class StoreError(RuntimeError):
    """Persistent state or stage transition is invalid."""


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


_TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.RECEIVED: {Stage.REPAIRING, Stage.FAILED},
    Stage.REPAIRING: {Stage.BATCH_REVIEW, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.BATCH_REVIEW: {Stage.INTEGRATING, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.INTEGRATING: {Stage.CANDIDATE_FROZEN, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.CANDIDATE_FROZEN: {Stage.HUMAN_REVIEW, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.RETRY_INFRA},
    Stage.HUMAN_REVIEW: {Stage.SUBMITTING, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.RETRY_INFRA},
    Stage.SUBMITTING: {Stage.CI_DISPATCHING, Stage.SUBMISSION_UNKNOWN, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.SUBMISSION_UNKNOWN: {Stage.SUBMITTING, Stage.CI_DISPATCHING, Stage.HUMAN_REVIEW, Stage.REVIEW_REQUIRED},
    Stage.CI_DISPATCHING: {Stage.CI_PENDING, Stage.CI_DISPATCH_UNKNOWN, Stage.RETRY_INFRA, Stage.REVIEW_REQUIRED},
    Stage.CI_DISPATCH_UNKNOWN: {Stage.CI_DISPATCHING, Stage.CI_PENDING, Stage.REVIEW_REQUIRED},
    Stage.CI_PENDING: {Stage.VERIFIED, Stage.REPAIRING, Stage.RETRY_INFRA, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.RETRY_INFRA: {Stage.CI_DISPATCHING, Stage.CI_PENDING, Stage.VERIFIED, Stage.HUMAN_REVIEW, Stage.REPAIRING, Stage.REVIEW_REQUIRED, Stage.FAILED},
    Stage.VERIFIED: {Stage.REVIEW_REQUIRED},
    Stage.REVIEW_REQUIRED: {Stage.REPAIRING, Stage.HUMAN_REVIEW, Stage.VERIFIED, Stage.RETRY_INFRA, Stage.FAILED},
    Stage.FAILED: {Stage.REPAIRING, Stage.REVIEW_REQUIRED},
}


class RunStore:
    _lifecycle_locks = tuple(threading.RLock() for _ in range(64))

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
                CREATE TABLE IF NOT EXISTS ci_accumulators (
                    candidate_id TEXT NOT NULL, ci_run_id TEXT NOT NULL, revision TEXT NOT NULL,
                    actual_tested_commit TEXT, checks_json TEXT NOT NULL, state TEXT NOT NULL,
                    event_hashes_json TEXT NOT NULL, identity_conflict INTEGER NOT NULL DEFAULT 0,
                    config_id TEXT NOT NULL DEFAULT '', backend TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, ci_run_id, revision)
                );
                CREATE INDEX IF NOT EXISTS traces_by_run ON traces(run_id, sequence_no);
                CREATE INDEX IF NOT EXISTS artifacts_by_run ON artifacts(run_id);
                """
            )
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                columns = {row["name"] for row in self._db.execute("PRAGMA table_info(ci_accumulators)").fetchall()}
                for name, declaration in (
                    ("config_id", "TEXT NOT NULL DEFAULT ''"),
                    ("backend", "TEXT NOT NULL DEFAULT ''"),
                    ("evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
                ):
                    if name not in columns:
                        self._db.execute(f"ALTER TABLE ci_accumulators ADD COLUMN {name} {declaration}")
                duplicate = self._db.execute(
                    "SELECT 1 FROM traces GROUP BY run_id,sequence_no HAVING COUNT(*)>1 LIMIT 1"
                ).fetchone()
                if duplicate:
                    last_by_run: dict[str, int] = {}
                    for row in self._db.execute("SELECT trace_id,run_id,sequence_no FROM traces ORDER BY run_id,sequence_no,trace_id").fetchall():
                        sequence = max(int(row["sequence_no"]), last_by_run.get(row["run_id"], 0) + 1)
                        if sequence != int(row["sequence_no"]):
                            self._db.execute("UPDATE traces SET sequence_no=? WHERE trace_id=?", (sequence, row["trace_id"]))
                        last_by_run[row["run_id"]] = sequence
                self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS traces_run_sequence ON traces(run_id,sequence_no)")
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def lifecycle_guard(self, run_id: str):
        """Serialize stage-sensitive filesystem actions with run lifecycle updates."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise StoreError("run id is not a safe lifecycle lock key")
        lock_path = self.root / f".run-lifecycle-{run_id}.lock"
        lock_id = hashlib.sha256(os.path.normcase(run_id).encode("utf-8")).hexdigest()
        thread_lock = self._lifecycle_locks[int(lock_id[:8], 16) % len(self._lifecycle_locks)]
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

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def create_run(self, record: RunRecord, payload: Mapping[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO runs(run_id, task_id, stage, payload_json, updated_at) VALUES (?, ?, ?, ?, ?)", (record.run_id, record.task_id, record.stage.value, canonical_json(sanitize(payload)), utc_now()))

    def update_worker_budget(self, run_id: str, worker_id: str, usage: Mapping[str, Any]) -> dict[str, int | float | bool]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", worker_id):
            raise StoreError("worker usage key is invalid")
        fields = ("model_calls", "model_attempts", "model_retries", "tool_calls", "tokens", "edit_attempts", "chunk_actions", "changed_files", "diff_lines")
        snapshot: dict[str, int | float | bool] = {key: max(0, int(usage.get(key, 0))) for key in fields}
        snapshot["elapsed_seconds"] = max(0.0, float(usage.get("elapsed_seconds", 0.0)))
        snapshot["token_usage_known"] = bool(usage.get("token_usage_known", True))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT payload_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise StoreError(f"run not found: {run_id}")
                payload = json.loads(row[0])
                workers = payload.get("worker_budget_usage", {})
                if not isinstance(workers, dict):
                    workers = {}
                workers[worker_id] = snapshot
                payload["worker_budget_usage"] = workers
                aggregate: dict[str, int | float | bool] = {
                    key: sum(int(value.get(key, 0)) for value in workers.values()) for key in fields
                }
                aggregate["wall_seconds"] = max((float(value.get("elapsed_seconds", 0.0)) for value in workers.values()), default=0.0)
                aggregate["token_usage_known"] = all(bool(value.get("token_usage_known", True)) for value in workers.values())
                payload["budget_used"] = aggregate
                self._db.execute("UPDATE runs SET payload_json=?,updated_at=? WHERE run_id=?", (canonical_json(sanitize(payload)), utc_now(), run_id))
                self._db.commit()
                return aggregate
            except Exception:
                self._db.rollback()
                raise

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            payload.update({"run_id": row["run_id"], "task_id": row["task_id"], "stage": row["stage"], "updated_at": row["updated_at"]})
            return payload

    def transition(self, run_id: str, stage: Stage, *, payload_update: Mapping[str, Any] | None = None, failure_class: str | None = None) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
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
                payload.update(sanitize(payload_update or {}))
                if failure_class:
                    payload["failure_class"] = failure_class
                self._db.execute("UPDATE runs SET stage = ?, payload_json = ?, updated_at = ? WHERE run_id = ?", (stage.value, canonical_json(sanitize(payload)), utc_now(), run_id))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def record_trace(self, run_id: str, payload: Any) -> int:
        sanitized = sanitize(payload)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM traces WHERE run_id = ?", (run_id,)).fetchone()
                sequence = int(row[0])
                cursor = self._db.execute("INSERT INTO traces(run_id, sequence_no, payload_json, created_at) VALUES (?, ?, ?, ?)", (run_id, sequence, canonical_json(sanitized), utc_now()))
                self._db.commit()
                return int(cursor.lastrowid)
            except Exception:
                self._db.rollback()
                raise

    def save_artifact(self, run_id: str, artifact_id: str, kind: str, content: str | bytes, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", artifact_id):
            raise StoreError("run and artifact identifiers must be safe single path components")
        run_dir = (self.artifact_root / run_id).resolve()
        run_dir.relative_to(self.artifact_root)
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
            self._db.execute("INSERT OR REPLACE INTO checkpoints(checkpoint_id, run_id, stage, artifact_ids_json, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (checkpoint_id, run_id, stage.value, canonical_json(artifact_ids), canonical_json(sanitize(payload)), utc_now()))

    def save_candidate(self, candidate: Candidate) -> None:
        if not candidate.git_tree_oid or not candidate.candidate_commit:
            raise StoreError("candidate must carry a Git tree OID and immutable candidate commit")
        with self._lock, self._db:
            self._db.execute("INSERT INTO candidates(candidate_id, run_id, payload_json) VALUES (?, ?, ?)", (candidate.candidate_id, candidate.run_id, canonical_json(candidate)))

    def save_frozen_candidate(self, candidate: Candidate, artifact_ids: list[str]) -> None:
        """Commit the candidate and its checkpoint atomically after artifacts exist."""
        if not candidate.git_tree_oid or not candidate.candidate_commit:
            raise StoreError("candidate must carry a Git tree OID and immutable candidate commit")
        if len(set(artifact_ids)) != 2:
            raise StoreError("a frozen candidate requires exactly its diff and report artifacts")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute(
                    "SELECT artifact_id,run_id,kind,sha256 FROM artifacts WHERE artifact_id IN (?,?)",
                    tuple(artifact_ids),
                ).fetchall()
                by_kind = {row["kind"]: row for row in rows if row["run_id"] == candidate.run_id}
                if set(by_kind) != {"candidate-diff", "candidate-report"}:
                    raise StoreError("frozen candidate artifacts are missing or have the wrong kind/run")
                if (by_kind["candidate-diff"]["sha256"] != candidate.artifact_hash
                        or by_kind["candidate-report"]["sha256"] != candidate.report_hash):
                    raise StoreError("frozen candidate artifact hashes do not match candidate identity")
                self._db.execute("INSERT INTO candidates(candidate_id,run_id,payload_json) VALUES (?,?,?)", (candidate.candidate_id, candidate.run_id, canonical_json(candidate)))
                self._db.execute(
                    "INSERT INTO checkpoints(checkpoint_id,run_id,stage,artifact_ids_json,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (f"checkpoint-{candidate.candidate_id}", candidate.run_id, Stage.CANDIDATE_FROZEN.value,
                     canonical_json(artifact_ids), canonical_json(sanitize({"candidate_id": candidate.candidate_id, "tree_hash": candidate.tree_hash})), utc_now()),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            row = self._db.execute("SELECT payload_json FROM candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
            if row is None:
                return None
            return Candidate(**json.loads(row[0]))

    def list_candidates(self, run_id: str) -> tuple[Candidate, ...]:
        with self._lock:
            rows = self._db.execute("SELECT payload_json FROM candidates WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
            return tuple(Candidate(**json.loads(row[0])) for row in rows)

    def list_validations(self, candidate_id: str) -> tuple[ValidationResult, ...]:
        with self._lock:
            rows = self._db.execute("SELECT payload_json FROM validations WHERE candidate_id = ? ORDER BY validation_id", (candidate_id,)).fetchall()
            values = []
            for row in rows:
                payload = json.loads(row[0])
                from ..domain import ValidationClass
                payload["classification"] = ValidationClass(payload["classification"])
                payload["state"] = ValidationState(payload.get("state", ValidationState.FINAL.value))
                values.append(ValidationResult(**payload))
            return tuple(values)

    def save_approval(self, approval: HumanApproval) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                candidate_row = self._db.execute("SELECT payload_json FROM candidates WHERE candidate_id=?", (approval.candidate_id,)).fetchone()
                run_row = self._db.execute("SELECT run_id, stage, payload_json FROM runs WHERE run_id=(SELECT run_id FROM candidates WHERE candidate_id=?)", (approval.candidate_id,)).fetchone()
                if candidate_row is None or run_row is None:
                    raise StoreError("approval candidate or run does not exist")
                candidate = Candidate(**json.loads(candidate_row[0]))
                if (candidate.tree_hash != approval.tree_hash or candidate.git_tree_oid != approval.git_tree_oid
                        or candidate.candidate_commit != approval.candidate_commit):
                    raise StoreError("approval is not bound to the current candidate identity")
                current_stage = Stage(run_row["stage"])
                if current_stage not in {Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.REVIEW_REQUIRED}:
                    raise StoreError(f"approval cannot change from stage {current_stage.value}")
                if current_stage != Stage.HUMAN_REVIEW and Stage.HUMAN_REVIEW not in _TRANSITIONS.get(current_stage, set()):
                    raise StoreError(f"invalid stage transition {current_stage.value} -> HUMAN_REVIEW")
                self._db.execute("INSERT OR REPLACE INTO approvals(candidate_id, tree_hash, payload_json) VALUES (?, ?, ?)", (approval.candidate_id, approval.tree_hash, canonical_json(sanitize(approval))))
                run_payload = json.loads(run_row["payload_json"])
                run_payload["approval"] = sanitize(approval)
                self._db.execute("UPDATE runs SET stage=?,payload_json=?,updated_at=? WHERE run_id=?", (Stage.HUMAN_REVIEW.value, canonical_json(sanitize(run_payload)), utc_now(), run_row["run_id"]))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def get_approval(self, candidate_id: str) -> HumanApproval | None:
        with self._lock:
            row = self._db.execute("SELECT payload_json FROM approvals WHERE candidate_id = ?", (candidate_id,)).fetchone()
            return HumanApproval(**json.loads(row[0])) if row else None

    def save_submission(self, intent: SubmissionIntent) -> None:
        candidate = self.get_candidate(intent.candidate_id)
        if (candidate is None or candidate.tree_hash != intent.candidate_tree_hash
                or candidate.git_tree_oid != intent.candidate_git_tree_oid
                or candidate.candidate_commit != intent.candidate_commit):
            raise StoreError("submission intent does not match candidate identity")
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO submissions(submission_id, candidate_id, status, payload_json) VALUES (?, ?, ?, ?)", (intent.submission_id, intent.candidate_id, intent.status.value, canonical_json(intent)))

    def begin_ci_dispatch(self, run_id: str, candidate_id: str, submission_id: str, dispatch_id: str, *, retry_unknown: bool = False) -> None:
        """Persist the dispatch identity before any CI side effect can occur."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                run_row = self._db.execute("SELECT stage,payload_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                candidate_row = self._db.execute("SELECT run_id FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                submission_row = self._db.execute("SELECT candidate_id,status FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()
                if run_row is None or candidate_row is None or candidate_row["run_id"] != run_id:
                    raise StoreError("CI dispatch candidate or run does not exist")
                if submission_row is None or submission_row["candidate_id"] != candidate_id or submission_row["status"] != SubmissionStatus.SUBMITTED.value:
                    raise StoreError("CI dispatch requires a confirmed Gerrit submission")
                stage = Stage(run_row["stage"])
                payload = json.loads(run_row["payload_json"])
                if payload.get("submission_id") != submission_id:
                    raise StoreError("CI dispatch submission does not match the run's current submission")
                allowed = {Stage.SUBMITTING, Stage.SUBMISSION_UNKNOWN}
                if retry_unknown:
                    allowed |= {Stage.CI_DISPATCH_UNKNOWN}
                    if stage == Stage.RETRY_INFRA and payload.get("ci_dispatch_state") == "NOT_ACCEPTED":
                        allowed.add(Stage.RETRY_INFRA)
                if stage not in allowed:
                    raise StoreError(f"CI dispatch cannot start from stage {stage.value}")
                existing_id = payload.get("ci_dispatch_id")
                if existing_id and existing_id != dispatch_id:
                    raise StoreError("CI dispatch retry must reuse the original idempotency key")
                payload.update({"candidate_id": candidate_id, "submission_id": submission_id, "ci_dispatch_id": dispatch_id, "ci_dispatch_state": "IN_FLIGHT"})
                self._db.execute("UPDATE runs SET stage=?,payload_json=?,updated_at=? WHERE run_id=?", (Stage.CI_DISPATCHING.value, canonical_json(sanitize(payload)), utc_now(), run_id))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def finish_ci_dispatch(self, run_id: str, dispatch_id: str, *, accepted: bool | None, ci_run_id: str | None = None, backend: str = "", missing_contract: tuple[str, ...] = ()) -> Stage:
        """Atomically persist the known or ambiguous outcome of a CI dispatch."""
        if accepted is True and ci_run_id:
            target, outcome, failure = Stage.CI_PENDING, "ACCEPTED", None
        elif accepted is False:
            target, outcome, failure = Stage.RETRY_INFRA, "NOT_ACCEPTED", "CI_DISPATCH_NOT_ACCEPTED"
        else:
            target, outcome, failure = Stage.CI_DISPATCH_UNKNOWN, "UNKNOWN", None
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT stage,payload_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise StoreError(f"run not found: {run_id}")
                payload = json.loads(row["payload_json"])
                if payload.get("ci_dispatch_id") != dispatch_id:
                    raise StoreError("CI dispatch outcome does not match the recorded intent")
                current = Stage(row["stage"])
                if current == Stage.CI_PENDING and ci_run_id and payload.get("ci_run_id") == ci_run_id:
                    self._db.commit()
                    return current
                if current != Stage.CI_DISPATCHING and payload.get("ci_dispatch_state") == "ACCEPTED" and (not ci_run_id or payload.get("ci_run_id") == ci_run_id):
                    self._db.commit()
                    return current
                if current != Stage.CI_DISPATCHING:
                    raise StoreError(f"CI dispatch outcome cannot be recorded from stage {current.value}")
                payload.update({"ci_dispatch_state": outcome, "ci_dispatch_backend": backend, "ci_dispatch_missing_contract": list(missing_contract)})
                if ci_run_id:
                    payload["ci_run_id"] = ci_run_id
                if failure:
                    payload["failure_class"] = failure
                else:
                    payload.pop("failure_class", None)
                if target not in _TRANSITIONS.get(current, set()):
                    raise StoreError(f"invalid stage transition {current.value} -> {target.value}")
                self._db.execute("UPDATE runs SET stage=?,payload_json=?,updated_at=? WHERE run_id=?", (target.value, canonical_json(sanitize(payload)), utc_now(), run_id))
                self._db.commit()
                return target
            except Exception:
                self._db.rollback()
                raise

    def claim_submission(self, intent: SubmissionIntent) -> None:
        """Atomically bind one approved candidate to one in-flight submission."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                candidate_row = self._db.execute("SELECT run_id,payload_json FROM candidates WHERE candidate_id=?", (intent.candidate_id,)).fetchone()
                approval_row = self._db.execute("SELECT payload_json FROM approvals WHERE candidate_id=?", (intent.candidate_id,)).fetchone()
                if candidate_row is None or approval_row is None:
                    raise StoreError("submission candidate or approval does not exist")
                candidate = Candidate(**json.loads(candidate_row["payload_json"]))
                approval = HumanApproval(**json.loads(approval_row[0]))
                if (not approval.approved or approval.tree_hash != candidate.tree_hash
                        or approval.git_tree_oid != candidate.git_tree_oid
                        or approval.candidate_commit != candidate.candidate_commit):
                    raise StoreError("submission requires approval bound to the current candidate identity")
                if (candidate.tree_hash != intent.candidate_tree_hash
                        or candidate.git_tree_oid != intent.candidate_git_tree_oid
                        or candidate.candidate_commit != intent.candidate_commit):
                    raise StoreError("submission intent does not match candidate identity")
                run_row = self._db.execute("SELECT stage,payload_json FROM runs WHERE run_id=?", (candidate_row["run_id"],)).fetchone()
                if run_row is None or Stage(run_row["stage"]) != Stage.HUMAN_REVIEW:
                    stage = run_row["stage"] if run_row is not None else "missing"
                    raise StoreError(f"candidate cannot be claimed for submission from stage {stage}")
                self._db.execute("INSERT INTO submissions(submission_id,candidate_id,status,payload_json) VALUES (?,?,?,?)", (intent.submission_id, intent.candidate_id, intent.status.value, canonical_json(intent)))
                payload = json.loads(run_row["payload_json"])
                payload.update({"submission_id": intent.submission_id, "change_id": intent.change_id, "candidate_commit": intent.candidate_commit})
                self._db.execute("UPDATE runs SET stage=?,payload_json=?,updated_at=? WHERE run_id=?", (Stage.SUBMITTING.value, canonical_json(sanitize(payload)), utc_now(), candidate_row["run_id"]))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def get_submission(self, submission_id: str) -> SubmissionIntent | None:
        with self._lock:
            row = self._db.execute("SELECT payload_json FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()
            if row is None:
                return None
            payload = json.loads(row[0])
            payload.setdefault("candidate_commit", payload.get("fixed_commit", ""))
            payload.setdefault("candidate_git_tree_oid", "")
            payload["status"] = SubmissionStatus(payload.get("status", SubmissionStatus.INTENT_RECORDED.value))
            return SubmissionIntent(**payload)

    def list_submissions(self, candidate_id: str) -> tuple[SubmissionIntent, ...]:
        with self._lock:
            rows = self._db.execute("SELECT submission_id FROM submissions WHERE candidate_id=? ORDER BY submission_id", (candidate_id,)).fetchall()
            return tuple(intent for row in rows if (intent := self.get_submission(row[0])) is not None)

    def _persist_validation_locked(self, result: ValidationResult) -> ValidationResult:
        candidate = self.get_candidate(result.candidate_id)
        if (candidate is None or candidate.tree_hash != result.candidate_tree_hash
                or (result.candidate_commit and candidate.candidate_commit != result.candidate_commit)):
            raise StoreError("validation result does not match a stored candidate")
        if result.classification == ValidationClass.VALIDATION_PASS and (result.actual_tested_commit != candidate.candidate_commit or result.candidate_commit != candidate.candidate_commit):
            raise StoreError("validation PASS is not bound to the candidate commit")
        existing = self._db.execute("SELECT payload_json FROM validations WHERE candidate_id = ? AND ci_run_id = ? AND revision = ?", (result.candidate_id, result.ci_run_id, result.revision)).fetchone()
        if existing:
            payload = json.loads(existing[0])
            payload["classification"] = ValidationClass(payload["classification"])
            payload["state"] = ValidationState(payload.get("state", ValidationState.FINAL.value))
            existing_result = ValidationResult(**payload)
            conflict_downgrade = (
                existing_result.classification == ValidationClass.VALIDATION_PASS
                and result.classification == ValidationClass.INCONCLUSIVE
                and (result.evidence.get("identity_conflict") or result.evidence.get("callback_conflict"))
            )
            inconclusive_refinement = (
                existing_result.classification == ValidationClass.INCONCLUSIVE
                and result.classification != ValidationClass.INCONCLUSIVE
                and result.actual_tested_commit == candidate.candidate_commit
                and not existing_result.evidence.get("identity_conflict")
                and not existing_result.evidence.get("callback_conflict")
            )
            if conflict_downgrade or inconclusive_refinement:
                result = replace(result, validation_id=existing_result.validation_id, created_at=existing_result.created_at)
                self._db.execute("UPDATE validations SET payload_json=? WHERE candidate_id=? AND ci_run_id=? AND revision=?", (canonical_json(result), result.candidate_id, result.ci_run_id, result.revision))
                return result
            return replace(existing_result, duplicate=True)
        self._db.execute("INSERT INTO validations(validation_id, candidate_id, ci_run_id, revision, payload_json) VALUES (?, ?, ?, ?, ?)", (result.validation_id, result.candidate_id, result.ci_run_id, result.revision, canonical_json(result)))
        return result

    def save_validation(self, result: ValidationResult) -> ValidationResult:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                persisted = self._persist_validation_locked(result)
                self._db.commit()
                return persisted
            except Exception:
                self._db.rollback()
                raise

    def save_local_validation_and_transition(
        self,
        result: ValidationResult,
        *,
        run_id: str,
        payload_update: Mapping[str, Any] | None = None,
    ) -> tuple[ValidationResult, Stage]:
        """Persist local validation evidence and its candidate-bound run state atomically."""
        if result.state != ValidationState.FINAL or result.backend.lower() != "local":
            raise StoreError("local transition requires a final local validation result")
        target_by_class = {
            ValidationClass.VALIDATION_PASS: Stage.HUMAN_REVIEW,
            ValidationClass.CODE_FAIL: Stage.REPAIRING,
            ValidationClass.INFRA_FAIL: Stage.RETRY_INFRA,
            ValidationClass.INCONCLUSIVE: Stage.REVIEW_REQUIRED,
        }
        failure_by_class = {
            ValidationClass.CODE_FAIL: "CODE_FAIL",
            ValidationClass.INFRA_FAIL: "INFRA_FAIL",
            ValidationClass.INCONCLUSIVE: "INCONCLUSIVE",
        }
        allowed_stages = {Stage.CANDIDATE_FROZEN, Stage.HUMAN_REVIEW, Stage.REVIEW_REQUIRED, Stage.RETRY_INFRA}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                candidate = self.get_candidate(result.candidate_id)
                if candidate is None or candidate.run_id != run_id:
                    raise StoreError("local validation run does not match candidate")
                row = self._db.execute("SELECT stage,payload_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise StoreError(f"run not found: {run_id}")
                current = Stage(row["stage"])
                payload = json.loads(row["payload_json"])
                if current not in allowed_stages:
                    raise StoreError(f"local validation cannot update run from stage {current.value}")
                if payload.get("candidate_id") != result.candidate_id:
                    raise StoreError("local validation does not match the run's current candidate")
                if result.classification == ValidationClass.CODE_FAIL and not (payload_update or {}).get("new_attempt_id"):
                    raise StoreError("code-failing local validation requires a new attempt id")
                persisted = self._persist_validation_locked(result)
                target = target_by_class[persisted.classification]
                if target != current and target not in _TRANSITIONS.get(current, set()):
                    raise StoreError(f"invalid local validation transition {current.value} -> {target.value}")
                payload.update(sanitize(payload_update or {}))
                payload["validation_id"] = persisted.validation_id
                payload["validation_backend"] = persisted.backend
                failure = failure_by_class.get(persisted.classification)
                if failure:
                    payload["failure_class"] = failure
                else:
                    payload.pop("failure_class", None)
                self._db.execute(
                    "UPDATE runs SET stage=?,payload_json=?,updated_at=? WHERE run_id=?",
                    (target.value, canonical_json(sanitize(payload)), utc_now(), run_id),
                )
                self._db.commit()
                return persisted, target
            except Exception:
                self._db.rollback()
                raise

    def save_validation_and_transition(self, result: ValidationResult, *, run_id: str, payload_update: Mapping[str, Any] | None = None, require_accumulator: bool = True) -> tuple[ValidationResult, Stage]:
        """Persist final CI evidence and its derived run state in one transaction."""
        if result.state != ValidationState.FINAL:
            raise StoreError("only final CI validation results can transition run state")
        target_by_class = {
            ValidationClass.VALIDATION_PASS: Stage.VERIFIED,
            ValidationClass.CODE_FAIL: Stage.REPAIRING,
            ValidationClass.INFRA_FAIL: Stage.RETRY_INFRA,
            ValidationClass.INCONCLUSIVE: Stage.REVIEW_REQUIRED,
        }
        failure_by_class = {
            ValidationClass.CODE_FAIL: "CODE_FAIL",
            ValidationClass.INFRA_FAIL: "INFRA_FAIL",
            ValidationClass.INCONCLUSIVE: "INCONCLUSIVE",
        }
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                candidate = self.get_candidate(result.candidate_id)
                if candidate is None or candidate.run_id != run_id:
                    raise StoreError("validation run does not match candidate")
                accumulator = self._db.execute(
                    "SELECT actual_tested_commit,checks_json,state,identity_conflict,config_id,backend FROM ci_accumulators WHERE candidate_id=? AND ci_run_id=? AND revision=?",
                    (result.candidate_id, result.ci_run_id, result.revision),
                ).fetchone()
                if (accumulator is None or accumulator["state"] != "FINAL") and require_accumulator:
                    raise StoreError("validation has no matching final CI accumulator")
                if accumulator is not None and accumulator["state"] == "FINAL":
                    accumulated_checks = json.loads(accumulator["checks_json"])
                    check_conflict = any(str(value).upper() == "CONFLICT" for value in accumulated_checks.values())
                    identity_conflict = bool(accumulator["identity_conflict"])
                    metadata_conflict = (
                        not accumulator["config_id"] or not accumulator["backend"]
                        or accumulator["config_id"] != result.config_id
                        or accumulator["backend"] != result.backend
                    )
                    commit_conflict = bool(accumulator["actual_tested_commit"] and accumulator["actual_tested_commit"] != candidate.candidate_commit)
                else:
                    check_conflict = False
                    identity_conflict = False
                    metadata_conflict = False
                    commit_conflict = False
                if identity_conflict or check_conflict or metadata_conflict or commit_conflict:
                    evidence = dict(result.evidence)
                    evidence["identity_conflict"] = bool(identity_conflict or commit_conflict)
                    evidence["callback_conflict"] = bool(check_conflict or metadata_conflict)
                    result = replace(result, classification=ValidationClass.INCONCLUSIVE, evidence=sanitize(evidence))
                persisted = self._persist_validation_locked(result)
                row = self._db.execute("SELECT stage,payload_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise StoreError(f"run not found: {run_id}")
                current = Stage(row["stage"])
                target = target_by_class[persisted.classification]
                allowed = {Stage.CI_PENDING, Stage.RETRY_INFRA, Stage.VERIFIED, Stage.REVIEW_REQUIRED}
                if current not in allowed:
                    raise StoreError(f"validation cannot update run from stage {current.value}")
                if current == Stage.VERIFIED and target in {Stage.REPAIRING, Stage.RETRY_INFRA}:
                    target = Stage.REVIEW_REQUIRED
                if target != current and target not in _TRANSITIONS.get(current, set()):
                    raise StoreError(f"invalid stage transition {current.value} -> {target.value}")
                payload = json.loads(row["payload_json"])
                payload.update(sanitize(payload_update or {}))
                payload["validation_id"] = persisted.validation_id
                payload["validation_backend"] = persisted.backend
                failure = failure_by_class.get(persisted.classification)
                if failure:
                    payload["failure_class"] = failure
                else:
                    payload.pop("failure_class", None)
                self._db.execute("UPDATE runs SET stage=?,payload_json=?,updated_at=? WHERE run_id=?", (target.value, canonical_json(sanitize(payload)), utc_now(), run_id))
                self._db.commit()
                return persisted, target
            except Exception:
                self._db.rollback()
                raise

    def merge_ci_checks(
        self, *, candidate_id: str, ci_run_id: str, revision: str,
        actual_tested_commit: str | None, checks: Mapping[str, str],
        required_checks: tuple[str, ...], final: bool = False,
        config_id: str = "", backend: str = "", evidence: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, str], str, bool, bool, bool, str | None]:
        event_hash = sha256_text(canonical_json({"checks": checks, "final": final, "actual_tested_commit": actual_tested_commit, "config_id": config_id, "backend": backend}))
        pending = {"", "UNKNOWN", "QUEUED", "PENDING", "RUNNING", "IN_PROGRESS", "PARTIAL"}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM ci_accumulators WHERE candidate_id=? AND ci_run_id=? AND revision=?",
                    (candidate_id, ci_run_id, revision),
                ).fetchone()
                merged = json.loads(row["checks_json"]) if row else {}
                events = set(json.loads(row["event_hashes_json"])) if row else set()
                previous_state = row["state"] if row else "UNKNOWN"
                duplicate = event_hash in events
                old_commit = row["actual_tested_commit"] if row else None
                identity_conflict = bool(row["identity_conflict"]) if row else False
                old_config = row["config_id"] if row else ""
                old_backend = row["backend"] if row else ""
                if old_commit and actual_tested_commit and old_commit != actual_tested_commit:
                    identity_conflict = True
                if (old_config and config_id and old_config != config_id) or (old_backend and backend and old_backend != backend):
                    identity_conflict = True
                if not duplicate:
                    for name, value in checks.items():
                        incoming = str(value).upper()
                        current = str(merged.get(name, "UNKNOWN")).upper()
                        if current in pending or current == incoming:
                            merged[name] = incoming
                        elif incoming not in pending and current not in pending and incoming != current:
                            merged[name] = "CONFLICT"
                    events.add(event_hash)
                resolved_commit = old_commit or actual_tested_commit
                resolved_config = old_config or config_id
                resolved_backend = old_backend or backend
                stored_evidence = json.loads(row["evidence_json"]) if row else {}
                if not duplicate and evidence:
                    stored_evidence.update(sanitize(evidence))
                check_conflict = any(str(value).upper() == "CONFLICT" for value in merged.values())
                all_final = all(str(merged.get(name, "UNKNOWN")).upper() not in pending for name in required_checks)
                state = "FINAL" if previous_state == "FINAL" or final or all_final else ("PARTIAL" if merged or events else "UNKNOWN")
                self._db.execute(
                    "INSERT INTO ci_accumulators(candidate_id,ci_run_id,revision,actual_tested_commit,checks_json,state,event_hashes_json,identity_conflict,config_id,backend,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(candidate_id,ci_run_id,revision) DO UPDATE SET actual_tested_commit=excluded.actual_tested_commit,checks_json=excluded.checks_json,state=excluded.state,event_hashes_json=excluded.event_hashes_json,identity_conflict=excluded.identity_conflict,config_id=excluded.config_id,backend=excluded.backend,evidence_json=excluded.evidence_json,updated_at=excluded.updated_at",
                    (candidate_id, ci_run_id, revision, resolved_commit, canonical_json(merged), state, canonical_json(sorted(events)), int(identity_conflict), resolved_config, resolved_backend, canonical_json(stored_evidence), utc_now()),
                )
                self._db.commit()
                return merged, state, duplicate, identity_conflict, check_conflict, resolved_commit
            except Exception:
                self._db.rollback()
                raise

    def list_ci_accumulators(self, candidate_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM ci_accumulators WHERE candidate_id=? ORDER BY updated_at", (candidate_id,)).fetchall()
            return tuple({"candidate_id": candidate_id, "ci_run_id": row["ci_run_id"], "revision": row["revision"], "actual_tested_commit": row["actual_tested_commit"], "checks": json.loads(row["checks_json"]), "state": row["state"], "identity_conflict": bool(row["identity_conflict"]), "config_id": row["config_id"], "backend": row["backend"], "evidence": json.loads(row["evidence_json"]), "updated_at": row["updated_at"]} for row in rows)

    def reconcile(self, run_id: str) -> list[str]:
        with self._lock:
            return self._reconcile_locked(run_id)

    def _reconcile_locked(self, run_id: str) -> list[str]:
        issues: list[str] = []
        rows = self._db.execute("SELECT artifact_id, path, sha256 FROM artifacts WHERE run_id = ?", (run_id,)).fetchall()
        registered: set[Path] = set()
        artifact_hashes = {row["artifact_id"]: row["sha256"] for row in rows}
        run_dir = (self.artifact_root / run_id).resolve()
        try:
            run_dir.relative_to(self.artifact_root)
        except ValueError:
            return ["run artifact directory escapes the configured artifact root"]
        for row in rows:
            raw_path = Path(row["path"])
            path = raw_path.resolve()
            try:
                path.relative_to(run_dir)
            except ValueError:
                issues.append(f"artifact path escapes run directory: {row['artifact_id']}")
                continue
            registered.add(path)
            if raw_path.is_symlink() or not path.is_file():
                issues.append(f"missing artifact: {row['artifact_id']}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != row["sha256"]:
                issues.append(f"artifact hash mismatch: {row['artifact_id']}")
        if run_dir.is_dir():
            for path in run_dir.glob("*.bin"):
                resolved = path.resolve()
                if resolved not in registered:
                    issues.append(f"unregistered artifact file: {path.name}")
        candidate_rows = self._db.execute("SELECT candidate_id,payload_json FROM candidates WHERE run_id=?", (run_id,)).fetchall()
        candidate_ids = {row["candidate_id"] for row in candidate_rows}
        candidate_artifacts: dict[str, set[str]] = {}
        for row in self._db.execute("SELECT artifact_id,kind FROM artifacts WHERE run_id=? AND kind IN ('candidate-diff','candidate-report')", (run_id,)).fetchall():
            suffix = "-diff" if row["kind"] == "candidate-diff" else "-report"
            if row["artifact_id"].endswith(suffix):
                candidate_artifacts.setdefault(row["artifact_id"][:-len(suffix)], set()).add(row["kind"])
        for candidate_id, kinds in candidate_artifacts.items():
            if candidate_id not in candidate_ids:
                issues.append(f"candidate artifacts have no committed candidate record: {candidate_id}")
            elif kinds != {"candidate-diff", "candidate-report"}:
                issues.append(f"candidate artifact pair is incomplete: {candidate_id}")
        current_run = self.get_run(run_id)
        if current_run and current_run["stage"] == Stage.INTEGRATING.value and not candidate_rows:
            issues.append("integration/freeze was interrupted before a candidate record was committed")
        for row in candidate_rows:
            candidate = Candidate(**json.loads(row["payload_json"]))
            for suffix, expected_hash in (("diff", candidate.artifact_hash), ("report", candidate.report_hash)):
                artifact_id = f"{candidate.candidate_id}-{suffix}"
                if artifact_hashes.get(artifact_id) != expected_hash:
                    issues.append(f"candidate artifact record missing or mismatched: {artifact_id}")
        known_artifact_ids = set(artifact_hashes)
        for row in self._db.execute("SELECT checkpoint_id,artifact_ids_json FROM checkpoints WHERE run_id=?", (run_id,)).fetchall():
            missing = sorted(set(json.loads(row["artifact_ids_json"])) - known_artifact_ids)
            if missing:
                issues.append(f"checkpoint {row['checkpoint_id']} references missing artifacts: {', '.join(missing)}")
        if issues:
            current = self.get_run(run_id)
            if current:
                current_stage = Stage(current["stage"])
                uncertain_side_effect_stages = {
                    Stage.SUBMITTING,
                    Stage.SUBMISSION_UNKNOWN,
                    Stage.CI_DISPATCHING,
                    Stage.CI_DISPATCH_UNKNOWN,
                }
                if current_stage in uncertain_side_effect_stages:
                    self.transition(run_id, current_stage, payload_update={"recovery_issues": issues})
                elif current_stage == Stage.REVIEW_REQUIRED:
                    self.transition(run_id, current_stage, payload_update={"recovery_issues": issues})
                else:
                    target = Stage.FAILED if current_stage == Stage.RECEIVED else Stage.REVIEW_REQUIRED
                    try:
                        self.transition(run_id, target, payload_update={"recovery_issues": issues})
                    except StoreError:
                        self.transition(run_id, Stage.REVIEW_REQUIRED, payload_update={"recovery_issues": issues})
        return issues
