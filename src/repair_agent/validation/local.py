"""Trusted-command local validator; commands are config data, never model input."""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from ..domain import Candidate, ValidationResult, redact_text
from ..runtime.workspace import tree_hash
from .results import IndependentValidator


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0


class LocalValidator:
    def __init__(self, classifier: IndependentValidator | None = None) -> None:
        self.classifier = classifier or IndependentValidator()

    def run(self, *, candidate: Candidate, workspace: Path, commit: str, config_id: str, commands: Mapping[str, CommandSpec]) -> ValidationResult:
        before = tree_hash(workspace)
        checks: dict[str, str] = {}
        evidence: dict[str, object] = {"backend": "local", "commands": {name: list(spec.argv) for name, spec in commands.items()}}
        for name in ("build", "ut", "scan"):
            spec = commands.get(name)
            if spec is None:
                checks[name] = "MISSING"
                continue
            started = time.monotonic()
            try:
                result = subprocess.run(list(spec.argv), cwd=workspace, text=True, capture_output=True, shell=False, check=False, timeout=spec.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                checks[name] = "INFRA_FAIL"
                evidence[name] = {"error": "timeout", "stdout": redact_text(str(exc.stdout or "")[-2000:]), "stderr": redact_text(str(exc.stderr or "")[-2000:])}
                continue
            except OSError as exc:
                checks[name] = "INFRA_FAIL"
                evidence[name] = {"error": str(exc)}
                continue
            checks[name] = "PASS" if result.returncode == 0 else "FAIL"
            evidence[name] = {"returncode": result.returncode, "elapsed_ms": int((time.monotonic() - started) * 1000), "stdout": redact_text(result.stdout[-2000:]), "stderr": redact_text(result.stderr[-2000:])}
        after = tree_hash(workspace)
        if before != after:
            checks["workspace_integrity"] = "FAIL"
            evidence["workspace_integrity"] = "validator command modified source tree"
        return self.classifier.classify(candidate=candidate, revision=commit, actual_tested_commit=commit, ci_run_id=f"local-{uuid.uuid4().hex}", config_id=config_id, backend="local", checks=checks, evidence=evidence)
