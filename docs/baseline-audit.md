# Baseline audit

## Current repository

At implementation start, the provided workspace was an empty non-Git directory with no existing source, tests, lock file, or `AGENTS.md`. There was therefore no existing parser, model client, executor, persistence layer, or CLI to reuse. The implementation is new and standard-library-only.

## Upstream references

The two supplied design documents name mini-swe-agent and CodeCureAgent as reference ideas. No upstream checkout, fixed commit, license file, or source audit was present in this repository, so no upstream code was copied. The package keeps the relevant separation—model, controlled environment, and business orchestration—without claiming upstream behavior or license inheritance.

## Reuse decision

The implementation uses Python standard-library primitives (`dataclasses`, `pathlib`, `sqlite3`, `subprocess` with argument arrays, `urllib`, and `concurrent.futures`). External enterprise formats remain adapters with explicit missing-contract errors. No dependency installation or upstream runtime setup was performed.
