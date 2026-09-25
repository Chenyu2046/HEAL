"""CLI entry points; no command silently converts unavailable services to PASS."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config
from .domain import to_primitive
from .models import OpenAICompatibleModel, ScriptedModel
from .orchestrator import OrchestratorError, RepairOrchestrator


def _json_file(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc


def _orchestrator(args: argparse.Namespace, task_payload: dict[str, Any] | None = None) -> RepairOrchestrator:
    config = load_config(args.config)
    decisions = _json_file(args.decisions) if getattr(args, "decisions", None) else []
    if not isinstance(decisions, list):
        raise ValueError("--decisions must contain a JSON array")
    model_mode = getattr(args, "model", None) or config.model.provider
    if model_mode == "scripted":
        factory = lambda task, worker_id: ScriptedModel(decisions, model_id="scripted")
    elif model_mode in {"openai-compatible", "openai_compatible"}:
        factory = lambda task, worker_id: OpenAICompatibleModel(endpoint=config.model.endpoint, model_id=config.model.model_id, api_key_env=config.model.api_key_env, timeout_seconds=config.model.timeout_seconds)
    else:
        raise ValueError(f"unsupported model mode: {model_mode}")
    return RepairOrchestrator(config, model_factory=factory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repair-agent")
    parser.add_argument("--config", default=None, help="JSON configuration")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--model", choices=("scripted", "openai-compatible"), default=None)
    run.add_argument("--decisions", default=None, help="JSON decisions for ScriptedModel")
    for name in ("max-model-calls", "max-tool-calls", "max-tokens", "max-wall-seconds", "max-edit-attempts", "max-chunk-actions"):
        run.add_argument(f"--{name}", type=float if name == "max-wall-seconds" else int, default=None)
    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--candidate-id", required=True)
    reconcile.add_argument("--submission-id", required=True)
    retry_ci = sub.add_parser("retry-ci-dispatch")
    retry_ci.add_argument("--candidate-id", required=True)
    gc = sub.add_parser("gc")
    gc.add_argument("--older-than-hours", type=float, default=168.0)
    gc.add_argument("--apply", action="store_true", help="perform cleanup; default is dry-run")
    gc.add_argument("--include-pending-review", action="store_true", help="allow expired HUMAN_REVIEW workspaces to be removed")
    report = sub.add_parser("report")
    report.add_argument("--run-id", required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--candidate-id", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--approved", action=argparse.BooleanOptionalAction, default=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--candidate-id", required=True)
    submit.add_argument("--branch", required=True)
    submit.add_argument("--change-id", required=True)
    submit.add_argument("--candidate-commit", "--fixed-commit", dest="candidate_commit", required=True)
    ci = sub.add_parser("ci-result")
    ci.add_argument("--candidate-id", required=True)
    ci.add_argument("--ci-run-id", required=True)
    ci.add_argument("--dispatch-id", required=True)
    ci.add_argument("--revision", required=True)
    ci.add_argument("--actual-tested-commit", default=None)
    ci.add_argument("--config-id", required=True)
    ci.add_argument("--backend", required=True)
    ci.add_argument("--checks", required=True, help="JSON object, e.g. '{\"build\":\"PASS\"}'")
    ci.add_argument("--final", action="store_true", help="mark an incremental CI callback as final")
    local = sub.add_parser("local-validate")
    local.add_argument("--candidate-id", required=True)
    local.add_argument("--workspace", required=True)
    local.add_argument("--commit", required=True)
    local.add_argument("--config-id", required=True)
    local.add_argument("--commands", required=True, help="JSON object mapping check names to argv arrays")
    for child in sub.choices.values():
        child.add_argument("--config", dest="config", default=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            payload = _json_file(args.task)
            if not isinstance(payload, dict):
                raise ValueError("task JSON must be an object")
            overrides = {key: getattr(args, key) for key in ("max_model_calls", "max_tool_calls", "max_tokens", "max_wall_seconds", "max_edit_attempts", "max_chunk_actions") if getattr(args, key) is not None}
            result = _orchestrator(args).run(payload, cli_budget_overrides=overrides)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
            return 0
        orchestrator = _orchestrator(args)
        if args.command == "resume":
            print(json.dumps(to_primitive(orchestrator.resume(args.run_id)), ensure_ascii=False, indent=2))
        elif args.command == "reconcile":
            result = orchestrator.reconcile_submission(args.candidate_id, submission_id=args.submission_id)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
        elif args.command == "retry-ci-dispatch":
            result = orchestrator.retry_ci_dispatch(args.candidate_id)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
        elif args.command == "gc":
            result = orchestrator.gc(older_than_hours=args.older_than_hours, apply=args.apply, include_pending_review=args.include_pending_review)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
        elif args.command == "report":
            print(json.dumps({"paths": orchestrator.report(args.run_id)}, ensure_ascii=False, indent=2))
        elif args.command == "approve":
            approval = orchestrator.approve(args.candidate_id, reviewer=args.reviewer, reason=args.reason, approved=args.approved)
            print(json.dumps(to_primitive(approval), ensure_ascii=False, indent=2))
        elif args.command == "submit":
            result = orchestrator.submit(args.candidate_id, branch=args.branch, change_id=args.change_id, candidate_commit=args.candidate_commit)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
        elif args.command == "ci-result":
            checks = json.loads(args.checks)
            if not isinstance(checks, dict):
                raise ValueError("--checks must be a JSON object")
            result = orchestrator.receive_ci(args.candidate_id, ci_run_id=args.ci_run_id, revision=args.revision, actual_tested_commit=args.actual_tested_commit, config_id=args.config_id, backend=args.backend, checks=checks, dispatch_id=args.dispatch_id, final=args.final)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
        elif args.command == "local-validate":
            commands = json.loads(args.commands)
            if not isinstance(commands, dict) or any(not isinstance(argv, list) for argv in commands.values()):
                raise ValueError("--commands must map check names to argv arrays")
            result = orchestrator.validate_local(args.candidate_id, commit=args.commit, config_id=args.config_id, workspace=args.workspace, commands=commands)
            print(json.dumps(to_primitive(result), ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, OrchestratorError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
