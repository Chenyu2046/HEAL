"""Immutable candidate snapshots tied to a real workspace tree hash."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from ..domain import BatchProposal, Candidate, Stage, canonical_json, sha256_bytes, sha256_text, to_primitive, utc_now
from ..runtime.store import RunStore
from ..runtime.workspace import tree_hash


class CandidateError(RuntimeError):
    """Candidate identity or freeze precondition failed."""


@dataclass(frozen=True)
class FreezeResult:
    candidate: Candidate
    artifact_ids: tuple[str, ...]


class CandidateFreezer:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    def freeze(self, *, run_id: str, workspace: Path, base_commit: str, proposals: Iterable[BatchProposal], finding_ids: Iterable[str]) -> FreezeResult:
        proposal_list = tuple(proposals)
        if not proposal_list:
            raise CandidateError("cannot freeze without integrated batch proposals")
        incomplete = [proposal.batch_id for proposal in proposal_list if not proposal.complete or proposal.unresolved or proposal.not_executed]
        if incomplete:
            raise CandidateError(f"incomplete proposals cannot be frozen: {', '.join(incomplete)}")
        actual_tree_hash = tree_hash(workspace)
        diff = "\n".join(proposal.diff for proposal in proposal_list if proposal.diff)
        report = {
            "run_id": run_id,
            "base_commit": base_commit,
            "tree_hash": actual_tree_hash,
            "proposals": [to_primitive(proposal) for proposal in proposal_list],
            "finding_ids": sorted(set(finding_ids)),
            "created_at": utc_now(),
        }
        report_json = canonical_json(report)
        candidate_id = f"cand-{uuid.uuid4().hex}"
        artifact_hash = sha256_bytes(diff.encode("utf-8"))
        report_hash = sha256_text(report_json)
        diff_artifact = self.store.save_artifact(run_id, f"{candidate_id}-diff", "candidate-diff", diff, {"tree_hash": actual_tree_hash})
        report_artifact = self.store.save_artifact(run_id, f"{candidate_id}-report", "candidate-report", report_json, {"tree_hash": actual_tree_hash})
        suppression_ids = tuple(sorted({finding_id for proposal in proposal_list for finding_id, action in proposal.action_map.items() if getattr(action, "value", str(action)) == "SUPPRESSION_CANDIDATE"}))
        candidate = Candidate(candidate_id, run_id, base_commit, actual_tree_hash, artifact_hash, report_hash, tuple(sorted({path for proposal in proposal_list for path in proposal.changed_files})), tuple(sorted(set(finding_ids))), suppression_ids)
        self.store.save_candidate(candidate)
        self.store.save_checkpoint(run_id, f"checkpoint-{candidate_id}", Stage.CANDIDATE_FROZEN, [diff_artifact["artifact_id"], report_artifact["artifact_id"]], {"candidate_id": candidate_id, "tree_hash": actual_tree_hash})
        return FreezeResult(candidate, (diff_artifact["artifact_id"], report_artifact["artifact_id"]))

    def assert_current(self, candidate: Candidate, workspace: Path) -> None:
        current = tree_hash(workspace)
        if current != candidate.tree_hash:
            raise CandidateError("candidate tree changed; approval and validation are invalid")
