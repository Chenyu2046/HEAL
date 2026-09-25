"""Finding normalization, affinity batching, and conflict-aware scheduling."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Any, Mapping

from .domain import (
    Budget,
    Finding,
    Issue,
    RepairTask,
    RiskClass,
    SourceType,
    UTFailure,
    classify_risk,
    issue_file,
    issue_id,
    normalize_repo_path,
    sha256_text,
)


class InputFormatError(ValueError):
    """The input is not one of the intentionally supported schemas."""


@dataclass(frozen=True)
class NormalizedInput:
    task: RepairTask
    warnings: tuple[str, ...] = ()


class InputNormalizer:
    """Accept only the documented JSON shape; unknown logs fail explicitly."""

    def normalize(self, payload: Mapping[str, Any], *, default_repo: str | None = None, default_budget: Budget | None = None, default_mode: str = "local-demo", default_model_id: str = "unknown", cli_budget_overrides: Mapping[str, Any] | None = None) -> NormalizedInput:
        if not isinstance(payload, Mapping):
            raise InputFormatError("task input must be a JSON object")
        task_id = str(payload.get("task_id", payload.get("id", ""))).strip()
        run_id = str(payload.get("run_id", "")).strip()
        safe_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        if not safe_id.fullmatch(task_id) or (run_id and not safe_id.fullmatch(run_id)):
            raise InputFormatError("task_id and run_id must be 1-128 safe filename characters")
        repo = str(payload.get("repo", default_repo or "")).strip()
        base_commit = str(payload.get("base_commit", "")).strip()
        if not task_id or not repo or not base_commit:
            raise InputFormatError("task_id, repo, and base_commit are required")

        raw_issues = list(payload.get("issues") or ())
        raw_issues.extend({**item, "source_type": SourceType.FINDING.value} for item in (payload.get("findings") or ()))
        raw_issues.extend({**item, "source_type": SourceType.UT_FAILURE.value} for item in (payload.get("ut_failures") or ()))
        if not raw_issues:
            raise InputFormatError("at least one finding or UT failure is required")

        issues: list[Issue] = []
        warnings: list[str] = []
        seen: dict[tuple[str, str], str] = {}
        source_by_id: dict[str, str] = {}
        for raw in raw_issues:
            if not isinstance(raw, Mapping):
                raise InputFormatError("each issue must be an object")
            source = str(raw.get("source_type", SourceType.FINDING.value)).lower()
            try:
                issue: Issue
                if source == SourceType.FINDING.value:
                    issue = Finding.from_mapping({**raw, "base_commit": raw.get("base_commit", base_commit)})
                elif source == SourceType.UT_FAILURE.value:
                    issue = UTFailure.from_mapping({**raw, "base_commit": raw.get("base_commit", base_commit)})
                else:
                    raise InputFormatError(f"unsupported source_type: {source}")
            except (TypeError, ValueError) as exc:
                raise InputFormatError(f"invalid {source} issue: {exc}") from exc
            key = (source, issue_id(issue))
            prior_source = source_by_id.get(issue_id(issue))
            if prior_source is not None and prior_source != source:
                raise InputFormatError(f"issue id is ambiguous across sources: {issue_id(issue)}")
            serialized = repr(issue)
            if key in seen:
                if seen[key] != serialized:
                    raise InputFormatError(f"conflicting duplicate identity: {source}:{issue_id(issue)}")
                warnings.append(f"duplicate removed: {source}:{issue_id(issue)}")
                continue
            seen[key] = serialized
            source_by_id[issue_id(issue)] = source
            if isinstance(issue, Finding):
                issue = replace(issue, risk=classify_risk(issue))
            elif isinstance(issue, UTFailure):
                issue = replace(issue, risk=classify_risk(issue))
            issues.append(issue)

        raw_budget = payload.get("budget") or {}
        if not isinstance(raw_budget, Mapping):
            raise InputFormatError("budget must be an object")
        resolved_budget = {
            "max_model_calls": (default_budget or Budget()).max_model_calls,
            "max_tool_calls": (default_budget or Budget()).max_tool_calls,
            "max_tokens": (default_budget or Budget()).max_tokens,
            "max_wall_seconds": (default_budget or Budget()).max_wall_seconds,
            "max_edit_attempts": (default_budget or Budget()).max_edit_attempts,
            "max_chunk_actions": (default_budget or Budget()).max_chunk_actions,
        }
        resolved_budget.update(raw_budget)
        resolved_budget.update({key: value for key, value in (cli_budget_overrides or {}).items() if value is not None})
        task = RepairTask(
            task_id=task_id,
            run_id=run_id or sha256_text(f"{task_id}:{base_commit}")[:16],
            repo=repo,
            base_commit=base_commit,
            issues=tuple(issues),
            risk_policy=str(payload.get("risk_policy", "default")),
            budget=Budget(
                max_model_calls=int(resolved_budget["max_model_calls"]),
                max_tool_calls=int(resolved_budget["max_tool_calls"]),
                max_tokens=int(resolved_budget["max_tokens"]),
                max_wall_seconds=float(resolved_budget["max_wall_seconds"]),
                max_edit_attempts=int(resolved_budget["max_edit_attempts"]),
                max_chunk_actions=int(resolved_budget["max_chunk_actions"]),
            ),
            mode=str(payload.get("mode", default_mode)),
            model_id=str(payload.get("model_id", default_model_id)),
        )
        return NormalizedInput(task=task, warnings=tuple(warnings))


@dataclass(frozen=True)
class WorkingBatch:
    batch_id: str
    task_id: str
    issues: tuple[Issue, ...]
    affinity_key: tuple[str, str, str]
    known_files: frozenset[str]
    known_symbols: frozenset[str]
    lifecycle_domains: frozenset[str]
    risk: RiskClass


class BatchPlanner:
    """Greedy affinity grouping with a complexity ceiling, not a root-cause claim."""

    def __init__(self, max_issues: int = 20, max_files: int = 8) -> None:
        self.max_issues = max_issues
        self.max_files = max_files

    def plan(self, task: RepairTask) -> tuple[WorkingBatch, ...]:
        groups: dict[tuple[str, str, str], list[Issue]] = {}
        for issue in task.issues:
            module = getattr(issue, "module", None) or self._module_from_file(issue_file(issue))
            symbol = getattr(issue, "symbol", None) or "<unknown-symbol>"
            lifecycle = getattr(issue, "lifecycle_domain", None) or "<unknown-lifecycle>"
            key = (module, symbol, lifecycle)
            groups.setdefault(key, []).append(issue)

        batches: list[WorkingBatch] = []
        for affinity, group in groups.items():
            chunks: list[list[Issue]] = []
            chunk: list[Issue] = []
            files: set[str] = set()
            for issue in group:
                issue_path = issue_file(issue)
                next_files = files | ({issue_path} if issue_path else set())
                if chunk and (len(chunk) >= self.max_issues or len(next_files) > self.max_files):
                    chunks.append(chunk)
                    chunk, files = [], set()
                chunk.append(issue)
                if issue_path:
                    files.add(issue_path)
            if chunk:
                chunks.append(chunk)
            for issues in chunks:
                risks = {classify_risk(issue) for issue in issues}
                risk = RiskClass.HIGH if RiskClass.HIGH in risks else RiskClass.MEDIUM if RiskClass.MEDIUM in risks else RiskClass.LOW
                index = len(batches) + 1
                batches.append(WorkingBatch(
                    batch_id=f"{task.task_id}-batch-{index}", task_id=task.task_id,
                    issues=tuple(issues), affinity_key=affinity,
                    known_files=frozenset(issue_file(issue) for issue in issues if issue_file(issue)),
                    known_symbols=frozenset(getattr(issue, "symbol", None) for issue in issues if getattr(issue, "symbol", None)),
                    lifecycle_domains=frozenset(getattr(issue, "lifecycle_domain", None) for issue in issues if getattr(issue, "lifecycle_domain", None)),
                    risk=risk,
                ))
        return tuple(batches)

    @staticmethod
    def _module_from_file(file: str | None) -> str:
        if not file:
            return "<unknown-module>"
        parts = Path(file).parts
        return parts[0] if parts else "<unknown-module>"


def known_conflict(left: WorkingBatch, right: WorkingBatch) -> bool:
    """Return only evidence of conflict; false is not proof of semantic independence."""

    if left.known_files & right.known_files:
        return True
    if left.known_symbols & right.known_symbols:
        return True
    if left.lifecycle_domains & right.lifecycle_domains and left.lifecycle_domains:
        return True
    if "<unknown-symbol>" not in left.affinity_key and "<unknown-symbol>" not in right.affinity_key:
        return left.affinity_key[0] == right.affinity_key[0] and left.affinity_key[1] == right.affinity_key[1]
    return False


class ConflictAwareScheduler:
    """Builds bounded groups and pauses when known ranges overlap."""

    def schedule(self, batches: tuple[WorkingBatch, ...], max_workers: int) -> tuple[tuple[WorkingBatch, ...], ...]:
        slots: list[list[WorkingBatch]] = []
        for batch in batches:
            placed = False
            for slot in slots:
                if len(slot) >= max_workers:
                    continue
                if not any(known_conflict(batch, existing) for existing in slot):
                    slot.append(batch)
                    placed = True
                    break
            if not placed:
                slots.append([batch])
        return tuple(tuple(slot) for slot in slots)
