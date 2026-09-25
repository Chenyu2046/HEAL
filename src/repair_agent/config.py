"""Validated configuration with explicit external-mode boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .domain import Budget


class ConfigError(ValueError):
    """Invalid or incomplete configuration."""


@dataclass(frozen=True)
class ToolLimits:
    max_file_bytes: int = 256_000
    max_output_chars: int = 80_000
    max_search_results: int = 200
    max_diff_lines: int = 20_000
    max_changed_files: int = 100
    command_timeout_seconds: float = 300.0


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "scripted"
    model_id: str = "scripted"
    endpoint: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 60.0
    max_retries: int = 1


@dataclass(frozen=True)
class Config:
    version: str = "1"
    mode: str = "local-demo"
    source_repo: str | None = None
    workspace_parent: str = ".repair-agent/workspaces"
    artifact_root: str = ".repair-agent/runs"
    skill_root: str = "skills"
    memory_root: str = ".repair-agent/memory"
    max_workers: int = 2
    chunking_enabled: bool = False
    budget: Budget = field(default_factory=Budget)
    tools: ToolLimits = field(default_factory=ToolLimits)
    model: ModelConfig = field(default_factory=ModelConfig)
    protected_paths: tuple[str, ...] = (".git", ".repair-agent", "third_party", "vendor", "dependencies", "ci", ".github")
    max_recent_observations: int = 10
    max_observation_chars: int = 4_000
    max_skill_context_chars: int = 12_000
    max_skill_scan_bytes: int = 512_000

    def __post_init__(self) -> None:
        if self.mode not in {"local-demo", "simulated-enterprise", "enterprise"}:
            raise ConfigError(f"unsupported mode: {self.mode}")
        if self.max_workers < 1:
            raise ConfigError("max_workers must be positive")
        if (self.model.max_retries < 0 or self.max_recent_observations < 1 or self.max_observation_chars < 1
                or self.max_skill_context_chars < 1 or self.max_skill_scan_bytes < 1):
            raise ConfigError("retry and context limits are invalid")


def _budget(value: Mapping[str, Any] | None) -> Budget:
    value = value or {}
    return Budget(
        max_model_calls=int(value.get("max_model_calls", 20)),
        max_tool_calls=int(value.get("max_tool_calls", 100)),
        max_tokens=int(value.get("max_tokens", 100_000)),
        max_wall_seconds=float(value.get("max_wall_seconds", 900.0)),
        max_edit_attempts=int(value.get("max_edit_attempts", 20)),
        max_chunk_actions=int(value.get("max_chunk_actions", 8)),
    )


def _load_mapping(path: Path) -> Mapping[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"configuration must be JSON: {path}: {exc}") from exc


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        return Config()
    config_path = Path(path)
    raw = _load_mapping(config_path)
    tools = raw.get("tools", {})
    model = raw.get("model", {})
    default_tools = ToolLimits()
    default_model = ModelConfig()
    tool_values = {key: type(getattr(default_tools, key))(value) for key, value in tools.items() if hasattr(default_tools, key)}
    model_values: dict[str, Any] = {}
    for key, value in model.items():
        if not hasattr(default_model, key):
            continue
        if value is None:
            model_values[key] = None
        else:
            model_values[key] = type(getattr(default_model, key))(value)
    return Config(
        version=str(raw.get("version", "1")),
        mode=str(raw.get("mode", "local-demo")),
        source_repo=str(raw["source_repo"]) if raw.get("source_repo") else None,
        workspace_parent=str(raw.get("workspace_parent", ".repair-agent/workspaces")),
        artifact_root=str(raw.get("artifact_root", ".repair-agent/runs")),
        skill_root=str(raw.get("skill_root", "skills")),
        memory_root=str(raw.get("memory_root", ".repair-agent/memory")),
        max_workers=int(raw.get("max_workers", 2)),
        chunking_enabled=bool(raw.get("chunking_enabled", False)),
        budget=_budget(raw.get("budget")),
        tools=ToolLimits(**tool_values),
        model=ModelConfig(**model_values),
        protected_paths=tuple(str(item) for item in raw.get("protected_paths", Config().protected_paths)),
        max_recent_observations=int(raw.get("max_recent_observations", 10)),
        max_observation_chars=int(raw.get("max_observation_chars", 4_000)),
        max_skill_context_chars=int(raw.get("max_skill_context_chars", 12_000)),
        max_skill_scan_bytes=int(raw.get("max_skill_scan_bytes", 512_000)),
    )
