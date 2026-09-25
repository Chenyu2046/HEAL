"""Finding normalization, affinity batching, and conflict-aware scheduling."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
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

    def normalize(self, payload: Mapping[str, Any], *, default_repo: str | None = None) -> NormalizedInput:
        if not isinstance(payload, Mapping):
            raise InputFormatError("task input must be a JSON object")
        task_id = str(payload.get("task_id", payload.get("id", ""))).strip()
        run_id = str(payload.get("run_id", "")).strip()
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
            serialized = repr(issue)
            if key in seen:
                if seen[key] != serialized:
                    warnings.append(f"duplicate identity with conflicting payload: {source}:{issue_id(issue)}")
                else:
                    warnings.append(f"duplicate removed: {source}:{issue_id(issue)}")
                continue
            seen[key] = serialized
            if isinstance(issue, Finding):
                issue = replace(issue, risk=classify_risk(issue))
            elif isinstance(issue, UTFailure):
                issue = replace(issue, risk=classify_risk(issue))
            issues.append(issue)

        raw_budget = payload.get("budget") or {}
        if not isinstance(raw_budget, Mapping):
            raise InputFormatError("budget must be an object")
        task = RepairTask(
            task_id=task_id,
            run_id=run_id or sha256_text(f"{task_id}:{base_commit}")[:16],
            repo=repo,
            base_commit=base_commit,
            issues=tuple(issues),
            risk_policy=str(payload.get("risk_policy", "default")),
            budget=Budget(
                max_model_calls=int(raw_budget.get("max_model_calls", 20)),
                max_tool_calls=int(raw_budget.get("max_tool_calls", 100)),
                max_tokens=int(raw_budget.get("max_tokens", 100_000)),
                max_wall_seconds=float(raw_budget.get("max_wall_seconds", 900.0)),
                max_edit_attempts=int(raw_budget.get("max_edit_attempts", 20)),
            ),
            mode=str(payload.get("mode", "local-demo")),
            model_id=str(payload.get("model_id", "unknown")),
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
            group = groups.setdefault(key, [])
            files = {issue_file(item) for item in group if issue_file(item)}
            if group and (len(group) >= self.max_issues or (issue_file(issue) and len(files | {issue_file(issue)}) > self.max_files)):
                key = (module, f"{symbol}#{len(groups)}", lifecycle)
                group = groups.setdefault(key, [])
            group.append(issue)

        batches: list[WorkingBatch] = []
        for index, (affinity, issues) in enumerate(groups.items(), 1):
            risks = {classify_risk(issue) for issue in issues}
            risk = RiskClass.HIGH if RiskClass.HIGH in risks else RiskClass.MEDIUM if RiskClass.MEDIUM in risks else RiskClass.LOW
            batches.append(
                WorkingBatch(
                    batch_id=f"{task.task_id}-batch-{index}",
                    task_id=task.task_id,
                    issues=tuple(issues),
                    affinity_key=affinity,
                    known_files=frozenset(issue_file(issue) for issue in issues if issue_file(issue)),
                    known_symbols=frozenset(getattr(issue, "symbol", None) for issue in issues if getattr(issue, "symbol", None)),
                    lifecycle_domains=frozenset(getattr(issue, "lifecycle_domain", None) for issue in issues if getattr(issue, "lifecycle_domain", None)),
                    risk=risk,
                )
            )
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
