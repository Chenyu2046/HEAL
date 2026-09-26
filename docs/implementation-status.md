# Implementation status

## Implemented in source

- Finding / UT Failure contracts, normalization, stable identity, deduplication, risk classification, and affinity Working Batch planning.
- Model/Provider protocol, ScriptedModel, OpenAI-compatible JSON tool client, bounded Agent Loop, budget accounting, observations, and stop conditions.
- Tool Registry / Executor, workspace path policy, bounded read/search, explicit unsupported semantic navigation, hash-guarded unique edit, and real Git diff lookup.
- ActionChunk / ChunkExecutor / BoundaryDetector with default-off integration and conservative stop behavior.
- Bounded homogeneous workers, independent Git worktree creation, known-range conflict scheduling, serial integration, expanded-scope and semantic-review warnings.
- Task State, revision/hash-bound Batch Cache, isolated Worker Episode Store, keyword/metadata retrieval, provenance fields, and versioned Skill routing.
- Version-checked Context Cache (per-worker `ContextCache` facade): full-text `read_file` replay and non-empty complete-scan `search_code` replay validated against touched-file hashes, 64-entry LRU regions, `context_cache_enabled` switch; PARTIAL and EMPTY scans are not replayed in v1.
- Progressive repository navigation (方案 Phase 2): ripgrep-backed `search_code` (`rg --json --fixed-strings --max-filesize` with fixed argv, protected-path `--glob` exclusions, deadline-bound subprocess timeout, automatic fallback to the previous Python scanner when `rg` is absent), identifier-aware heuristic ranking (exact identifier / same file / same module / warning symbol / analysis-trace tokens, batch context injected by the orchestrator), file-level aggregation with per-file hit counts and sample lines, `total_hits`, and PARTIAL on caps; heuristic standard-library `list_symbols` (lexical C/C++ outline in `tools/symbols.py`, comments/string braces stripped before brace pairing, class-external `Class::method` recognized, SymbolCache as the third `ContextCache` region with hash-validated zero-read replay, EMPTY/TRUNCATED semantics); context-expansion budgets `max_context_files`/`max_symbol_expansions`/`max_search_rounds` with pre-execution enforcement, whitelisted review reasons, usage counting, slot budget slicing, and resume remaining calculation.
- Batch Proposal, serial integration, tree-hash Candidate Freeze, candidate-bound approval, independent result classification, and code/infra/inconclusive separation.
- SQLite RunStore, atomic artifact writes, trace redaction, checkpoints, artifact reconciliation, submission intents, `SUBMISSION_UNKNOWN`, and CI identity deduplication.
- CLI, JSON configuration, Markdown/JSON reporting, and explicit unconfigured Gerrit/CI boundaries.

## Intentionally not executed in this delivery

Tests, builds, application startup, model calls, benchmark/A-B/C runs, Docker/dependency installation, real Gerrit, enterprise CI, CodeSonar, and end-to-end validation were not run per the user request.

## Static checks performed

All 31 Python source files were parsed with the Python AST parser without importing or starting the package. Relative-import targets were checked against the source inventory. No forbidden stub markers (`NotImplementedError`, `TODO`, `FIXME`, or a bare `pass`) remain in `src`.

## Known external contracts

Enterprise Gerrit endpoint/authentication/Change-Id and response schemas, enterprise CI submission/callback schemas, CodeSonar export format, clangd/compile_commands availability, and repository-specific trusted build/UT/scan commands still need configuration and real samples. The code returns explicit `NOT_CONFIGURED` or `UNSUPPORTED` where those contracts are absent.
