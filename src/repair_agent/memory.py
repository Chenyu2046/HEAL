"""Task, batch, and small provenance-bound episodic memory layers."""

from __future__ import annotations

import json
import os
import tempfile
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .domain import ActionKind, Observation, RiskClass, canonical_json, sha256_text, utc_now


@dataclass
class TaskStateMemory:
    task_id: str
    worker_id: str
    current_hypothesis: str = ""
    observations: list[Observation] = field(default_factory=list)
    action_map: dict[str, ActionKind] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def add(self, observation: Observation) -> None:
        self.observations.append(observation)
        if observation.error:
            self.evidence.append(observation.error)


@dataclass(frozen=True)
class BatchCacheEntry:
    key: str
    workspace_revision: int
    file_hashes: Mapping[str, str]
    value: Any
    created_at: str = field(default_factory=utc_now)


class BatchCache:
    def __init__(self) -> None:
        self._entries: dict[str, BatchCacheEntry] = {}

    def put(self, key: str, workspace_revision: int, file_hashes: Mapping[str, str], value: Any) -> None:
        self._entries[key] = BatchCacheEntry(key, workspace_revision, dict(file_hashes), value)

    def get(self, key: str, workspace_revision: int, file_hashes: Mapping[str, str]) -> Any | None:
        entry = self._entries.get(key)
        if entry is None or entry.workspace_revision != workspace_revision or dict(entry.file_hashes) != dict(file_hashes):
            return None
        return entry.value

    def invalidate(self, paths: set[str] | None = None) -> None:
        if paths is None:
            self._entries.clear()
            return
        self._entries = {
            key: value for key, value in self._entries.items() if not paths.intersection(value.file_hashes)
        }


@dataclass(frozen=True)
class Episode:
    episode_id: str
    repo: str
    module: str
    rule: str
    source_commit: str
    memory_type: str
    keywords: tuple[str, ...]
    summary: str
    provenance: tuple[str, ...]
    human_review: str
    ci_result: str
    created_at: str = field(default_factory=utc_now)


class EpisodeStore:
    """JSON episode files keep the initial store inspectable and dependency-free."""

    def __init__(self, root: str | Path, worker_id: str | None = None, task_id: str | None = None) -> None:
        base = Path(root).resolve()
        self.root = base / "tasks" / (task_id or "_unbound") / (worker_id or "default")
        self.experience_root = base / "experience"
        self.root.mkdir(parents=True, exist_ok=True)
        self.experience_root.mkdir(parents=True, exist_ok=True)

    def store_candidate(self, episode: Episode) -> Path:
        if episode.human_review not in {"APPROVED", "REJECTED", "PENDING"}:
            raise ValueError("human_review must be explicit")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", episode.episode_id):
            raise ValueError("episode_id must be a simple file name")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", episode.episode_id):
            raise ValueError("episode_id must be a simple file name")
        payload = canonical_json(episode)
        path = self.root / f"{episode.episode_id}.json"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def store_experience(self, episode: Episode) -> Path:
        if episode.human_review != "APPROVED" or episode.ci_result not in {"VALIDATION_PASS", "PASS"}:
            raise ValueError("shared experience requires human approval and a passing validation result")
        path = self.experience_root / f"{episode.episode_id}.json"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", episode.episode_id):
            raise ValueError("episode_id must be a simple file name")
        payload = canonical_json(episode)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.experience_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def retrieve(
        self,
        *,
        repo: str,
        module: str | None,
        rule: str | None,
        keywords: tuple[str, ...],
        source_commit: str,
        limit: int = 5,
        deadline: float | None = None,
    ) -> tuple[Episode, ...]:
        scored: list[tuple[int, Episode]] = []
        wanted = {word.lower() for word in keywords if word}
        paths = sorted(set(self.root.glob("*.json")) | set(self.experience_root.glob("*.json")))
        for path in paths:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("memory retrieval stopped at the workspace deadline")
            try:
                if path.stat().st_size > 256_000:
                    continue
                episode = Episode(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("memory retrieval stopped at the workspace deadline")
            if episode.repo != repo or (module and episode.module not in {module, "*"}) or (rule and episode.rule not in {rule, "*"}):
                continue
            if source_commit and episode.source_commit != source_commit:
                continue
            overlap = wanted.intersection(word.lower() for word in episode.keywords)
            score = len(overlap) * 3
            if episode.source_commit == source_commit:
                score += 2
            if episode.human_review == "APPROVED":
                score += 1
            scored.append((score, episode))
        scored.sort(key=lambda item: (-item[0], item[1].created_at))
        return tuple(item[1] for item in scored[:limit])


def episode_candidate_id(repo: str, rule: str, summary: str) -> str:
    return sha256_text(f"{repo}:{rule}:{summary}")[:24]
