# Security review note

## Scope

Reviewed filesystem reads/writes, path normalization, symlink handling, subprocess boundaries, model credentials, traces/reports, memory/skill file access, and external Gerrit/CI callbacks.

## Mitigations present

- Workspace-relative paths reject absolute paths, traversal, drive-qualified paths, protected directories, and symlink components.
- `edit_file` requires the current content hash and exactly one old-text match, writes through a same-directory atomic replacement, and preserves file mode. File creation/deletion is explicit `UNSUPPORTED`.
- Model and local validation subprocesses use fixed argument arrays with `shell=False`; the model cannot provide arbitrary shell commands.
- Tool Registry marks read-only tools; ChunkExecutor preflights the whole chunk and sends every action through ToolExecutor.
- API keys, authorization values, tokens, and passwords are redacted from trace/report values. Model chain-of-thought is not persisted.
- Skill IDs, episode IDs, artifact IDs, and worktree task paths are kept within their managed roots.
- Submission and CI records bind candidate, tree hash, fixed revision, Change-Id, CI run, and tested commit; unknown responses are not retried blindly.

## In-loop trusted check execution (R1)

- **argv source is configuration only.** Every `run_checks` invocation executes `CheckSpec.argv` tuples from the config file (`src/repair_agent/config.py`); the model may select checks by name but cannot define, redefine, or extend a command vector. `load_config` rejects bad names, empty argv slots, and non-positive timeouts at startup, never mid-run.
- **Trusted prefix caveat.** `check_command_prefix` (e.g. a container invocation) is prepended verbatim to every check argv and recorded as `command_prefix_applied` in the tool content. It is convenience, not containment: no OS-level sandbox is claimed by the prefix or by the harness.
- **Residual arbitrary-binary risk.** Whatever the configuration names is executed with the worker's identity. A malicious or careless config still runs arbitrary project tooling — the same trust level as `local-validate`; see Residual risk below.
- **Identity bracket.** `tree_hash` (catches untracked non-ignored writes) plus `git_tree_oid` are taken before and after the checks; any mismatch returns `ERROR`/`complete=False` and blocks the worker. Check verdicts — including PASS — never enter promotion paths; promotion evidence remains post-freeze validation only.
- Output tails are `redact_text`-ed before bounding, so secret-shaped check output does not leak into observations, ledgers, or reports.

## clangd adapter IPC (R4b)

- The adapter spawns the configured binary with a fixed two-token argv (`[binary, --compile-commands-dir=…]`), `shell=False`, `cwd` = the worker worktree. Binary resolution happens at construction; a missing binary is an explicit bounded error.
- The JSON-RPC handshake and every request are individually deadline-bounded (`ClangdConfig.timeout_seconds` clamped to the remaining wall deadline) via a reader thread plus a timeout-bounded queue — a hung server yields a bounded `ERROR` observation, never a hang.
- One server per worker workspace; the executor owner reaps the process tree in a `finally` block, and worktree removal plus process exit bound any leak window.
- Returned file URIs are resolved to workspace-relative paths through the same protected-path/symlink policy as every other tool; escaping or protected paths are dropped, not followed. Retained locations carry content hashes; symbol text passes `redact_text`.
- The server is an LSP-speaking subprocess of the trusted binary — the same trust level as the binary itself; no untrusted input is passed beyond the model-chosen query string, which is sent as a JSON string field only.
- `find_definition`/`find_references` are excluded from action chunks (server lifecycle state makes chunk-replay semantics unsafe), even though they are read-only.

## Review-reason passthrough (R6)

- `model_reason` is model-supplied text surfaced as diagnostic metadata. It passes a single choke-point transform — `redact_text` followed by a hard 1000-char cap with an explicit truncation marker — before storage (`AgentLoop._review`).
- The durable review reason stays byte-identical to the pre-R6 whitelist (`_safe_review_reason` unchanged); the model's words never replace it.
- `sanitize` re-redacts worker-result artifacts and reports downstream — defense in depth, not the primary control.
- `model_reason` never enters `BatchProposal` fields, `PatchScopeGuard`, action-map classification, suppression gating, validation classification, or candidate identity; no code path reads it except display/serialization.

## Restated boundary

The worktree remains an edit isolation mechanism, not a security sandbox for untrusted code. Checks and clangd execute trusted configuration binaries with the worker's identity; they do not contain those binaries.

## Residual risk / required follow-up

- A configured local build command is trusted configuration and can still execute arbitrary project tooling. Run it only in a trusted repository or add a container/process/resource sandbox before untrusted inputs.
- Enterprise authentication and private response schemas are unknown; no real endpoint is guessed or contacted.
- Report and SQLite retention/access policy must be set for enterprise source and logs.
- A complete C++ semantic navigation/security policy needs clangd configuration and a review of compile command provenance.

No credential, network call, build command, scan, or external submission was executed in this round.
