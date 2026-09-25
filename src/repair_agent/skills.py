"""Versioned, deterministic Skill loading and routing."""

from __future__ import annotations

import re
import time
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


class SkillContextError(ValueError):
    """Skill discovery exceeded an explicit resource bound."""


class SkillStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def load(self, skill_id: str, *, max_bytes: int | None = None, deadline: float | None = None) -> SkillRecord:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("skill read stopped at the workspace deadline")
        relative = Path(skill_id)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"skill id escapes skill root: {skill_id}")
        candidates = [(self.root / f"{relative.as_posix()}.md").resolve(), (self.root / f"{relative.as_posix()}.json").resolve()]
        candidates = [candidate for candidate in candidates if candidate.is_relative_to(self.root)]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"skill not found: {skill_id}")
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise ValueError(f"skill exceeds read limit: {max_bytes} bytes")
        with path.open("rb") as stream:
            content_bytes = stream.read(max_bytes + 1 if max_bytes is not None else -1)
        if max_bytes is not None and len(content_bytes) > max_bytes:
            raise ValueError(f"skill exceeds read limit: {max_bytes} bytes")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("skill read stopped at the workspace deadline")
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

    def route_many(
        self,
        issues: tuple[Issue, ...] | list[Issue],
        *,
        max_skill_bytes: int = 256_000,
        max_scan_bytes: int = 512_000,
        max_context_chars: int = 12_000,
        deadline: float | None = None,
    ) -> tuple[SkillRecord, ...]:
        if not self.store.root.is_dir():
            return ()
        signals = tuple((
            str(getattr(issue, "rule_id", getattr(issue, "test_id", ""))).lower(),
            str(getattr(issue, "module", "") or "").lower(),
            getattr(issue, "risk", RiskClass.UNKNOWN),
        ) for issue in issues)
        results: dict[tuple[str, str, str], SkillRecord] = {}
        scanned_bytes = 0
        context_chars = 0
        try:
            paths = self.store.root.rglob("*.md")
            for path in paths:
                if deadline is not None and time.monotonic() >= deadline:
                    raise SkillContextError("skill context deadline exceeded")
                size = path.stat().st_size
                if size > max_skill_bytes:
                    raise SkillContextError("skill file exceeds configured byte limit")
                if scanned_bytes + size > max_scan_bytes:
                    raise SkillContextError("skill scan exceeds configured byte limit")
                skill_id = path.relative_to(self.store.root).with_suffix("").as_posix()
                skill = self.store.load(skill_id, max_bytes=max_skill_bytes, deadline=deadline)
                scanned_bytes += len(skill.content.encode("utf-8"))
                if scanned_bytes > max_scan_bytes:
                    raise SkillContextError("skill scan exceeds configured byte limit")
                if deadline is not None and time.monotonic() >= deadline:
                    raise SkillContextError("skill context deadline exceeded")
                rule_ids = {item.lower() for item in skill.rule_ids}
                modules = {item.lower() for item in skill.modules}
                if not any(
                    (rule and (rule in rule_ids or rule in skill_id.lower()))
                    or (module and (module in modules or module in skill_id.lower()))
                    or risk in skill.risks
                    or skill_id == "default"
                    for rule, module, risk in signals
                ):
                    continue
                key = (skill.skill_id, skill.version, skill.content_hash)
                if key in results:
                    continue
                rendered_chars = len(f"[Skill {skill.skill_id} v{skill.version} from {skill.source}]\n{skill.content}")
                context_chars += rendered_chars
                if context_chars > max_context_chars:
                    raise SkillContextError("skill context exceeds configured character limit")
                results[key] = skill
        except SkillContextError:
            raise
        except (OSError, UnicodeError, ValueError, TimeoutError) as exc:
            raise SkillContextError("skill context could not be loaded completely") from exc
        return tuple(results[key] for key in sorted(results))

    def route(self, issue: Issue) -> tuple[SkillRecord, ...]:
        return self.route_many((issue,))

    def inject(self, issue: Issue) -> str:
        skills = self.route(issue)
        if not skills:
            return "No matching versioned skill is configured. Current source evidence remains authoritative."
        return "\n\n".join(f"[Skill {skill.skill_id} v{skill.version} from {skill.source}]\n{skill.content}" for skill in skills)
