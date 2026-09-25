"""Machine-readable and Markdown reports with explicit status separation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .runtime.trace import sanitize


def _redact(value: Any) -> Any:
    return sanitize(value)


class ReportWriter:
    def write(self, root: str | Path, report: Mapping[str, Any]) -> tuple[Path, Path]:
        target = Path(root)
        target.mkdir(parents=True, exist_ok=True)
        safe = _redact(dict(report))
        json_path = target / "report.json"
        md_path = target / "report.md"
        self._atomic_write(json_path, json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True))
        self._atomic_write(md_path, self._markdown(safe))
        return md_path, json_path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _markdown(self, report: Mapping[str, Any]) -> str:
        lines = [f"# Harman Code Quality Agent Report", "", f"- Run: `{report.get('run_id', 'unknown')}`", f"- Stage: `{report.get('stage', 'unknown')}`", f"- Candidate: `{report.get('candidate_id', 'none')}`", ""]
        budget = report.get("budget_used", {})
        if isinstance(budget, Mapping):
            if bool(budget.get("token_usage_known", True)):
                lines.append(f"- Model tokens used: `{budget.get('tokens', 0)}`")
            else:
                lines.append("- Model token usage: `UNKNOWN`; further model work is blocked to preserve the budget")
            lines.append("")
        sections = (
            ("Candidate patches (not verified fixes)", "candidate_patches"),
            ("Human approvals", "human_approvals"),
            ("Validation passed", "validation_passed"),
            ("Code failures", "validation_code_fail"),
            ("Validation infrastructure failures", "validation_infrastructure"),
            ("Inconclusive validation", "validation_inconclusive"),
            ("Validation pending / partial", "validation_pending"),
            ("Approved suppression candidates", "approved_suppressions"),
            ("Unresolved", "unresolved"),
            ("Not executed", "not_executed"),
            ("Checks not run", "checks_not_run"),
            ("Infrastructure / configuration", "infrastructure"),
        )
        for title, key in sections:
            lines.extend([f"## {title}", ""])
            value = report.get(key, [])
            if not value:
                lines.append("- None recorded")
            elif isinstance(value, list):
                lines.extend(f"- {json.dumps(item, ensure_ascii=False, sort_keys=True)}" for item in value)
            else:
                lines.append(f"- {value}")
            lines.append("")
        lines.extend(["## Verification boundary", "", "This report never treats a candidate patch as a verified fix without identity-matched required checks.", ""])
        return "\n".join(lines)
