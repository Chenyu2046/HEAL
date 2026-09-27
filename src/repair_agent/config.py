"""Validated configuration with explicit external-mode boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .domain import Budget


class ConfigError(ValueError):
    """Invalid or incomplete configuration."""


_CHECK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class CheckSpec:
    """One configured trusted check; the model may only select it by name."""

    name: str                      # ^[a-z][a-z0-9_]{0,63}$ — the only string the model may select
    argv: tuple[str, ...]          # fixed command vector; every element a non-empty string
    timeout_seconds: float = 120.0  # per-check cap; clamped to the remaining wall deadline

    def __post_init__(self) -> None:
        if not _CHECK_NAME.fullmatch(self.name):
            raise ConfigError(f"check name must match {_CHECK_NAME.pattern}: {self.name!r}")
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ConfigError(f"check argv must be a non-empty vector of non-empty strings: {self.name}")
        if not self.timeout_seconds > 0:
            raise ConfigError(f"check timeout_seconds must be positive: {self.name}")


@dataclass(frozen=True)
class ToolLimits:
    max_file_bytes: int = 256_000
    max_output_chars: int = 80_000
    max_search_results: int = 200
    max_diff_lines: int = 20_000
    max_changed_files: int = 100
    command_timeout_seconds: float = 300.0
    check_timeout_seconds: float = 120.0  # default when a check spec omits timeout


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
    context_cache_enabled: bool = True
    budget: Budget = field(default_factory=Budget)
    tools: ToolLimits = field(default_factory=ToolLimits)
    model: ModelConfig = field(default_factory=ModelConfig)
    protected_paths: tuple[str, ...] = (".git", ".repair-agent", "third_party", "vendor", "dependencies", "ci", ".github")
    max_recent_observations: int = 10
    max_observation_chars: int = 4_000
    max_skill_context_chars: int = 12_000
    max_skill_scan_bytes: int = 512_000
    checks: tuple[CheckSpec, ...] = ()          # empty ⇒ run_checks tool is UNSUPPORTED
    check_command_prefix: tuple[str, ...] = ()  # optional trusted prefix (tech-design §1.6)
    evidence_ledger_enabled: bool = True        # R2 rollback switch (tech-design §2.1)
    dedup_observations_enabled: bool = False    # R3 switch; changes prompt semantics, default off (§2.6)

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
        max_context_files=int(value.get("max_context_files", 24)),
        max_symbol_expansions=int(value.get("max_symbol_expansions", 12)),
        max_search_rounds=int(value.get("max_search_rounds", 8)),
        max_check_runs=int(value.get("max_check_runs", 8)),
    )


def _checks(value: Any, *, default_timeout: float) -> tuple[CheckSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ConfigError("checks must be an object mapping check names to commands")
    specs: list[CheckSpec] = []
    for raw_name, entry in value.items():
        name = str(raw_name)
        if isinstance(entry, Mapping):
            argv = entry.get("argv")
            timeout = float(entry.get("timeout_seconds", default_timeout))
        elif isinstance(entry, (list, tuple)):
            argv, timeout = entry, default_timeout  # bare argv form ⇒ default timeout
        else:
            raise ConfigError(f"check {name!r} must be an object with argv or a bare command list")
        if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ConfigError(f"check {name!r} argv must be a non-empty list of non-empty strings")
        specs.append(CheckSpec(name, tuple(argv), timeout))
    return tuple(specs)


def _check_prefix(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError("check_command_prefix must be a list of non-empty strings")
    return tuple(value)


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
    tool_limits = ToolLimits(**tool_values)
    check_specs = _checks(raw.get("checks"), default_timeout=tool_limits.check_timeout_seconds)
    check_prefix = _check_prefix(raw.get("check_command_prefix"))
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
        context_cache_enabled=bool(raw.get("context_cache_enabled", True)),
        budget=_budget(raw.get("budget")),
        tools=tool_limits,
        model=ModelConfig(**model_values),
        protected_paths=tuple(str(item) for item in raw.get("protected_paths", Config().protected_paths)),
        max_recent_observations=int(raw.get("max_recent_observations", 10)),
        max_observation_chars=int(raw.get("max_observation_chars", 4_000)),
        max_skill_context_chars=int(raw.get("max_skill_context_chars", 12_000)),
        max_skill_scan_bytes=int(raw.get("max_skill_scan_bytes", 512_000)),
        checks=check_specs,
        check_command_prefix=check_prefix,
        evidence_ledger_enabled=bool(raw.get("evidence_ledger_enabled", True)),
        dedup_observations_enabled=bool(raw.get("dedup_observations_enabled", False)),
    )
