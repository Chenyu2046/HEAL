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

The current source tree is authoritative. A textual `search_code` result is not advertised as complete clangd/C++ semantic navigation. `find_definition` and `find_references` return `UNSUPPORTED` until a configured semantic adapter exists.

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

- `search_code` prefers a ripgrep backend (`--json`, fixed argv, `.gitignore`-aware like the tree hash, protected-path globs excluded) and falls back to the bundled Python scanner when `rg` is absent; results are identifier-aware heuristically ranked and aggregated per file. `list_symbols` returns a lexical C/C++ outline (comments and macros are unreliable) from the standard library — both are navigation evidence, not clangd/C++ semantic navigation.
- `edit_file` supports only existing regular files, with current-content hash and unique old-text validation. Creation/deletion is explicit `UNSUPPORTED`.
- Chunking is implemented but disabled by default. It accepts only known, independent, read-only actions and stops on empty, ambiguous, partial, truncated, version-changed, unsupported, or error results. It never freezes a candidate.
- Workers have isolated task/batch memory. Only explicitly stored, provenance-bound episodes can be shared.
- Candidate identity is bound to tree hash. New edits, rebases, or a different tested revision cannot inherit approval or validation.
- Enterprise Gerrit and CI adapters return explicit `NOT_CONFIGURED` boundaries without guessing private contracts.
- The worktree is an edit isolation mechanism, not a security sandbox for untrusted code.

## Benchmark

`py -3.13 scripts/benchmark.py context|recall` runs a fixture-based harness over deterministic synthetic C/C++ repositories (no dataset files; fixtures are generated at runtime into temporary directories). `context` drives the same scripted repair with the ContextCache on and off and reports model calls, per-tool call counts, physical evidence reads, repeated physical reads (方案 §18 "Duplicate Physical File Reads / Task", separate from cache-hit logical calls), and wall time. `recall` reports File Recall@3/@5 over aggregated search results and Symbol Recall@5 over `list_symbols` candidates on planted symbols. Both are deterministic. This harness is **notCovered** for the real 800-warning dataset experiments and the three-arm A/B comparison of 方案 §19 — those require the warning dataset.

## Current verification boundary

This delivery intentionally does not run tests, builds, the application, model calls, benchmarks on real data, Docker setup, real Gerrit, enterprise CI, or CodeSonar. See [docs/implementation-status.md](docs/implementation-status.md) and [docs/reproduction.md](docs/reproduction.md) for the deferred checks.
