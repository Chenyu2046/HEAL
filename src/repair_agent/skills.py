"""Versioned, deterministic Skill loading and routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .domain import Issue, RiskClass, sha256_bytes


@dataclass(frozen=True)
class SkillRecord:
    skill_id: str
    version: str
    source: str
    content_hash: str
    content: str
    rule_ids: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    risks: tuple[RiskClass, ...] = ()


class SkillStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def load(self, skill_id: str) -> SkillRecord:
        relative = Path(skill_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"skill id escapes skill root: {skill_id}")
        candidates = [(self.root / f"{relative.as_posix()}.md").resolve(), (self.root / f"{relative.as_posix()}.json").resolve()]
        candidates = [candidate for candidate in candidates if candidate.is_relative_to(self.root)]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"skill not found: {skill_id}")
        content_bytes = path.read_bytes()
        content = content_bytes.decode("utf-8")
        metadata = self._metadata(content)
        return SkillRecord(
            skill_id=skill_id,
            version=metadata.get("version", "unversioned"),
            source=str(path),
            content_hash=sha256_bytes(content_bytes),
            content=content,
            rule_ids=tuple(item for item in metadata.get("rules", "").split(",") if item),
            modules=tuple(item for item in metadata.get("modules", "").split(",") if item),
            risks=tuple(RiskClass(item) for item in metadata.get("risks", "").split(",") if item in {risk.value for risk in RiskClass}),
        )

    def _metadata(self, content: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for line in content.splitlines()[:20]:
            match = re.match(r"^<!--\s*([a-zA-Z_]+)\s*:\s*(.*?)\s*-->$", line)
            if match:
                metadata[match.group(1).lower()] = match.group(2)
        return metadata


class SkillRouter:
    def __init__(self, store: SkillStore) -> None:
        self.store = store

    def route(self, issue: Issue) -> tuple[SkillRecord, ...]:
        if not self.store.root.is_dir():
            return ()
        rule = str(getattr(issue, "rule_id", getattr(issue, "test_id", ""))).lower()
        module = str(getattr(issue, "module", "") or "").lower()
        risk = getattr(issue, "risk", RiskClass.UNKNOWN)
        results: list[SkillRecord] = []
        for path in sorted(self.store.root.rglob("*.md")):
            skill_id = path.relative_to(self.store.root).with_suffix("").as_posix()
            try:
                skill = self.store.load(skill_id)
            except (OSError, UnicodeError, ValueError):
                continue
            metadata_hit = rule in {item.lower() for item in skill.rule_ids} or module in {item.lower() for item in skill.modules} or risk in skill.risks
            filename_hit = rule and rule in skill_id.lower() or module and module in skill_id.lower()
            if metadata_hit or filename_hit or skill_id == "default":
                results.append(skill)
        return tuple(results)

    def inject(self, issue: Issue) -> str:
        skills = self.route(issue)
        if not skills:
            return "No matching versioned skill is configured. Current source evidence remains authoritative."
        return "\n\n".join(f"[Skill {skill.skill_id} v{skill.version} from {skill.source}]\n{skill.content}" for skill in skills)
