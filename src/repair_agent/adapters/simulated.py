"""Stateful simulation adapters for later deterministic recovery checks."""

from __future__ import annotations

import uuid

from ..domain import Candidate, SubmissionIntent, SubmissionStatus
from .ci import CIRequest
from .gerrit import SubmissionResponse


class SimulatedGerrit:
    def __init__(self, *, lose_response: bool = False) -> None:
        self.lose_response = lose_response
        self._submissions: dict[tuple[str, str], SubmissionResponse] = {}

    def submit(self, intent: SubmissionIntent, candidate: Candidate) -> SubmissionResponse:
        key = (intent.change_id, intent.candidate_commit)
        existing = self._submissions.get(key)
        if existing:
            return existing
        response = SubmissionResponse(
            SubmissionStatus.SUBMITTED,
            "simulated Gerrit accepted the fixed commit",
            remote_change=f"sim-change-{uuid.uuid4().hex[:12]}",
            patch_set="1",
            remote_revision=intent.candidate_commit,
            response_known=True,
        )
        self._submissions[key] = response
        if self.lose_response:
            return SubmissionResponse(SubmissionStatus.SUBMISSION_UNKNOWN, "simulated remote success with lost local response", response_known=False)
        return response

    def query(self, intent: SubmissionIntent) -> SubmissionResponse:
        return self._submissions.get((intent.change_id, intent.candidate_commit), SubmissionResponse(SubmissionStatus.SUBMISSION_UNKNOWN, "no matching simulated remote revision", response_known=True, safe_to_retry=True))


class SimulatedCI:
    def __init__(self) -> None:
        self._requests: dict[str, CIRequest] = {}
        self._requests_by_key: dict[str, CIRequest] = {}
        self._results: dict[str, dict[str, str]] = {}

    def submit(self, candidate: Candidate, intent: SubmissionIntent, *, idempotency_key: str) -> CIRequest:
        existing = self._requests_by_key.get(idempotency_key)
        if existing:
            return existing
        run_id = f"sim-ci-{uuid.uuid4().hex[:12]}"
        request = CIRequest(True, run_id, "simulated CI accepted the candidate", "simulated")
        self._requests[run_id] = request
        self._requests_by_key[idempotency_key] = request
        self._results.setdefault(run_id, {})
        return request

    def set_result(self, run_id: str, checks: dict[str, str]) -> None:
        if run_id not in self._requests:
            raise KeyError(run_id)
        self._results[run_id] = dict(checks)

    def result(self, run_id: str) -> dict[str, str]:
        if run_id not in self._requests:
            raise KeyError(run_id)
        return dict(self._results[run_id])
