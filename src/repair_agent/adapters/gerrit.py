"""Gerrit boundary. Private deployment details fail explicitly when absent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain import Candidate, SubmissionIntent, SubmissionStatus


@dataclass(frozen=True)
class SubmissionResponse:
    status: SubmissionStatus
    message: str
    remote_change: str | None = None
    patch_set: str | None = None
    remote_revision: str | None = None
    response_known: bool = True
    missing_contract: tuple[str, ...] = ()
    safe_to_retry: bool = False


class GerritAdapter(Protocol):
    def submit(self, intent: SubmissionIntent, candidate: Candidate) -> SubmissionResponse: ...

    def query(self, intent: SubmissionIntent) -> SubmissionResponse: ...


class NotConfiguredGerritAdapter:
    def submit(self, intent: SubmissionIntent, candidate: Candidate) -> SubmissionResponse:
        return SubmissionResponse(
            SubmissionStatus.NOT_CONFIGURED,
            "enterprise Gerrit endpoint is not configured; no remote side effect was attempted",
            missing_contract=("endpoint", "authentication", "repository mapping", "Change-Id policy", "response schema"),
        )

    def query(self, intent: SubmissionIntent) -> SubmissionResponse:
        return SubmissionResponse(
            SubmissionStatus.NOT_CONFIGURED,
            "enterprise Gerrit query is not configured",
            response_known=False,
            missing_contract=("endpoint", "authentication", "query contract"),
        )
