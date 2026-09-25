"""Immutable candidate snapshots tied to a real workspace tree hash."""

from __future__ import annotations

import uuid
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from ..config import ToolLimits
from ..domain import BatchProposal, Candidate, canonical_json, sha256_bytes, sha256_text, to_primitive, utc_now
from ..runtime.store import RunStore, StoreError
from ..runtime.workspace import WorkspaceError, git_tree_oid, tree_hash
from .scope import PatchScopeGuard


class CandidateError(RuntimeError):
    """Candidate identity or freeze precondition failed."""


@dataclass(frozen=True)
class FreezeResult:
    candidate: Candidate
    artifact_ids: tuple[str, ...]


class CandidateFreezer:
    def __init__(self, store: RunStore, limits: ToolLimits | None = None) -> None:
        self.store = store
        self.scope_guard = PatchScopeGuard(limits or ToolLimits())

    def freeze(self, *, run_id: str, workspace: Path, base_commit: str, proposals: Iterable[BatchProposal], finding_ids: Iterable[str]) -> FreezeResult:
        proposal_list = tuple(proposals)
        if not proposal_list:
            raise CandidateError("cannot freeze without integrated batch proposals")
        incomplete = [proposal.batch_id for proposal in proposal_list if not proposal.complete or proposal.unresolved or proposal.not_executed]
        if incomplete:
            details = [f"{proposal.batch_id}(complete={proposal.complete}, unresolved={list(proposal.unresolved)}, not_executed={list(proposal.not_executed)})" for proposal in proposal_list if proposal.batch_id in incomplete]
            raise CandidateError("incomplete proposals cannot be frozen: " + "; ".join(details))
        try:
            actual_tree_hash = tree_hash(workspace)
            tree_oid = git_tree_oid(workspace, stage_changes=True)
        except (WorkspaceError, OSError) as exc:
            raise CandidateError(f"cannot establish frozen candidate tree: {exc}") from exc
        candidate_id = f"cand-{uuid.uuid4().hex}"
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "HEAL Candidate",
            "GIT_AUTHOR_EMAIL": "heal-candidate@localhost",
            "GIT_COMMITTER_NAME": "HEAL Candidate",
            "GIT_COMMITTER_EMAIL": "heal-candidate@localhost",
        })
        try:
            commit_result = subprocess.run(
                ["git", "commit-tree", tree_oid, "-p", base_commit],
                cwd=workspace, input=f"HEAL candidate {candidate_id}\n", text=True,
                capture_output=True, check=False, timeout=30, env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CandidateError(f"cannot create immutable candidate commit: {exc}") from exc
        if commit_result.returncode != 0:
            raise CandidateError(f"cannot create immutable candidate commit: {commit_result.stderr.strip()}")
        candidate_commit = commit_result.stdout.strip()
        resolved_tree = subprocess.run(
            ["git", "rev-parse", f"{candidate_commit}^{{tree}}"],
            cwd=workspace, text=True, capture_output=True, check=False, timeout=30,
        )
        if resolved_tree.returncode != 0 or resolved_tree.stdout.strip() != tree_oid:
            raise CandidateError("candidate commit does not resolve to the frozen Git tree")
        anchored = subprocess.run(
            ["git", "update-ref", f"refs/heal/candidates/{candidate_id}", candidate_commit],
            cwd=workspace, text=True, capture_output=True, check=False, timeout=30,
        )
        if anchored.returncode != 0:
            raise CandidateError(f"cannot anchor candidate commit: {anchored.stderr.strip()}")
        diff_result = subprocess.run(["git", "diff", "--cached", "--binary", "--no-ext-diff", "--no-color", base_commit, "--"], cwd=workspace, text=True, capture_output=True, check=False, timeout=60)
        files_result = subprocess.run(["git", "diff", "--cached", "--name-only", "-z", base_commit, "--"], cwd=workspace, capture_output=True, check=False, timeout=30)
        if diff_result.returncode != 0 or files_result.returncode != 0:
            raise CandidateError(f"cannot read integrated candidate diff: {(diff_result.stderr or files_result.stderr.decode(errors='replace')).strip()}")
        diff = diff_result.stdout
        changed_files = tuple(sorted({item.decode("utf-8", errors="replace") for item in files_result.stdout.split(b"\0") if item}))
        scope = self.scope_guard.inspect(diff, changed_files)
        if scope.violations:
            raise CandidateError("integrated patch scope requires review: " + "; ".join(scope.violations))
        report = {
            "run_id": run_id,
            "base_commit": base_commit,
            "tree_hash": actual_tree_hash,
            "git_tree_oid": tree_oid,
            "candidate_commit": candidate_commit,
            "proposals": [to_primitive(proposal) for proposal in proposal_list],
            "patch_scope": to_primitive(scope),
            "finding_ids": sorted(set(finding_ids)),
            "created_at": utc_now(),
        }
        report_json = canonical_json(report)
        artifact_hash = sha256_bytes(diff.encode("utf-8"))
        report_hash = sha256_text(report_json)
        diff_artifact = self.store.save_artifact(run_id, f"{candidate_id}-diff", "candidate-diff", diff, {"tree_hash": actual_tree_hash})
        report_artifact = self.store.save_artifact(run_id, f"{candidate_id}-report", "candidate-report", report_json, {"tree_hash": actual_tree_hash})
        suppression_ids = tuple(sorted({finding_id for proposal in proposal_list for finding_id, action in proposal.action_map.items() if getattr(action, "value", str(action)) == "SUPPRESSION_CANDIDATE"}))
        candidate = Candidate(
            candidate_id=candidate_id, run_id=run_id, base_commit=base_commit,
            tree_hash=actual_tree_hash, artifact_hash=artifact_hash, report_hash=report_hash,
            changed_files=changed_files,
            finding_ids=tuple(sorted(set(finding_ids))), git_tree_oid=tree_oid,
            candidate_commit=candidate_commit, suppression_candidate_ids=suppression_ids,
        )
        artifact_ids = [diff_artifact["artifact_id"], report_artifact["artifact_id"]]
        try:
            self.store.save_frozen_candidate(candidate, artifact_ids)
        except StoreError as exc:
            raise CandidateError(f"cannot atomically commit frozen candidate and checkpoint: {exc}") from exc
        return FreezeResult(candidate, tuple(artifact_ids))

    def assert_current(self, candidate: Candidate, workspace: Path) -> None:
        current = tree_hash(workspace)
        if current != candidate.tree_hash:
            raise CandidateError("candidate tree changed; approval and validation are invalid")
        current_oid = git_tree_oid(workspace)
        if current_oid != candidate.git_tree_oid:
            raise CandidateError("candidate Git tree changed; approval and validation are invalid")

    @staticmethod
    def assert_commit_identity(candidate: Candidate, workspace: Path, commit: str | None = None) -> None:
        expected_commit = commit or candidate.candidate_commit
        if not expected_commit or expected_commit != candidate.candidate_commit or not candidate.git_tree_oid:
            raise CandidateError("candidate commit identity is missing or does not match")
        resolved = subprocess.run(
            ["git", "rev-parse", f"{expected_commit}^{{tree}}"], cwd=workspace,
            text=True, capture_output=True, check=False, timeout=30,
        )
        if resolved.returncode != 0 or resolved.stdout.strip() != candidate.git_tree_oid:
            raise CandidateError("candidate commit tree does not match frozen Git tree")
