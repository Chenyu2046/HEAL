"""Enterprise CI boundary with identity-aware callback contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain import Candidate, SubmissionIntent


@dataclass(frozen=True)
class CIRequest:
    accepted: bool
    run_id: str | None
    message: str
    backend: str
    missing_contract: tuple[str, ...] = ()


class CIAdapter(Protocol):
    def submit(self, candidate: Candidate, intent: SubmissionIntent) -> CIRequest: ...


class NotConfiguredCIAdapter:
    def submit(self, candidate: Candidate, intent: SubmissionIntent) -> CIRequest:
        return CIRequest(
            accepted=False,
            run_id=None,
            message="enterprise CI endpoint is not configured; no validation was started",
            backend="enterprise",
            missing_contract=("endpoint", "authentication", "pipeline mapping", "callback schema", "scan identity fields"),
        )
