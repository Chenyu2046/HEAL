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

## Residual risk / required follow-up

- A configured local build command is trusted configuration and can still execute arbitrary project tooling. Run it only in a trusted repository or add a container/process/resource sandbox before untrusted inputs.
- Enterprise authentication and private response schemas are unknown; no real endpoint is guessed or contacted.
- Report and SQLite retention/access policy must be set for enterprise source and logs.
- A complete C++ semantic navigation/security policy needs clangd configuration and a review of compile command provenance.

No credential, network call, build command, scan, or external submission was executed in this round.
