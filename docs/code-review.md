# Static code review record

## Review boundary

The repository had no Git metadata or prior implementation at the start of this task. Review therefore used the full source inventory, source call-chain inspection, AST parsing, and targeted search. No tests, build, application startup, model call, benchmark, or external integration was run.

The review covered three independent perspectives: module contracts, security/recovery, and the end-to-end state path.

## Round 1: module contracts

Findings and fixes:

1. Workspace symlink checks originally resolved the path before checking link components. Fixed by checking every raw path component before resolution in `runtime/workspace.py`.
2. Findings accepted an empty file path. Fixed with strict required-field validation in `domain.py`.
3. The task budget was not copied from normalized input. Fixed in `planning.py`; cumulative usage is persisted on the Run record.
4. Untracked files were listed but not included in the real diff evidence. Fixed in `tools/source.py` by collecting Git no-index diffs for untracked files.
5. Validation results loaded from SQLite did not always restore enum values. Fixed in `runtime/store.py`.
6. Simulated Gerrit response loss could not be reconciled because the stored remote record was also marked unknown. Fixed in `adapters/simulated.py`.
7. Candidate/report writes and protected paths needed stronger boundaries. Fixed with atomic writes, candidate tree checks, protected directory checks, and safe Skill/Episode file names.

## Round 2: full business path

Path reviewed:

```text
input -> normalize/deduplicate -> affinity batch -> scheduler
-> isolated worker -> ToolExecutor -> proposal/real diff
-> serial integration -> candidate freeze -> human approval
-> submission intent -> remote reconciliation -> CI callback
-> validation classification -> recovery/report
```

Findings and fixes:

1. Chunk interruption could not produce a candidate because incomplete chunks return review-required and remaining actions are persisted as `NOT_EXECUTED`.
2. Worker edits outside known ranges are paused and run through one bounded re-schedule before the result can be considered; semantic independence is still reported as a review obligation.
3. Candidate approval and submission now re-check the integration workspace tree hash. A post-freeze edit cannot inherit approval.
4. A crash during `SUBMITTING` recovers to `SUBMISSION_UNKNOWN`; submission is blocked until the same Change-Id and fixed revision are reconciled.
5. Old or out-of-order CI callbacks are stored as evidence but cannot move a non-pending or already verified run. Duplicate `(candidate, CI run, revision)` results are deduplicated.
6. Missing checks, source-tree mutation by a validator, and simulated all-pass results cannot become `VALIDATION_PASS`.
7. Local validation has a real trusted-argv entry point, but it was not executed in this round.

## Residual risks

- Different files are still not proof of semantic independence; header/shared-state warnings are surfaced for review, but complete C++ semantic conflict detection requires clangd/compile configuration or human review.
- Enterprise Gerrit, CI, CodeSonar export, authentication, and callback schemas remain configuration-bound and are intentionally `NOT_CONFIGURED` without their private contracts.
- Worktrees isolate edits but are not a sandbox for untrusted code.
- The current empty non-Git workspace cannot provide a runtime Git/worktree or real diff execution evidence.
