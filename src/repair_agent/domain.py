"""Stable data contracts shared by the workflow components."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def to_primitive(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [to_primitive(item) for item in value]
    return value


def normalize_repo_path(value: str) -> str:
    raw = str(value).replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
        raise ValueError(f"path must be workspace-relative: {value!r}")
    return path.as_posix()


class SourceType(StrEnum):
    FINDING = "finding"
    UT_FAILURE = "ut_failure"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: Any) -> "Severity":
        normalized = str(value or "unknown").strip().lower()
        aliases = {"sev1": cls.CRITICAL, "sev2": cls.HIGH, "sev3": cls.MEDIUM, "sev4": cls.LOW}
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            return cls.UNKNOWN


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class Stage(StrEnum):
    RECEIVED = "RECEIVED"
    REPAIRING = "REPAIRING"
    BATCH_REVIEW = "BATCH_REVIEW"
    INTEGRATING = "INTEGRATING"
    CANDIDATE_FROZEN = "CANDIDATE_FROZEN"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    SUBMITTING = "SUBMITTING"
    CI_PENDING = "CI_PENDING"
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    RETRY_INFRA = "RETRY_INFRA"
    FAILED = "FAILED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"


class ToolStatus(StrEnum):
    OK = "OK"
    EMPTY = "EMPTY"
    AMBIGUOUS = "AMBIGUOUS"
    PARTIAL = "PARTIAL"
    TRUNCATED = "TRUNCATED"
    VERSION_CHANGED = "VERSION_CHANGED"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"
    NOT_EXECUTED = "NOT_EXECUTED"


class ActionKind(StrEnum):
    FIX_CANDIDATE = "FIX_CANDIDATE"
    SUPPRESSION_CANDIDATE = "SUPPRESSION_CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    UNRESOLVED = "UNRESOLVED"
    NOT_EXECUTED = "NOT_EXECUTED"


class ValidationClass(StrEnum):
    VALIDATION_PASS = "VALIDATION_PASS"
    CODE_FAIL = "CODE_FAIL"
    INFRA_FAIL = "INFRA_FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class SubmissionStatus(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    RECONCILED = "RECONCILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True)
class Finding:
    finding_id: str
    rule_id: str
    severity: Severity
    file: str
    line: int | None
    message: str
    analysis_trace: tuple[str, ...] = ()
    scan_id: str | None = None
    base_commit: str | None = None
    symbol: str | None = None
    module: str | None = None
    lifecycle_domain: str | None = None
    finding_fingerprint: str = ""
    identity_uncertain: bool = False
    risk: RiskClass = RiskClass.UNKNOWN
    source_type: SourceType = SourceType.FINDING

    def __post_init__(self) -> None:
        if not self.finding_id.strip():
            raise ValueError("finding_id is required")
        object.__setattr__(self, "file", normalize_repo_path(self.file))
        if not self.finding_fingerprint:
            fingerprint = canonical_json(
                {
                    "rule": self.rule_id,
                    "file": self.file,
                    "line": self.line,
                    "symbol": self.symbol,
                    "message": self.message,
                }
            )
            object.__setattr__(self, "finding_fingerprint", sha256_text(fingerprint))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Finding":
        finding_id = value.get("finding_id", value.get("id"))
        if not finding_id:
            raise ValueError("known finding input requires finding_id or id")
        if not value.get("file"):
            raise ValueError("finding file is required")
        line = value.get("line")
        return cls(
            finding_id=str(finding_id),
            rule_id=str(value.get("rule_id", value.get("rule", "UNKNOWN_RULE"))),
            severity=Severity.parse(value.get("severity")),
            file=str(value.get("file", "")),
            line=int(line) if line not in (None, "") else None,
            message=str(value.get("message", "")),
            analysis_trace=tuple(str(item) for item in value.get("analysis_trace", value.get("trace", ())) or ()),
            scan_id=str(value["scan_id"]) if value.get("scan_id") is not None else None,
            base_commit=str(value["base_commit"]) if value.get("base_commit") is not None else None,
            symbol=str(value["symbol"]) if value.get("symbol") is not None else None,
            module=str(value["module"]) if value.get("module") is not None else None,
            lifecycle_domain=str(value["lifecycle_domain"]) if value.get("lifecycle_domain") is not None else None,
            finding_fingerprint=str(value.get("finding_fingerprint", "")),
            identity_uncertain=bool(value.get("identity_uncertain", False)),
            risk=_parse_risk(value.get("risk")),
        )


@dataclass(frozen=True)
class UTFailure:
    failure_id: str
    test_id: str
    severity: Severity
    file: str | None
    line: int | None
    message: str
    log_excerpt: str = ""
    base_commit: str | None = None
    module: str | None = None
    risk: RiskClass = RiskClass.UNKNOWN
    source_type: SourceType = SourceType.UT_FAILURE

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "UTFailure":
        failure_id = value.get("failure_id", value.get("id"))
        test_id = value.get("test_id", value.get("test"))
        if not failure_id or not test_id:
            raise ValueError("known UT input requires failure_id/id and test_id/test")
        line = value.get("line")
        file_value = value.get("file")
        return cls(
            failure_id=str(failure_id),
            test_id=str(test_id),
            severity=Severity.parse(value.get("severity", "high")),
            file=normalize_repo_path(str(file_value)) if file_value else None,
            line=int(line) if line not in (None, "") else None,
            message=str(value.get("message", "")),
            log_excerpt=str(value.get("log_excerpt", value.get("log", ""))),
            base_commit=str(value["base_commit"]) if value.get("base_commit") is not None else None,
            module=str(value["module"]) if value.get("module") is not None else None,
            risk=_parse_risk(value.get("risk")),
        )

    @property
    def finding_id(self) -> str:
        return self.failure_id


Issue = Finding | UTFailure


@dataclass(frozen=True)
class Budget:
    max_model_calls: int = 20
    max_tool_calls: int = 100
    max_tokens: int = 100_000
    max_wall_seconds: float = 900.0
    max_edit_attempts: int = 20


@dataclass(frozen=True)
class RepairTask:
    task_id: str
    run_id: str
    repo: str
    base_commit: str
    issues: tuple[Issue, ...]
    risk_policy: str = "default"
    budget: Budget = field(default_factory=Budget)
    mode: str = "local-demo"
    model_id: str = "unknown"


@dataclass(frozen=True)
class WorkspaceVersion:
    base_commit: str
    revision: int
    file_hashes: Mapping[str, str]


@dataclass(frozen=True)
class Observation:
    tool_call_id: str
    tool: str
    status: ToolStatus
    content: Any = None
    artifact_ref: str | None = None
    source_paths: tuple[str, ...] = ()
    workspace_revision: int = 0
    file_hashes: Mapping[str, str] = field(default_factory=dict)
    complete: bool = True
    elapsed_ms: int = 0
    error: str | None = None


@dataclass(frozen=True)
class BatchProposal:
    batch_id: str
    worker_id: str
    base_commit: str
    workspace_revision: int
    action_map: Mapping[str, ActionKind]
    changed_files: tuple[str, ...]
    diff: str
    diff_hash: str
    risk: RiskClass
    unresolved: tuple[str, ...] = ()
    not_executed: tuple[str, ...] = ()
    complete: bool = True
    review_notes: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    run_id: str
    base_commit: str
    tree_hash: str
    artifact_hash: str
    report_hash: str
    changed_files: tuple[str, ...]
    finding_ids: tuple[str, ...]
    suppression_candidate_ids: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    valid: bool = True


@dataclass(frozen=True)
class HumanApproval:
    candidate_id: str
    tree_hash: str
    approved: bool
    reviewer: str
    reason: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class SubmissionIntent:
    submission_id: str
    candidate_id: str
    candidate_tree_hash: str
    repo: str
    branch: str
    change_id: str
    fixed_commit: str
    status: SubmissionStatus = SubmissionStatus.INTENT_RECORDED
    remote_change: str | None = None
    patch_set: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ValidationResult:
    validation_id: str
    candidate_id: str
    candidate_tree_hash: str
    revision: str
    actual_tested_commit: str | None
    ci_run_id: str
    config_id: str
    backend: str
    classification: ValidationClass
    checks: Mapping[str, str]
    evidence: Mapping[str, Any] = field(default_factory=dict)
    duplicate: bool = False
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_id: str
    stage: Stage
    config_version: str
    model_id: str
    budget_used: Mapping[str, int | float]
    checkpoint_id: str | None = None
    failure_class: str | None = None
    report_path: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


def classify_risk(issue: Issue) -> RiskClass:
    if issue.risk != RiskClass.UNKNOWN:
        return issue.risk
    haystack = " ".join(
        str(getattr(issue, name, ""))
        for name in ("rule_id", "test_id", "message", "analysis_trace", "file", "symbol", "lifecycle_domain")
    ).lower()
    high_markers = ("lifecycle", "thread", "atomic", "mutex", "shared", "public", "race", "ownership")
    medium_markers = ("control", "return", "resource", "cleanup", "null", "uninitialized")
    if any(marker in haystack for marker in high_markers):
        return RiskClass.HIGH
    if any(marker in haystack for marker in medium_markers):
        return RiskClass.MEDIUM
    if issue.severity in (Severity.CRITICAL, Severity.HIGH):
        return RiskClass.MEDIUM
    return RiskClass.LOW


def issue_id(issue: Issue) -> str:
    return issue.finding_id


def issue_file(issue: Issue) -> str | None:
    return getattr(issue, "file", None)


def _parse_risk(value: Any) -> RiskClass:
    try:
        return RiskClass(str(value or RiskClass.UNKNOWN.value).lower())
    except ValueError:
        return RiskClass.UNKNOWN


def has_protected_name(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    protected_dirs = {".git", ".repair-agent", "third_party", "vendor", "dependencies", "ci", ".github"}
    return any(part in protected_dirs or part.endswith(".lock") or part == "lockfile" for part in parts)


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[^\s,;]+"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(r"\1<redacted>", result)
    return result
