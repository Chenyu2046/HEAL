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
    dispatch_id: str | None = None


class CIAdapter(Protocol):
    """CI dispatch contract.

    Implementations must deduplicate repeated calls by ``idempotency_key``.
    ``accepted=False`` means the request is definitively not enqueued; an
    exception or an accepted response without a run id has an unknown outcome.
    Callbacks must echo this id as ``dispatch_id``. Adapters whose private
    endpoint cannot provide these contracts must reject before dispatch.
    """

    def submit(self, candidate: Candidate, intent: SubmissionIntent, *, idempotency_key: str) -> CIRequest: ...


class NotConfiguredCIAdapter:
    def submit(self, candidate: Candidate, intent: SubmissionIntent, *, idempotency_key: str) -> CIRequest:
        return CIRequest(
            accepted=False,
            run_id=None,
            message="enterprise CI endpoint is not configured; no validation was started",
            backend="enterprise",
            missing_contract=("endpoint", "authentication", "pipeline mapping", "callback schema", "scan identity fields", "idempotency-key dispatch and deduplication", "callback dispatch identity"),
        )
