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

- `edit_file` supports only existing regular files, with current-content hash and unique old-text validation. Creation/deletion is explicit `UNSUPPORTED`.
- Chunking is implemented but disabled by default. It accepts only known, independent, read-only actions and stops on empty, ambiguous, partial, truncated, version-changed, unsupported, or error results. It never freezes a candidate.
- Workers have isolated task/batch memory. Only explicitly stored, provenance-bound episodes can be shared.
- Candidate identity is bound to tree hash. New edits, rebases, or a different tested revision cannot inherit approval or validation.
- Enterprise Gerrit and CI adapters return explicit `NOT_CONFIGURED` boundaries without guessing private contracts.
- The worktree is an edit isolation mechanism, not a security sandbox for untrusted code.

## Current verification boundary

This delivery intentionally does not run tests, builds, the application, model calls, benchmarks, Docker setup, real Gerrit, enterprise CI, or CodeSonar. See [docs/implementation-status.md](docs/implementation-status.md) and [docs/reproduction.md](docs/reproduction.md) for the deferred checks.
