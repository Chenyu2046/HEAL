# Harman Code Quality Agent

This repository contains a standard-library-only implementation of the controlled workflow described by the Harman design documents.

The executable path is:

```text
JSON Finding / UT Failure
  -> normalize, deduplicate, classify, affinity batches
  -> bounded homogeneous workers in independent Git worktrees
  -> Agent Loop -> ToolExecutor -> real Git diff
  -> serial integration -> Candidate Freeze
  -> human approval -> submission intent / CI identity
  -> validation classification -> recovery/reporting
```

The current source tree is authoritative. A textual `search_code` result is not advertised as complete clangd/C++ semantic navigation. `find_definition` and `find_references` return `UNSUPPORTED` until a configured semantic adapter exists (`clangd` section in the config activates the R4b adapter).

## Positioning vs mainstream coding agents

What this agent does differently from generic coding agents, with the code that carries each claim:

- **In-loop trusted check execution.** Workers run configured build/UT/scan commands mid-loop and read the verdict from the exit code only — output text is never parsed, a check that prints "OK" but exits non-zero is a FAIL. Checks are configuration data; the model can select them by name but never define or redefine them (`src/repair_agent/tools/checks.py`, `src/repair_agent/agent.py` verdict mapping). Each invocation brackets the source tree with `tree_hash` + `git_tree_oid` before and after; any tracked or untracked non-ignored write blocks the worker (`src/repair_agent/runtime/workspace.py`, `src/repair_agent/validation/local.py` precedent).
- **Task memory that is evidence, not vibes.** Every worker carries a bounded `EvidenceLedger` derived deterministically from tool observations (file hashes, per-check verdicts, failed-edit attempts) — never from model claims, except redacted/bounded `batch_ready` statements ingested at one explicit entry point (`src/repair_agent/memory.py`). On a re-plan, the prior attempt's ledger is injected read-only into the new worker's state with an explicit `prior_ledger_loaded` marker in the run payload (`src/repair_agent/orchestrator.py`, `latest_worker_ledger` in `src/repair_agent/runtime/store.py`).
- **Context compression with honest semantics.** Byte-identical in-window `read_file` observations collapse to single-hop references in the prompt while the tools still return full content; the switch is default-off because it changes prompt semantics (`_prompt_observations` in `src/repair_agent/agent.py`).
- **Lexical navigation without pretending to be semantic.** `list_symbols` outlines namespaces, classes, member functions, and member variables with overload ordinals from a stdlib scanner that documents its false-positive ceilings (`src/repair_agent/tools/symbols.py`); semantic navigation is either an explicit `UNSUPPORTED` or a configured, deadline-bounded clangd adapter (`src/repair_agent/tools/clangd.py`) that is deliberately excluded from action chunks (`src/repair_agent/tools/chunking.py`).
- **Failure semantics over success theater.** A check verdict — including PASS — is never promotion evidence; candidates are frozen by tree hash and only post-freeze, identity-matched validation promotes anything (`src/repair_agent/orchestrator.py`, `src/repair_agent/validation/`).

## Entry points

```bash
repair-agent run --task task.json --config configs/default.json --model scripted --decisions decisions.json
repair-agent resume --run-id RUN_ID --config configs/default.json
repair-agent report --run-id RUN_ID --config configs/default.json
repair-agent approve --candidate-id CANDIDATE_ID --reviewer NAME --reason "reviewed" --config configs/default.json
repair-agent submit --candidate-id CANDIDATE_ID --branch main --change-id Iabc --fixed-commit REV --config configs/default.json
repair-agent ci-result --candidate-id CANDIDATE_ID --ci-run-id CI1 --revision REV --actual-tested-commit REV --config-id cfg --backend enterprise --checks '{"build":"PASS","ut":"PASS","scan":"PASS"}' --config configs/default.json
repair-agent local-validate --candidate-id CANDIDATE_ID --workspace PATH --commit REV --config-id cfg --commands '{"build":["cmake","--build","build"],"ut":["ctest","--test-dir","build"],"scan":["codesonar","--version"]}' --config configs/default.json
```

`--decisions` is an explicit ScriptedModel protocol input for later deterministic checks. An empty or missing decision list leads to `REVIEW_REQUIRED`; it does not create a fake pass. The OpenAI-compatible model requires an endpoint and credential environment variable when configured.

## Deliberate boundaries

- `search_code` prefers a ripgrep backend (`--json`, fixed argv, `.gitignore`-aware like the tree hash, protected-path globs excluded) and falls back to the bundled Python scanner when `rg` is absent; results are identifier-aware heuristically ranked and aggregated per file. `list_symbols` returns a lexical C/C++ outline (comments, macros, and templates are unreliable; member-variable detection accepts documented false positives) from the standard library — both are navigation evidence, not clangd/C++ semantic navigation.
- `edit_file` supports only existing regular files, with current-content hash and unique old-text validation. Creation/deletion is explicit `UNSUPPORTED`.
- In-loop checks execute trusted configuration binaries only. The argv source is the config file (optionally behind a configured `check_command_prefix` such as a container invocation); the model cannot supply or alter a command. A prefix is convenience, not containment — no OS-level sandbox is claimed. Unconfigured checks make `run_checks` an explicit `UNSUPPORTED`.
- When clangd is configured, the adapter spawns the configured binary with a fixed argv, bounds the handshake and every request by deadline, and drops escaping/protected paths from results. `find_definition`/`find_references` are excluded from action chunks: they are read-only on paper, but server lifecycle state makes chunk-replay semantics unsafe.
- Chunking is implemented but disabled by default. It accepts only known, independent, read-only actions and stops on empty, ambiguous, partial, truncated, version-changed, unsupported, or error results. It never freezes a candidate.
- Workers have isolated task/batch memory. Only explicitly stored, provenance-bound episodes can be shared. Re-plan injection passes prior evidence read-only and labeled `prior_attempt_evidence`; it is never merged into the new worker's ledger.
- `model_reason` is diagnostic metadata: redacted, hard-capped, and rendered only in review notes/reports. It never enters scope guarding, action classification, suppression gating, or candidate identity.
- Candidate identity is bound to tree hash. New edits, rebases, or a different tested revision cannot inherit approval or validation.
- Enterprise Gerrit and CI adapters return explicit `NOT_CONFIGURED` boundaries without guessing private contracts.
- The worktree is an edit isolation mechanism, not a security sandbox for untrusted code.

## Benchmark

`py -3.13 scripts/benchmark.py context|dedup|suite|recall` runs a fixture-based harness over deterministic synthetic C/C++ repositories (no dataset files; fixtures are generated at runtime into temporary directories). `context` drives the same scripted repair with the ContextCache on and off and reports model calls, per-tool call counts, physical evidence reads, repeated physical reads (方案 §18 "Duplicate Physical File Reads / Task", separate from cache-hit logical calls), and wall time. `dedup` compares in-window read deduplication on vs off. `suite` runs three arms (baseline / cache / ledger) that isolate single mechanism contributions — not the 方案 §19 A/B. `recall` reports File Recall@3/@5 over aggregated search results and Symbol Recall@5 over `list_symbols` candidates on planted symbols. All are deterministic modulo wall time; context sizes are byte-based estimates, never measured tokens — see [docs/BENCHMARK.md](docs/BENCHMARK.md) for field definitions. This harness is **notCovered** for the real 800-warning dataset experiments and the three-arm A/B comparison of 方案 §19 — those require the warning dataset.

## Current verification boundary

This delivery intentionally does not run tests, builds, the application, model calls, benchmarks on real data, Docker setup, real Gerrit, enterprise CI, or CodeSonar. See [docs/implementation-status.md](docs/implementation-status.md) and [docs/reproduction.md](docs/reproduction.md) for the deferred checks.
