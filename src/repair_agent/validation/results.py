"""Independent validation identity and result classification."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Mapping

from ..domain import Candidate, ValidationClass, ValidationResult


@dataclass(frozen=True)
class ValidationPolicy:
    required_checks: tuple[str, ...] = ("build", "ut", "scan")
    allow_simulated: bool = True


class IndependentValidator:
    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        self.policy = policy or ValidationPolicy()

    def classify(
        self,
        *,
        candidate: Candidate,
        revision: str,
        actual_tested_commit: str | None,
        ci_run_id: str,
        config_id: str,
        backend: str,
        checks: Mapping[str, str],
        evidence: Mapping[str, object] | None = None,
    ) -> ValidationResult:
        if not candidate.valid:
            classification = ValidationClass.INCONCLUSIVE
        elif actual_tested_commit is None:
            classification = ValidationClass.INCONCLUSIVE
        elif any(check not in checks for check in self.policy.required_checks):
            classification = ValidationClass.INCONCLUSIVE
        elif any(str(checks.get(check, "")).upper() in {"INFRA_FAIL", "INFRA", "TIMEOUT_INFRA"} for check in self.policy.required_checks):
            classification = ValidationClass.INFRA_FAIL
        elif any(str(checks.get(check, "")).upper() in {"FAIL", "CODE_FAIL", "ERROR"} for check in self.policy.required_checks):
            classification = ValidationClass.CODE_FAIL
        elif any(str(checks.get(check, "")).upper() not in {"PASS", "VALIDATION_PASS"} for check in self.policy.required_checks):
            classification = ValidationClass.INCONCLUSIVE
        else:
            classification = ValidationClass.VALIDATION_PASS
        if str(checks.get("workspace_integrity", "PASS")).upper() not in {"PASS", "VALIDATION_PASS"}:
            classification = ValidationClass.INCONCLUSIVE
        if backend.lower() in {"simulated", "scripted"} and classification == ValidationClass.VALIDATION_PASS:
            classification = ValidationClass.INCONCLUSIVE
        return ValidationResult(
            validation_id=f"validation-{uuid.uuid4().hex}",
            candidate_id=candidate.candidate_id,
            candidate_tree_hash=candidate.tree_hash,
            revision=revision,
            actual_tested_commit=actual_tested_commit,
            ci_run_id=ci_run_id,
            config_id=config_id,
            backend=backend,
            classification=classification,
            checks=dict(checks),
            evidence=dict(evidence or {}),
        )
