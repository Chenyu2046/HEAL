"""Experience Memory consolidation (方案 §12): persist verified repair episodes.

Only fully-passing outcomes (human APPROVED + VALIDATION_PASS) become reusable
experience; the failure-path Refinement Trace is explicitly out of scope for v1
(方案 §12 天花板). Every derived field comes honestly from existing records
(candidate / approval / validation / candidate-report artifact); when a source
is missing the field stays empty — most notably root_cause, which has no honest
origin anywhere in the current records and is never guessed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .domain import ValidationClass, sha256_bytes, sha256_text
from .memory import Episode, EpisodeStore
from .runtime.store import RunStore


class ExperienceWriter:
    """Assemble RepairEpisodes from persisted run records and write them to the shared experience storage.

    Called by the orchestrator after a run reaches VERIFIED (receive_ci callback
    or resume recovery). Preconditions that are simply not met (no approval, no
    passing validation) return an empty tuple — that is not an error; only real
    assembly/write failures raise, and the orchestrator converts those to traces.
    """

    def __init__(self, store: EpisodeStore) -> None:
        self.store = store

    def record_verified_run(self, run_store: RunStore, *, run_id: str, candidate_id: str) -> tuple[str, ...]:
        """Write one Episode per finding of the verified candidate; returns the episode ids."""
        run = run_store.get_run(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        candidate = run_store.get_candidate(candidate_id)
        if candidate is None or candidate.run_id != run_id:
            raise ValueError(f"candidate does not belong to run: {candidate_id}")
        approval = run_store.get_approval(candidate_id)
        if approval is None or not approval.approved or approval.tree_hash != candidate.tree_hash:
            return ()
        passing = [
            item
            for item in run_store.list_validations(candidate_id)
            if item.classification == ValidationClass.VALIDATION_PASS and item.candidate_commit == candidate.candidate_commit
        ]
        if not passing:
            return ()
        validation = max(passing, key=lambda item: item.created_at)
        report = self._load_report(run_store, candidate_id)
        proposals = [item for item in report.get("proposals", ()) if isinstance(item, Mapping)]
        scope = report.get("patch_scope")
        scope = scope if isinstance(scope, Mapping) else {}
        task_payload = run.get("task") if isinstance(run.get("task"), Mapping) else {}
        repo = str(task_payload.get("repo", ""))
        issues = [item for item in (task_payload.get("issues") or ()) if isinstance(item, Mapping)]
        episode_ids: list[str] = []
        for finding in issues:
            finding_id = str(finding.get("finding_id", finding.get("id", "")))
            if not finding_id or finding_id not in candidate.finding_ids:
                continue
            if str(finding.get("source_type", "finding")) != "finding":
                # UT failures carry no rule/symbol structure; v1 consolidates findings only.
                continue
            episode = self._episode_for_finding(
                run_id=run_id,
                candidate=candidate,
                validation=validation,
                repo=repo,
                finding=finding,
                finding_id=finding_id,
                proposals=proposals,
                scope=scope,
            )
            self.store.store_experience(episode)
            episode_ids.append(episode.episode_id)
        return tuple(episode_ids)

    def _episode_for_finding(
        self,
        *,
        run_id: str,
        candidate: Any,
        validation: Any,
        repo: str,
        finding: Mapping[str, Any],
        finding_id: str,
        proposals: list[Mapping[str, Any]],
        scope: Mapping[str, Any],
    ) -> Episode:
        rule = str(finding.get("rule_id", "UNKNOWN_RULE"))
        file_value = str(finding.get("file", ""))
        symbol = str(finding.get("symbol") or "")
        module = str(finding.get("module") or "") or (file_value.split("/")[0] if file_value else "<unknown-module>")
        action = ""
        changed_files = tuple(str(item) for item in candidate.changed_files)
        for proposal in proposals:
            action_map = proposal.get("action_map")
            action_map = action_map if isinstance(action_map, Mapping) else {}
            if finding_id in {str(key) for key in action_map}:
                action = str(action_map[finding_id])
                if proposal.get("changed_files"):
                    changed_files = tuple(str(item) for item in proposal["changed_files"])
                break
        checks = {str(key): str(value) for key, value in dict(validation.checks).items()}
        feedback = "; ".join(f"{key}={checks[key]}" for key in sorted(checks))
        if scope:
            scope_summary = (
                f"patch scope: {scope.get('changed_files', '?')} file(s), {scope.get('diff_lines', '?')} changed lines"
                f" ({scope.get('added_lines', '?')} added, {scope.get('deleted_lines', '?')} deleted)"
            )
        elif changed_files:
            scope_summary = f"changed files: {len(changed_files)}"
        else:
            scope_summary = ""
        evidence_parts = [part for part in (scope_summary, f"checks: {feedback}" if feedback else "") if part]
        keywords = tuple(dict.fromkeys(item for item in (rule, symbol, module, file_value, action) if item))
        # Deterministic per (candidate, finding): the resume path rewrites the same
        # episode id instead of duplicating experience entries.
        episode_id = f"exp-{sha256_text(f'{candidate.candidate_id}:{finding_id}')[:24]}"
        line = finding.get("line")
        return Episode(
            episode_id=episode_id,
            repo=repo,
            module=module,
            rule=rule,
            source_commit=candidate.base_commit,
            memory_type="fix",
            keywords=keywords,
            summary=f"{rule} at {file_value}:{line if line is not None else '?'} handled as {action or 'UNRECORDED'} (candidate {candidate.candidate_id})",
            provenance=(
                f"run:{run_id}",
                f"candidate:{candidate.candidate_id}",
                f"finding:{finding_id}",
                f"validation:{validation.validation_id}",
                f"ci_run:{validation.ci_run_id}",
            ),
            human_review="APPROVED",
            ci_result="VALIDATION_PASS",
            trigger_rule=rule,
            trigger_module=module,
            trigger_symbol=symbol,
            warning_signature=str(finding.get("finding_fingerprint") or ""),
            # Diagnosis.RootCause: no honest source field exists in the persisted
            # records; it stays empty by design rather than being invented.
            root_cause="",
            evidence_summary="; ".join(evidence_parts),
            fix_summary=action,
            changed_files=changed_files,
            validation_feedback=feedback,
            build_result=checks.get("build", ""),
            ut_result=checks.get("ut", ""),
            scan_result=checks.get("scan", ""),
            validated=True,
            schema_version="2",
        )

    def _load_report(self, run_store: RunStore, candidate_id: str) -> Mapping[str, Any]:
        """Read the candidate-report artifact; any integrity/read problem yields an empty report."""
        record = run_store.get_artifact(f"{candidate_id}-report")
        if record is None:
            return {}
        try:
            payload = Path(str(record["path"])).read_bytes()
        except OSError:
            return {}
        if sha256_bytes(payload) != str(record["sha256"]):
            # Artifact integrity mismatch: use empty derived fields rather than tampered content.
            return {}
        try:
            report = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return report if isinstance(report, dict) else {}
