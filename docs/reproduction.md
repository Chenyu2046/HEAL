# Deferred reproduction plan

The commands below are for the follow-up verification phase and were not run in this delivery.

1. Put a known Git repository and an exact base commit in `configs/default.json`.
2. Provide a standard task JSON containing a `finding_id`, rule, file, line, message, and base commit.
3. Provide a ScriptedModel decision sequence that reads the finding file, performs a hash-guarded unique edit, and returns `batch_ready` with an explicit `action_map`.
4. Run the CLI in a disposable repository and inspect the real Git diff, candidate tree hash, report, and SQLite trace.
5. Add the deferred test suite for traversal/symlink/hash/unique-match/version/chunk/budget/recovery/identity cases before running local builds or simulated CI.
6. Configure trusted build/UT/scan argv arrays and verify that missing checks are `INCONCLUSIVE`, code failures return to a new attempt, infrastructure failures retry without changing code, and old CI revisions do not advance the current stage.

No simulated or scripted result from this document should be reported as real CodeSonar or enterprise quality evidence.
