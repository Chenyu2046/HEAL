# HEAL Technical Design — Agent Harness Reliability Round

Status: design of record for this round; implements `docs/requirements.md` R1–R7 as restated by `docs/prd.md` §4. Branch baseline `codex/harman-agent-harness-reliability` at `51075a2`.
Baseline verified in the design session: `py -3.13 -m unittest discover -s tests -p "test_*.py"` → **Ran 109 tests … OK** (73.6 s, executed on this working tree). No group below may regress that suite.

Design language: field-level data-structure drafts, explicit integration points with existing mechanisms (cited by `path:line`), failure semantics pinned per `ToolStatus`/`complete`, compatibility/migration notes, test plans keyed to PRD acceptance criteria, and deliberate ceilings. Where this design deviates from or tightens the PRD, the deviation is called out inline and summarized in §6.

Implementation groups:

| Group | Requirements | Theme |
|-------|--------------|-------|
| G1 | R1 | In-loop execution feedback: `run_checks` tool, `max_check_runs` budget, tree-identity integrity, optional trusted command prefix |
| G2 | R2, R3 | Memory & compression: `EvidenceLedger`, `batch_ready` hypothesis fields, checkpoint persistence, re-plan injection, in-window observation dedup |
| G3 | R4a, R4b (stretch), R5a, R5b, R6, R7 | Semantics & evaluation: lexical scanner enhancements, clangd adapter (cut-eligible), benchmark suite, model smoke tests, reason passthrough, documentation |

Ship order (per requirements §7): **R1 → R2 → R3 → R4a → R5a → R4b (review decision) → R6 → R7**, suite green after each step. Each group lands as its own commit(s); the design doc is committed separately from feature code.

---

## 0. Verified current-state facts this design builds on

Every design decision below is grounded in these readings of the current code (all read in this session):

- **Blocking rule.** `_record` (agent.py:331-344) appends `(tool, status)` to `_blocking_tool_failures` for any observation with `complete=False` or status in `{ERROR, PARTIAL, TRUNCATED, VERSION_CHANGED, AMBIGUOUS, UNSUPPORTED, NOT_EXECUTED}`; the list is cleared only at run start (agent.py:178) and any entry rejects `batch_ready` (agent.py:293-296). This is why G1's verdict→status mapping is load-bearing.
- **Executor envelope.** `ToolExecutor.execute` (executor.py:114-147): schema validation (`ToolSpec.validation_error`, executor.py:37-56) runs before handlers; handlers receive `_deadline` (executor.py:136-138); a non-read-only tool takes the write lock (executor.py:134); uncaught handler exceptions become `ERROR`/`complete=False` (executor.py:140-141); serialized content above `limits.max_output_chars` is downgraded to `TRUNCATED`/`complete=False` (executor.py:142-145).
- **Trusted-execution precedent.** `LocalValidator` (validation/local.py:24-70): fixed argv, `subprocess.run(..., shell=False, check=False, timeout=...)`, `redact_text` on captured output tails, exit code as the sole PASS/FAIL source (local.py:58), `tree_hash`+`git_tree_oid` before/after (local.py:30-35, 60-69). `git_tree_oid` alone uses the index (`git write-tree`, workspace.py:81-96) and would **miss** untracked non-ignored writes; `tree_hash` enumerates `git ls-files --cached --others --exclude-standard` (workspace.py:48-78) and catches them. G1 must use both, exactly like `LocalValidator`.
- **Budget enumeration is manual.** Repo-wide grep for `max_search_rounds` confirms exactly eleven code sites across six files: `domain.py:258,261`, `agent.py:119,321,368,385`, `config.py:79`, `planning.py:106,126`, `orchestrator.py:352,683`. **Additional audit finding:** the usage side is a twelfth sibling site the budget-key grep does not surface — `RunStore.update_worker_budget` enumerates usage-counter names in a `fields` tuple (store.py:202), and slot allocation (orchestrator.py:343-361) plus `resume()` remaining (orchestrator.py:672-684) read `used.get("<counter>")` from the aggregated snapshot. A new budget therefore needs *both* greps: `max_check_runs` and `check_runs`. This is folded into R1 acceptance 5 below.
- **Checkpoint rows.** `save_checkpoint` writes free-form payloads into a table keyed `(checkpoint_id, run_id)` (store.py:93-97, 311-313); worker checkpoints currently carry two booleans (orchestrator.py:334). There is no cross-run checkpoint read today.
- **Re-plan reality.** `new_attempt_id` is bookkeeping only (written at orchestrator.py:566, 757; consumed only by the same-run guard at store.py:619). The real re-entry is `resume()` → action `replan_required` (orchestrator.py:653, 657) → the caller opens a new run with a new `run_id`.
- **Prompt construction.** `_prompt_observations` (agent.py:402-427): out-of-window observations collapse to `historical_summary` (50-path cap) and `pinned_evidence` (8-entry cap); the recent window is re-serialized in full every prompt.
- **Context cache.** `ContextCache` replays full content on hit (`_replay_read`, source.py:136-154); stats are pure counters (context.py:180-192).
- **Scanner.** `scan_symbols`/`_scan` (tools/symbols.py): only `class`/`struct` propagate as enclosing prefixes (symbols.py:400); `_try_function` requires a following `(` (symbols.py:314-316), so data members are never captured; output is bounded by `MAX_SYMBOL_DECLS` + char budget (`_symbol_payload`, source.py:187-220).
- **Reporting.** `ReportWriter._markdown` renders a fixed section list (reporting.py:51-74); payload keys are sanitized via `sanitize` → `redact_text` (runtime/trace.py:12-34).

No PRD/requirements claim was found contradicted by the code. One tightening was found and is adopted: the budget-completeness audit must cover the usage-counter site (`store.py:202`) in addition to the eleven budget-key sites (§2.5).

---

## 1. G1 — In-loop execution feedback (R1, P0)

### 1.1 Files touched

| File | Change |
|------|--------|
| `src/repair_agent/tools/checks.py` | **New.** `CheckTools` handler: configured-check execution, verdict mapping, output bounding, identity bracket. |
| `src/repair_agent/tools/executor.py` | Register `run_checks` spec + handler; thread `check_specs`/`check_prefix` through `ToolExecutor.__init__`. |
| `src/repair_agent/config.py` | `CheckSpec`, `Config.checks`, `Config.check_command_prefix`, `ToolLimits.check_timeout_seconds`, `load_config` parsing/validation. |
| `src/repair_agent/domain.py` | `Budget.max_check_runs` + validation (domain.py:258, 261). |
| `src/repair_agent/agent.py` | `AgentUsage.check_runs`; `within()`; pre-execution gate; exhausted-reasons entry; run counting; check-verdict summary in proposal review notes. |
| `src/repair_agent/planning.py` | Budget default + CLI-override plumbing (planning.py:97-127). |
| `src/repair_agent/orchestrator.py` | Slot allocation (orchestrator.py:352) and `resume()` remaining (orchestrator.py:683); pass `check_specs`/`check_prefix` into `ToolExecutor` in `_run_worker` (orchestrator.py:327). |
| `src/repair_agent/runtime/store.py` | `update_worker_budget` `fields` tuple gains `"check_runs"` (store.py:202). |
| `tests/test_run_checks.py` | **New.** Acceptance tests (§1.7). |

### 1.2 Configuration data structures (field-level)

```python
# config.py
@dataclass(frozen=True)
class CheckSpec:
    name: str                      # ^[a-z][a-z0-9_]{0,63}$ — the only string the model may select
    argv: tuple[str, ...]          # fixed command vector; every element a non-empty string
    timeout_seconds: float = 120.0 # per-check cap; clamped to the remaining wall deadline

@dataclass(frozen=True)
class Config(...):
    checks: tuple[CheckSpec, ...] = ()          # empty ⇒ tool is UNSUPPORTED
    check_command_prefix: tuple[str, ...] = ()  # optional trusted prefix (§1.6)

@dataclass(frozen=True)
class ToolLimits(...):
    check_timeout_seconds: float = 120.0        # default when a spec omits timeout
```

`load_config` parsing (extends config.py:90-127):

```jsonc
// config JSON
{
  "checks": {
    "build": {"argv": ["make", "build"], "timeout_seconds": 180},
    "ut":    ["ctest", "--output-on-failure"]          // bare argv form ⇒ default timeout
  },
  "check_command_prefix": ["docker", "run", "--rm", "-v", ".:/ws", "heal-checks:1"]
}
```

Validation rules (all violations raise `ConfigError` at load — fail at startup, never mid-run):
- check name matches `^[a-z][a-z0-9_]{0,63}$`; duplicate names rejected;
- `argv` non-empty, every element a non-empty string; empty-element rejection prevents accidental empty argv slots after prefixing;
- `timeout_seconds > 0`; no upper clamp at config time (clamping to the wall deadline happens at execution, §1.4);
- `check_command_prefix` elements are non-empty strings; empty tuple ⇒ no prefix.

### 1.3 Tool surface (field-level)

Registered unconditionally (mirrors `find_definition`/`memory_retrieve`, executor.py:103-104, 108) so the schema is stable across configurations:

```python
ToolSpec(
    name="run_checks",
    description=("Run configured trusted checks by name; the verdict comes from the exit code only. "
                 "Checks are configuration data; they cannot be defined or redefined from here."),
    read_only=False,                 # ⇒ never chunk-eligible (chunking.py:48-52), takes the write lock (executor.py:134)
    required_args=("names",),
    allowed_in_chunk=False,
    properties={"names": {"type": "array", "items": {"type": "string"}}},
)
```

Argument rules, mapped to acceptance 8:
- **Extra argument keys** — rejected by the existing generic schema check (`unknown = set(arguments) - set(self.properties)`, executor.py:41-43) before any handler code runs.
- **Empty `names` list** — the generic validator cannot express "minItems" for arrays (its `minLength` branch calls `.strip()`, which would crash on a list; executor.py:54-55), so the handler rejects `names == []` with `ERROR` before executing anything.
- **Unknown check names** — the handler validates *all* names against the configured specs first and executes **none** on any unknown name (all-or-nothing, pre-execution). This satisfies "rejected … before execution" even though the name set is dynamic and cannot be a JSON schema enum.

`ToolExecutor.__init__` gains `check_specs: tuple[CheckSpec, ...] = ()` and `check_command_prefix: tuple[str, ...] = ()`; the handler is `CheckTools(workspace, specs, prefix, limits).run_checks` in the new `tools/checks.py`.

### 1.4 Execution semantics (field-level)

`CheckTools.run_checks(arguments)` returns the standard handler 6-tuple `(status, content, paths, hashes, complete, error)`:

1. **Unconfigured** — `specs` empty ⇒ `(UNSUPPORTED, None, (), {}, False, "no checks are configured")`. Identical shape to `memory_retrieve` unconfigured (executor.py:161-163). Note the honest consequence: per the `_record` rule this observation is blocking (agent.py:338-342). That is the existing, deliberate semantic for calling an unconfigured capability (same as `memory_retrieve`); the tool description says checks must be configured.
2. **Pre-execution validation** — empty/unknown names ⇒ `(ERROR, None, (), {}, False, "unknown checks: …")`. Nothing has executed.
3. **Identity bracket (before)** — `before = tree_hash(workspace.root)`, `before_oid = git_tree_oid(workspace.root)` (workspace.py:48-96; `LocalValidator` precedent local.py:30-31). `WorkspaceError`/`OSError` here ⇒ `(ERROR, None, (), {}, False, "identity unavailable: …")` — acceptance 3's "identity inputs unavailable" path; blocks.
4. **Per-check execution**, in the order the model listed the names (deterministic):
   - effective argv = `[*check_command_prefix, *spec.argv]`; `cwd = workspace.root`; `shell=False`; `check=False`; `capture_output=True`; `text=True`;
   - timeout = `min(spec.timeout_seconds, remaining_wall_deadline)` where remaining comes from the executor-injected `_deadline` (executor.py:136-138); if remaining ≤ 0 before a later check starts, that check is recorded with verdict `INFRA_FAIL` and error `"deadline exhausted before execution"` (never silently skipped);
   - `subprocess.TimeoutExpired` ⇒ verdict `INFRA_FAIL`, evidence `{error: "timeout", stdout_tail, stderr_tail}` (local.py:50-53 pattern);
   - `OSError` ⇒ verdict `INFRA_FAIL`, evidence `{error: redact_text(str(exc))}` (local.py:54-57 pattern);
   - otherwise verdict = `"PASS" if returncode == 0 else "FAIL"` — **exit code is the sole verdict source; output text is never parsed** (local.py:58 pattern).
5. **Output bounding (pre-trim arithmetic).** Per-check stream tails are capped so the serialized content provably stays below the executor downgrade threshold (executor.py:142-145): `tail = max(256, (max_output_chars − 4096) // (2 * len(results)))` chars per stream, applied **after** `redact_text` (domain.py:455-459), each capped stream suffixed `"…[truncated]"` when cut. A final guard re-checks `len(canonical_json(content)) < limits.max_output_chars` and trims tails further if needed. A multi-megabyte build log therefore can never downgrade a legitimate verdict to `TRUNCATED`/`complete=False` (acceptance 2).
6. **Identity bracket (after)** — `after`/`after_oid` computed exactly like step 3.
   - hash/OID mismatch (tracked change **or** untracked non-ignored file created by the check) ⇒ `(ERROR, content, (), {}, False, "check integrity mismatch: source tree changed during run_checks (before=<h8>… after=<h8>…)")`. Status `ERROR` is chosen over `OK`/`complete=False` so the blocking review reason reads `run_checks/ERROR` (agent.py:295 renders `tool/status`); `complete=False` is what blocks. Per-check verdicts already obtained remain in `content` — evidence is never discarded, but the worker is blocked (fail-closed, acceptance 3). Why both hashes: `git_tree_oid` alone reads the index and would miss untracked non-ignored writes (§0); `tree_hash` catches them; both mirror `LocalValidator` (local.py:30-35, 60-69).
   - git failure on the after-side ⇒ same `ERROR`/`complete=False` shape with `"could not verify source tree after checks: …"` (local.py:63-66 pattern).
7. **Content** (the verdict lives here, not in the status):

```jsonc
{
  "checks": [
    {
      "name": "build", "verdict": "FAIL", "returncode": 2,
      "elapsed_ms": 1234, "timeout_seconds": 180,
      "stdout_tail": "…redacted…", "stderr_tail": "…redacted…",
      "truncated_streams": false, "error": null          // INFRA_FAIL reason, redacted
    }
  ],
  "integrity": {"before_tree_hash": "…", "after_tree_hash": "…",
                 "before_git_tree_oid": "…", "after_git_tree_oid": "…", "verified": true},
  "command_prefix_applied": false
}
```

`source_paths=()`, `file_hashes={}` — file-level evidence accounting (`usage.context_files`, agent.py:334-337) is untouched by checks; identity is carried by the tree hashes in `content`.

### 1.5 Status/complete mapping (normative for blocking behavior)

| Outcome | ToolStatus | complete | Blocks `batch_ready`? | In `_blocking_tool_failures`? |
|---|---|---|---|---|
| Verdict `PASS` | `OK` | `True` | no | no |
| Verdict `FAIL` (any output size) | `OK` | `True` | no | no |
| Verdict `INFRA_FAIL` (timeout / OSError / deadline) | `OK` | `True` | no | no |
| Unconfigured | `UNSUPPORTED` | `False` | **yes** | yes (existing rule) |
| Unknown/empty names (pre-execution) | `ERROR` | `False` | **yes** | yes |
| Identity mismatch / identity unavailable | `ERROR` | `False` | **yes** | yes |
| Uncaught handler exception | `ERROR` | `False` | **yes** | yes (executor.py:140-141) |

Rationale (per requirements R1): `ToolStatus` describes the tool call's execution; the verdict field describes the check's outcome. `OK`/`complete=True` truthfully means "the check executed and this is its complete verdict" — the same shape as `search_code` returning `EMPTY`/complete. `INFRA_FAIL` is explicitly not-PASS, is persisted in the ledger (G2) and surfaced in proposal review notes (§1.6); it does not block because the blocking list never clears within a run (agent.py:178) and one flaky build tool would otherwise convert every worker into `REVIEW_REQUIRED`. A worker may still propose while its last check timed out — mitigated by honest surfacing, revisit with an N-strikes rule if review experience demands it. A check verdict — including PASS — is never promotion evidence; post-freeze independent validation (orchestrator.py:747-763) is unchanged.

### 1.6 Optional trusted command prefix

`check_command_prefix` (e.g. a container invocation) is prepended to every check argv. It is convenience, not containment: no OS-level sandbox is claimed; "the worktree is an edit isolation mechanism, not a security sandbox" (README boundary, restated in R7's security review). `content.command_prefix_applied` records whether a prefix was active, so the audit trail distinguishes raw from prefixed execution.

### 1.7 Budget plumbing (`max_check_runs`) — the completeness audit

New `Budget.max_check_runs: int = 8` (validated non-negative in the existing `min(...)` check, domain.py:261). Every site that must be touched, with the verified sibling it mirrors:

| # | Site | Mirrors (`max_search_rounds`) | Change |
|---|------|-------------------------------|--------|
| 1 | `domain.py` Budget field + `__post_init__` | domain.py:258, 261 | add field; extend `min(...)` |
| 2 | `agent.py` `AgentUsage.check_runs: int = 0` | — | new usage counter |
| 3 | `agent.py` `AgentUsage.within` | agent.py:119 | `and self.check_runs < budget.max_check_runs` |
| 4 | `agent.py` exhausted-reasons list | agent.py:321 | `"check run budget exhausted"` |
| 5 | `agent.py` `_tool_budget_error` (single-call pre-gate) | agent.py:368 | signature gains `requested_check_runs: int = 0`; rejects when `usage.check_runs + requested_check_runs > budget.max_check_runs` — all-or-nothing, before any command runs |
| 6 | `agent.py` `_chunk_budget_error` | agent.py:385 | **deliberately absent**: `run_checks` is `read_only=False`, so `BoundaryDetector.validate` rejects any chunk containing it (chunking.py:48-52) and the chunk pre-gate is unreachable for this tool; documented so the audit is honest rather than "complete" by omission |
| 7 | `agent.py` run-loop counting | agent.py:257-260 | after `_record`, `usage.check_runs += len(content["checks"])` when the observation executed checks (0 on pre-execution rejection — a schema error never burns budget) |
| 8 | `config.py` `_budget` | config.py:79 | `max_check_runs=int(value.get("max_check_runs", 8))` |
| 9 | `planning.py` resolved-budget defaults | planning.py:106 | add default entry |
| 10 | `planning.py` `Budget(...)` construction | planning.py:126 | pass through (CLI overrides flow via planning.py:109) |
| 11 | `orchestrator.py` `_allocate_slot_budgets` | orchestrator.py:352 | `"max_check_runs": max(0, total.max_check_runs - int(used.get("check_runs", 0)))` |
| 12 | `orchestrator.py` `resume()` remaining | orchestrator.py:683 | same shape against the run task budget |
| 13 | `runtime/store.py` `update_worker_budget` fields tuple | store.py:202 | **usage-side site found by this design** (grep `check_runs`, not `max_check_runs`): without it the per-worker snapshot drops the counter, slot allocation and resume remaining silently read 0 — the one fail-open direction in the accounting |

The audit command recorded for acceptance 5: `grep -rn "max_check_runs" src/ scripts/` **and** `grep -rn "check_runs" src/repair_agent/` — expected 13 code sites listed above (plus tests). Each `run_checks` tool call also consumes a normal tool call (`usage.tool_calls += 1`, agent.py:254), so `max_tool_calls` still bounds invocation count.

### 1.8 Proposal review notes

`_proposal` (agent.py:464-519) appends one bounded note summarizing per-check last verdicts from the run's `run_checks` observations, sorted by check name, capped at the first 8 checks:

```
"In-loop checks: build=FAIL(exit 2), ut=INFRA_FAIL(timeout)"
```

A FAIL or INFRA_FAIL verdict therefore reaches the proposal's review notes (acceptances 2 and 4) without entering any decision path — `review_notes` is display metadata on `BatchProposal` (domain.py:316) and never influences scope guarding or classification.

### 1.9 Compatibility & migration

- No existing behavior changes: with `checks` unconfigured the registry gains one tool that returns `UNSUPPORTED`; every existing test and flow is untouched (acceptance 7 requires exactly this plus the green suite).
- `Budget`/`AgentUsage` gain defaulted fields — persisted payloads (`to_primitive(usage)` via the usage callback, orchestrator.py:330; store.py:199-229) tolerate old snapshots missing `check_runs` (`used.get("check_runs", 0)` pattern already used at orchestrator.py:352, 683).
- Rollback: removing the `checks` config section returns the tool to `UNSUPPORTED`; code revert is independent (PRD §8.3).

### 1.10 Deliberate trade-offs & ceilings

- **Wall clock:** default per-check timeout (120 s) ≪ default `max_wall_seconds` (900 s), but N checks can consume N× that; the pre-gate bounds count, not time. Ceiling accepted; the timeout is config data.
- **Tree hashing cost:** two full `tree_hash` calls (each `git ls-files` + per-file sha256) per `run_checks` invocation — O(tree size). Acceptable at fixture/enterprise-repo scale for one invocation per iteration; upgrade path if needed: hash only files listed by `git status --porcelain` plus a sampling root, at the cost of weaker integrity (documented, not built).
- **All-or-nothing multi-name calls:** a call naming 5 checks with budget for 3 executes none and returns `ERROR`. Simpler than partial execution with `NOT_EXECUTED` sub-results inside one observation; the model can re-issue smaller calls.
- **`names` cannot be a schema enum:** dynamic config vs static schema — rejected pre-execution in the handler instead (§1.3).
- **No output parsing, ever:** a check that prints "OK" but exits non-zero is a FAIL. This is the feature, not a bug.

### 1.11 Test plan (acceptance → test, new `tests/test_run_checks.py` unless noted)

| PRD/Requirements acceptance | Test |
|---|---|
| 1. Deterministic verdict + truncated redacted output; exit code sole source | Real subprocess fixtures (`sys.executable -c "import sys; sys.exit(0)"` / `exit 2`); assert `verdict`, `returncode`, redaction of a secret-shaped stdout token |
| 2. FAIL non-blocking incl. output > `max_output_chars` | Scripted `run_checks` (exit 1, prints > `max_output_chars` chars incl. a secret) → observation `OK`/`complete=True`, `_blocking_tool_failures` empty, subsequent `batch_ready` accepted, verdict present in proposal `review_notes` |
| 3. Integrity directions | (a) check creates `scratch.txt` (untracked, not ignored) → `ERROR`/`complete=False`, `batch_ready` rejected with `run_checks/ERROR` in the reason; (b) check writes only into `build/` (gitignored, fixture `.gitignore` per `make_repo` convention) → integrity verified, non-blocking; (c) git failure injected via `patch` on `tree_hash` → `ERROR`/`complete=False`, blocks |
| 4. INFRA_FAIL stance | Sleep-past-timeout fixture and nonexistent-binary fixture → verdict `INFRA_FAIL`, `OK`/`complete=True`, `batch_ready` accepted, verdict in ledger and review notes |
| 5. Budget completeness audit | Grep audit over both key sets with the 13-site expectation (§1.7) + behavioral: config-JSON settable; survives CLI budget override (planning.py:109 path); exhaustion yields `"check run budget exhausted"`; slot split in `_allocate_slot_budgets`; `resume().budget_remaining["max_check_runs"]` |
| 6. Never chunked | `ActionChunk` containing `run_checks` → `ChunkExecutor.execute` returns not-accepted with `non-read-only tool is not eligible for chunking` (chunking.py:49) |
| 7. Unconfigured `UNSUPPORTED` + suite green | Unconfigured executor → `UNSUPPORTED`/`complete=False`; full suite run |
| 8. Schema-preflight rejection | Extra argument key → executor-level `ERROR` before handler; unknown name → handler `ERROR` with no subprocess spawned (assert via counting fixture) |

---

## 2. G2 — Memory & compression (R2, R3, P0/P1)

### 2.1 Files touched

| File | Change |
|------|--------|
| `src/repair_agent/memory.py` | **New:** `EvidenceLedger`, `FileEvidence`, `CheckRecord`, `AttemptRecord`, `build_ledger()`; `TaskStateMemory` kept but marked deprecated (import compatibility; AgentLoop stops using it). |
| `src/repair_agent/agent.py` | AgentLoop uses `EvidenceLedger`; `state["evidence_ledger"]` per turn; batch_ready model-field ingestion; `_prompt_observations` dedup (switchable); `AgentResult.ledger`/`AgentResult.model_reason` (the latter lands with G3/R6 but is declared here to avoid two contract changes); constructor params `evidence_ledger_enabled`, `dedup_recent_observations`, `prior_attempt_evidence`. |
| `src/repair_agent/models.py` | `ModelDecision.from_mapping`: optional `hypothesis`/`next_questions`/`attempt_summary` on `batch_ready` only (models.py:100-104). |
| `src/repair_agent/orchestrator.py` | `_run_worker` persists ledger in the checkpoint payload (orchestrator.py:334); `run(..., replan_of_run_id=...)`; prior-ledger load + run-payload markers; `_run_worker` passes `prior_attempt_evidence`; `resume()` output exposes the linkage (orchestrator.py:685). |
| `src/repair_agent/runtime/store.py` | **New:** `latest_worker_ledger(run_id)` cross-run read; worker checkpoint payload gains `"kind": "worker"`. |
| `src/repair_agent/config.py` | `evidence_ledger_enabled: bool = True`, `dedup_observations_enabled: bool = False`, ledger bound constants wired from `Config`. |
| `scripts/benchmark.py` | `dedup` comparison mode (R3 acceptance 4; §3.5). |
| `tests/test_task_memory.py`, `tests/test_prompt_dedup.py` | **New.** |

### 2.2 EvidenceLedger (R2) — data structures (field-level)

```python
# memory.py
MAX_LEDGER_FILES = 50          # mirrors the 50-path cap of historical_summary (agent.py:412)
MAX_LEDGER_ATTEMPTS = 8        # mirrors the 8-entry cap of pinned_evidence (agent.py:414-416)
MAX_HYPOTHESIS_CHARS = 1_000
MAX_ATTEMPT_SUMMARY_CHARS = 1_000
MAX_NEXT_QUESTIONS = 5
MAX_QUESTION_CHARS = 300
_TRUNCATION_MARKER = "…[truncated]"   # same marker style as agent.py:424

@dataclass(frozen=True)
class FileEvidence:
    path: str            # workspace-relative, normalized
    last_hash: str       # last observed content hash; "" when the observation carried no hash
    last_tool: str       # tool of the last observation touching the file
    last_revision: int   # workspace_revision of that observation

@dataclass(frozen=True)
class CheckRecord:       # fed by R1 once present; empty before
    name: str
    last_verdict: str        # PASS | FAIL | INFRA_FAIL
    runs: int
    last_returncode: int | None   # exit code of the last run; None for INFRA_FAIL
    last_error: str          # redacted, tail-bounded; "" when none

@dataclass(frozen=True)
class AttemptRecord:
    tool_call_id: str
    path: str
    status: str              # ToolStatus value of the failed edit
    error: str               # redact_text-ed, 300-char cap

@dataclass
class EvidenceLedger:
    task_id: str
    worker_id: str
    file_evidence: dict[str, FileEvidence] = field(default_factory=dict)   # insertion-ordered
    checks: dict[str, CheckRecord] = field(default_factory=dict)           # insertion-ordered
    failed_attempts: tuple[AttemptRecord, ...] = ()
    hypothesis: str = ""
    next_questions: tuple[str, ...] = ()
    attempt_summary: str = ""

    def record(self, observation: Observation) -> None: ...   # the only mutation entry point
    def set_model_fields(self, hypothesis, next_questions, attempt_summary) -> None: ...
    def to_payload(self) -> dict[str, Any]: ...               # deterministic, frame-free
```

**Deterministic derivation rules** (all inside `record`, called from `_record` — agent.py:331-344 — so chunk observations are included too):

1. For each path in `observation.source_paths ∪ observation.file_hashes.keys()`: upsert `FileEvidence` with `last_hash = observation.file_hashes.get(path, "")`, `last_tool = observation.tool`, `last_revision = observation.workspace_revision`. Last write wins by observation order; insertion order (first-seen) is preserved and is the eviction order — over `MAX_LEDGER_FILES`, the oldest-inserted entry is dropped. Deterministic because observation order is the only input.
2. `observation.tool == "run_checks"` and `isinstance(observation.content, Mapping)` with a `"checks"` list: for each entry, upsert `CheckRecord` (last verdict wins; `runs` accumulates).
3. `observation.tool == "edit_file"` and (`not observation.complete` or status in the blocking set): append `AttemptRecord`; keep the last `MAX_LEDGER_ATTEMPTS`.
4. Everything else is ignored. **The ledger is never written from model claims except via `set_model_fields`.**

`set_model_fields` applies bounds + `redact_text` at ingestion: `hypothesis`/`attempt_summary` truncated to their caps with the marker; `next_questions` keeps the first 5 items, each redacted and capped. Called by AgentLoop only for `batch_ready` decisions.

**Serialization.** `to_payload()` excludes `task_id`/`worker_id` (frame metadata, not evidence — this is what makes R2 acceptance 1's "two loops ⇒ byte-identical ledgers" true even across worker ids) and emits plain dicts with sorted file/check keys in `canonical_json` form (domain.py:19-20). Caps applied before serialization, so payload size is bounded: ≤ 50 file entries + ≤ ~10 check records + 8 attempts + three bounded strings.

**Injection into `state`.** AgentLoop sets `state["evidence_ledger"] = ledger.to_payload()` at state construction (agent.py:190-196) and refreshes it immediately after each `_record`, so every model turn sees current evidence. Cost: one bounded dict rebuild per turn.

### 2.3 `batch_ready` model-contributed fields (R2)

`ModelDecision.from_mapping` (models.py:100-104) for `kind == "batch_ready"` only:

```python
hypothesis: str | None        # absent/None ⇒ "" ; non-str ⇒ ModelProtocolError
next_questions: tuple[str, ...] | None   # list of str ⇒ tuple; anything else ⇒ ModelProtocolError
attempt_summary: str | None
```

Defaults keep every existing decision valid (backwards compatible). These fields ride the existing `reason`-style path: they are *not* on `review_required` (requirements: "batch_ready (and only batch_ready)"). Honest visibility note (kept from the PRD): `batch_ready` terminates the loop — every branch at agent.py:293-303 returns — so the same worker never reads these fields back from `state`; their consumers are the checkpoint ledger and the re-plan injection path. Mid-loop value comes exclusively from the deterministic ledger (§2.2). The design adds **no** same-worker visibility claim and no test asserts one.

### 2.4 Checkpoint persistence + re-plan injection (R2) — the named wiring point

**Persist.** `_run_worker` (orchestrator.py:316) extends the checkpoint payload (orchestrator.py:334):

```python
{"kind": "worker",                    # new; enables cross-run filtering (old rows lack it ⇒ ignored)
 "batch_id": ..., "worker_id": ..., "proposal_present": ..., "review_required": ...,
 "evidence_ledger": result.ledger}    # AgentResult.ledger: to_payload() at every return path
```

`AgentResult` (agent.py:126-134) gains appended defaulted fields `ledger: Mapping[str, Any] | None = None` (and `model_reason: str | None = None` for R6) — positional constructions elsewhere stay valid. AgentLoop sets `ledger` in `_review` and on the `batch_ready` success return; `None` only for the pre-loop review paths (duplicate issues, skill errors) where no ledger exists.

**Cross-run read (new store API).**

```python
# store.py
def latest_worker_ledger(self, run_id: str) -> dict[str, Any] | None:
    """Latest 'kind=worker' checkpoint payload for a run, or None. Read-only; no lifecycle guard needed
    because checkpoints are immutable rows."""
```

Implementation: select all checkpoint rows for `run_id`, parse payloads in Python, filter `payload.get("kind") == "worker"`, return the newest by `created_at` (then `rowid`). SQL-side JSON filtering is deliberately avoided (SQLite JSON1 is an optional compile-time feature — not portable across the stdlib-only deployment targets).

**Wiring, pinned to named functions (R2 acceptance 4):**

1. `resume()` output (orchestrator.py:685) gains `"replan_of_run_id": run_id` — the prior run id the caller should pass when opening the replacement run (R2 acceptance 5). Always present; existing keys unchanged.
2. `RepairOrchestrator.run(payload, *, cli_budget_overrides=None, replan_of_run_id: str | None = None)` — persisted into the run payload at `create_run` time (orchestrator.py:157-165); value validated against the store's run-id safety pattern; `None` ⇒ today's behavior exactly.
3. `_run_normalized` loads once, right after `create_run`:
   - `replan_of_run_id` absent ⇒ payload markers `{"prior_ledger_loaded": false, "prior_ledger_reason": "not_provided"}`, no injection.
   - present but store lookup returns `None` ⇒ `{"prior_ledger_loaded": false, "prior_ledger_reason": "prior_run_not_found" | "no_worker_checkpoint"}` (`no_worker_checkpoint` covers legacy runs whose checkpoints lack `kind`/ledger).
   - found ⇒ `{"prior_ledger_loaded": true, "prior_ledger_source_run_id": replan_of_run_id}` and the payload dict is passed down to every `_run_worker` call in this run (all workers of a re-plan see the same prior evidence; it is read-only for all of them).
   - unreadable payload (JSON/shape error) ⇒ `prior_ledger_loaded: false`, reason `"ledger_unreadable"` — the failure mode is recorded, never silently swallowed (fail-closed honesty; R2 acceptance 4b).
4. `_run_worker(..., prior_ledger: Mapping | None = None)` passes `prior_attempt_evidence=prior_ledger` into `AgentLoop`.
5. `AgentLoop.__init__(..., prior_attempt_evidence=None)`: when set, `state["prior_attempt_evidence"] = {"from_run_id": <prior run id>, "ledger": <deep copy via canonical_json round-trip>}`. The new worker's `EvidenceLedger` is a separate object; no code path writes into the injected copy (unit-tested by mutating the new ledger and asserting the injected dict is unchanged).

`new_attempt_id` stays bookkeeping and is deliberately not the linkage key — runs, not attempts, are what the store and CLI operate on. Workflow-level re-plan only; no per-model-call resume (方案 §3.1 scope, restated as a limit).

### 2.5 R2 boundary semantics (ToolStatus/complete impact)

| Event | Ledger effect | Blocking impact |
|---|---|---|
| Any observation (incl. `NOT_EXECUTED` chunk leftovers) | file evidence upserted for its paths | unchanged from today |
| `run_checks` verdicts (R1) | `CheckRecord` upsert | none (verdict path is non-blocking per §1.5) |
| Failed edit (`VERSION_CHANGED`, `EMPTY`, `AMBIGUOUS`, `ERROR`, …) | `AttemptRecord` appended | unchanged (already blocking per `_record`) |
| `batch_ready` with over-long/secret-bearing fields | redacted+truncated at ingestion, before ledger/checkpoint/injection (R2 acceptance 3) | none |
| Prior ledger missing/unreadable | cold start, marker recorded | none |

### 2.6 In-window observation deduplication (R3)

**Scope.** Window = `observations[cutoff:]` in `_prompt_observations` (agent.py:419-426), `max_recent_observations` default 10 (config.py:53-54). v1 dedups **`read_file` only** — the dominant token cost, and the only tool whose content carries a single well-defined `(path, range, content_hash)` triple. `list_symbols` (path + content_hash, no range) is a trivial follow-up; `search_code` aggregates many files under one response with no single hash and stays out. Ceiling documented here and in the tool description.

**Eligibility (all must hold, per occurrence):** `tool == "read_file"`, `status == ToolStatus.OK`, `complete is True`, `content` is a Mapping carrying `path`, `start_line`, `end_line`, `content_hash`, and a str `text`. Dedup key: `(path, start_line, end_line, content_hash)`. Anything less than byte-identical — different hash (file changed between reads), different range, `TRUNCATED`/`PARTIAL`/`EMPTY`/any non-OK status — is never deduplicated.

**Mechanism (inside `_prompt_observations`, after windowing, before per-item char trimming).** Walk the window in order; for each key with ≥ 2 eligible occurrences, the **first** occurrence keeps its full primitive; every later occurrence is emitted as a self-describing reference:

```jsonc
{
  "tool_call_id": "<later occurrence's id>",   // keeps the model's call→result mapping resolvable
  "tool": "read_file",
  "status": "OK",
  "observation_ref": {
    "path": "src/audio/manager.cpp",
    "start_line": 1, "end_line": 30,
    "content_hash": "sha256…",
    "replays_tool_call_id": "<first occurrence's id>"
  }
}
```

Chain-free: all references point at the retained first occurrence (single hop). Reference items are tiny and exempt from the `max_observation_chars` trim (agent.py:422-425), which continues to apply to the remaining full-content items exactly as today. Three or more identical reads: one full copy, N−1 references. The model can always re-read explicitly; tools are unchanged and still return full content (`_replay_read`, source.py:136-154, untouched).

**Switch.** `Config.dedup_observations_enabled: bool = False` → `AgentLoop(dedup_recent_observations=...)` wired in `_run_worker` (orchestrator.py:330). Default off because it changes prompt semantics (protocol the model must understand) — the same reasoning that keeps `chunking_enabled` default-off (config.py:47). It is also the direct on/off mechanism R3 acceptance 4 and the benchmark `dedup` mode require. References are computed fresh per prompt; nothing is persisted (rollback = flip the switch; no state migration).

**Failure/boundary semantics:** dedup is a prompt-construction concern only — no `Observation`, `ToolStatus`, or `complete` value changes; `_record`/blocking behavior untouched; evidence freshness untouched (both occurrences still exist in the observation list for ledger derivation and trace recording).

### 2.7 Compatibility & migration (G2)

- Old persisted decisions parse unchanged (new `batch_ready` fields are optional with defaults); old worker checkpoints (no `kind`, no ledger) are skipped by `latest_worker_ledger` ⇒ legitimate cold start, marker explains why.
- Old run payloads lack `replan_of_run_id`/`prior_ledger_*` — all consumers use `.get` with defaults (the store payload is a free-form dict, store.py:231-238).
- `state` gains two keys (`evidence_ledger`, `prior_attempt_evidence`); `ScriptedModel` and `OpenAICompatibleModel` both serialize `state` opaquely (models.py:180) — no schema change; the ledger is bounded so the token cost is bounded.
- `TaskStateMemory` remains importable with its current shape (its dead `current_hypothesis` field, memory.py:24-36, is superseded, not deleted — one-round deprecation keeps any external import stable).
- Rollback: `evidence_ledger_enabled=False` + `dedup_observations_enabled=False` + not passing `replan_of_run_id` reproduces today's behavior with no data migration; checkpoints written by new code remain readable by old code only down to the extra payload keys (additive JSON).

### 2.8 Deliberate trade-offs & ceilings (G2)

- **Ledger caps vs completeness:** 50 files / 8 attempts / last-verdict-per-check. A worker touching >50 files loses the oldest first-seen entries; the raw observations remain authoritative. Caps are what keep R2 from cancelling R3's savings (PRD risk 6).
- **Stale prior evidence:** the injected ledger's hashes refer to the *prior* run's tree; the new worker re-verifies via its own observations (hash-carrying by construction). Injection is labeled `prior_attempt_evidence` and is never merged into the new ledger.
- **Injection depends on the caller:** if an integration forgets `replan_of_run_id`, cold start silently persists — mitigated exactly as the PRD specifies: named wiring point (`_run_worker`/`_run_normalized`), orchestrator-level test, and the observable `prior_ledger_loaded: false` marker instead of an assumption.
- **Determinism excludes frame fields:** `to_payload()` dropping `task_id`/`worker_id` is a design choice to make acceptance 1 crisp; the checkpoint payload separately records `worker_id` at the checkpoint level (orchestrator.py:334) so attribution is not lost.
- **v1 dedup is read_file-only:** over-lapping ranges and aggregate tools are left as future work (§2.6).

### 2.9 Test plan (acceptance → test)

| Acceptance | Test (new files unless noted) |
|---|---|
| R2-1 determinism | Two `AgentLoop`s (different worker ids) fed identical observation sequences (incl. a `run_checks` verdict and a failed edit) → `canonical_json(ledger.to_payload())` byte-identical |
| R2-2 batch_ready fields | Scripted `batch_ready` with the three fields → checkpoint payload contains them bounded+redacted; decision without them ⇒ payload identical to today's shape; **no** same-worker `state` visibility asserted |
| R2-3 redaction/bounds | Fields carrying secret-shaped tokens (`token=…`) and >cap lengths → redacted/truncated before ledger, checkpoint, and injected state (existing secret patterns, domain.py:445-452) |
| R2-4a injection unit | Helper injects prior ledger read-only; mutating the new ledger does not alter the injected copy |
| R2-4b injection orchestrator | Two `RepairOrchestrator` runs (store shared): second with `replan_of_run_id` → worker `state["prior_attempt_evidence"]["ledger"]` equals first run's checkpoint ledger; with key absent / prior run missing / payload unreadable → cold start + `prior_ledger_loaded: false` + reason |
| R2-5 resume linkage | `resume()` on a REVIEW_REQUIRED run returns `replan_of_run_id`; existing resume flows unchanged (full suite) |
| R3-1 dedup basic | Read identical `(path, range)` twice in-window → `_prompt_observations` output has full text once + one reference resolving to the first `tool_call_id` |
| R3-2 changed file | Re-read with different `content_hash` → never deduplicated |
| R3-3 incomplete | Either occurrence `TRUNCATED`/`PARTIAL` → never deduplicated |
| R3-4 token comparison | `py -3.13 scripts/benchmark.py dedup`: on vs off over the same scripted sequence — estimate decreases, repair outcomes identical (asserted in `tests/test_benchmark.py` as a unit test on the aggregation, plus the CLI mode for manual runs) |

---

## 3. G3 — Semantics & evaluation (R4a, R4b stretch, R5a, R5b, R6, R7)

### 3.1 Files touched

| File | Change |
|------|--------|
| `src/repair_agent/tools/symbols.py` | `member_variable` kind, namespace compounding, `overload_index` (R4a). |
| `src/repair_agent/tools/source.py` | `_symbol_payload` emits `overload_index`; tool description updated. |
| `src/repair_agent/tools/chunking.py` | R4b only: remove `find_definition`/`find_references` from `READ_ONLY_TOOLS` (chunking.py:35). |
| `src/repair_agent/tools/clangd.py` | **New, R4b only.** LSP-over-stdio adapter. |
| `scripts/benchmark.py` | `suite` mode (three arms), `dedup` mode, suite aggregation functions. |
| `tests/test_progressive_navigation.py` | R4a fixture/expectation extensions. |
| `tests/test_clangd_adapter.py`, `tests/test_model_smoke.py` | **New** (R4b, R5b). |
| `src/repair_agent/agent.py`, `src/repair_agent/orchestrator.py`, `src/repair_agent/reporting.py` | R6 reason passthrough. |
| `README.md`, `docs/security-review.md`, `docs/implementation-status.md`, `docs/BENCHMARK.md` (**new**) | R7. |

### 3.2 R4a — lexical scanner enhancements (field-level)

`SymbolDecl` (symbols.py:34-39) gains one appended defaulted field — additive, all existing constructors valid:

```python
@dataclass(frozen=True)
class SymbolDecl:
    type: str                # existing kinds + "member_variable"
    name: str                # unchanged rules + namespace-qualified names
    signature: str
    start_line: int
    end_line: int
    overload_index: int = 0  # 1-based ordinal among same (type, name) decls in file scan order; 0 = unique
```

1. **Member variables.** `_scan` gains an `enclosing_kind` parameter (`None | "namespace" | "class"`). Inside a class/struct body, a new `_try_member_variable` attempt matches `Type name ['[' … ']' | '…'] ['=' initializer] ';'` — identifier not in `_TYPE_KEYWORDS`/`_CONTROL_KEYWORDS`, **no `(` between the identifier and the terminating `;`** (so member functions and calls are excluded), not preceded by `~`. Emits `SymbolDecl(type="member_variable", name=f"{enclosing}::{ident}", …)`. Function-local variables are not captured (the attempt only runs when `enclosing_kind == "class"`).
2. **Namespace qualification.** At symbols.py:400 the propagation becomes: `class`/`struct` ⇒ nested enclosing = `compound(enclosing_ns, decl.name)` and `enclosing_kind="class"`; `namespace` ⇒ nested enclosing = `compound(enclosing_ns, decl.name)` and `enclosing_kind="namespace"`, where `compound` joins existing prefix + name with `::` (so `namespace a { namespace b { … } }` yields `a::b::` prefixes, mirroring the class mechanism). Contained free functions get qualified names with their existing kinds (`function`/`helper`); out-of-class `Class::method` definitions keep working via `_qualified_name_backward` (symbols.py:197-219). `_probe_symbol_confidence`'s `endswith("::" + symbol)` check (memory.py:274) is prefix-compatible by construction.
3. **Overload disambiguation.** Post-pass in `scan_symbols` (scan order is left-to-right lexical, hence deterministic): for each `(type, name)` group with n > 1 decls, assign `overload_index = 1..n`; unique names keep `0`. Names are **not** mutated (a `name#2` scheme would break `endswith("::"+symbol)` consumers); the signature field already distinguishes overloads and stays.
4. **Ceilings (module docstring, extending symbols.py:3-13):** macros and typedefs can create false `member_variable` positives/negatives; comma-declarator lines (`int a, b;`) capture only the first name; initializers containing `(` cause a conservative skip; template-heavy member declarations may be mis-bounded; template recognition stays best-effort. The `list_symbols` tool description (executor.py:102) mentions members/namespaces and keeps "heuristic lexical outline … not semantic navigation".
5. **Consumers:** `_symbol_payload` (source.py:187-220) emits `"overload_index": item.overload_index` and stays under `MAX_SYMBOL_DECLS` + char budget unchanged (new kinds count toward the same caps — R4a acceptance 5); `SymbolCache` replays `SymbolDecl` tuples opaquely (context.py:125-169); the confidence probe and `compute_recall_stats`/`run_recall_case` (benchmark.py:377-413) work unchanged. The recall fixture gains `src/audio/plugin.h` and one `frames_` (member_variable) case in the same change, since namespace prefixing changes emitted names — fixture and expectations land together (R4a risk note).

### 3.3 R4b — clangd adapter (STRETCH — cut-eligible by design)

> **Cut notice:** this subsection is self-contained. If review cuts R4b, none of G3's other items change: `find_definition`/`find_references` stay honest `UNSUPPORTED` (executor.py:103-104, source.py:435-436), R4a lands alone, and the R7 documentation omits the clangd sections. Pre-agreed kill criterion (requirements R4b): a deterministic, deadline-bounded, stdlib-only handshake that cannot be demonstrated within the round cuts the requirement; a half-working adapter must not ship.

**Configuration.**

```python
@dataclass(frozen=True)
class ClangdConfig:
    binary: str                    # path or name resolved via shutil.which at executor construction
    compile_commands_dir: str      # workspace-relative location of compile_commands.json
    timeout_seconds: float = 10.0  # per-request and handshake bound
# Config.clangd: ClangdConfig | None = None
```

Absent ⇒ both tools keep today's exact `UNSUPPORTED` behavior (acceptance 1); the `Config` field is the entire activation surface.

**Adapter (`tools/clangd.py`).**

```python
class ClangdNavigation:
    def __init__(self, workspace, config: ClangdConfig, *, max_output_chars: int, max_locations: int = 50): ...
    def find_definition(self, symbol: str, *, deadline: float | None) -> handler-6-tuple: ...
    def find_references(self, symbol: str, *, deadline: float | None) -> handler-6-tuple: ...
    def close(self) -> None: ...          # explicit teardown; also registered via try/finally in the executor owner
```

- **Process:** one `clangd` per worker workspace, spawned lazily on first navigation call with fixed argv `[binary, f"--compile-commands-dir={dir}"]`, `cwd=workspace.root`, `shell=False`, stdin/stdout pipes (ripgrep precedent, source.py:325-361). Lifecycle owned by the worker's `SourceTools` instance; `close()` reaps it; worktree removal (`GitWorktreeManager.remove`) plus process exit bound the leak window.
- **Framing:** stdlib `Content-Length`-framed JSON-RPC over the pipes; a daemon reader thread pushes parsed frames into a `queue.Queue`; every read is `queue.get(timeout=min(config.timeout_seconds, remaining_deadline))` — a hung server yields a bounded error observation, never a hang (deadline pattern of `_collect_rg`, source.py:346-350).
- **Handshake:** `initialize` → `initialized` → `textDocument/definition|references` with `"textDocument/definition": {"scheme": "file"}` capability hint; handshake and each request are individually deadline-bounded; failures ⇒ `(ERROR, None, (), {}, False, "clangd: <bounded reason>")`.
- **Results:** locations capped at `max_locations`; paths normalized to workspace-relative via `workspace.resolve` (escaping/protected paths dropped, `_parse_rg_output` precedent, source.py:388-392); each retained path paired with its content hash via `workspace.hash_paths`; text fields pass `redact_text`. Output shape:

```jsonc
{"symbol": "AudioManager::process", "locations": [{"path": "src/audio/manager.cpp", "line": 21, "character": 6, "content_hash": "…"}],
 "truncated": false, "server": "clangd"}
```

**Chunk exclusion (real code change).** `BoundaryDetector.READ_ONLY_TOOLS` drops `find_definition`/`find_references` (chunking.py:35) and their `ToolSpec`s get `allowed_in_chunk=False` (executor.py:103-104). Both tools are read-only, but server lifecycle state makes chunk-replay semantics unsafe — v1 excludes them deliberately; recorded in README "Deliberate boundaries".

### 3.4 R5a — benchmark `suite` mode (field-level)

`scripts/benchmark.py` gains two subcommands (CLI shape of benchmark.py:435-448):

- **`suite`** — three arms over one shared scripted decision sequence on the existing context fixture (benchmark.py:156-207), extended: a configured `run_checks` self-check (`[sys.executable, "-c", "import sys; sys.exit(0)"]` → deterministic PASS) enters the sequence and fixture once R1 lands; **before R1 the field is reported as `"check_runs": null`** — reported absent, never faked (the sequence only contains `run_checks` when the tool exists).

| Arm | `cache` | ledger injection | Isolates |
|-----|---------|------------------|----------|
| `baseline` | off | off (`evidence_ledger_enabled=False`) | raw loop |
| `cache` | on | off | ContextCache effect |
| `ledger` | on | on | Task-Memory injection effect |

Honest labeling carried from requirements §6.9: these arms isolate mechanism contributions and are **not** the 方案 §19 A/B (whose third arm is the full HEAL bundle); navigation is common to all arms and chunking (default-off) is not an arm. `AgentLoop` needs one new constructor switch `evidence_ledger_enabled: bool = True` (also G2's rollback switch) — this is the only production-code change R5a requires.

Per-arm report fields: `model_calls`, `tool_calls_by_name`, `check_runs` (null pre-R1), `physical_evidence_reads` / `distinct_files_read` / `repeated_physical_reads` (`_EvidenceReadCounter`, benchmark.py:217-258), `logical_cache_hits` + `cache_stats` (`ContextCache.stats()`, context.py:180-192), `wall_seconds` (reported, never a correctness signal), `repair_completed`/`review_required`, and:

```jsonc
"context_token_estimate_bytes": 48211,
"context_token_estimate_basis": "sum of UTF-8 bytes of each serialized prompt payload (task+state+observations), same construction as models.py _prompt_token_reserve; an ESTIMATE — stdlib-only, no tokenizer; never quote as measured tokens"
```

The estimate is captured by a thin `_CountingModel(ModelAdapter)` wrapper around `ScriptedModel` that measures `len(json.dumps({...prompt payload...}, ensure_ascii=False, separators=(",",":")).encode("utf-8"))` per call — the `_prompt_token_reserve` pattern (models.py:305-309) applied as a measurement rather than a reserve.

- Aggregation is a pure function `compute_suite_comparison(arms: dict) -> dict` (per-arm deltas, `model_calls_equal`, `both_repairs_completed`, `estimate_decreases_cache_vs_baseline`) — unit-testable in the `compute_recall_stats` style (benchmark.py:377-389). Determinism: fixtures are static, the decision sequence fixed, no randomness; `wall_seconds` is the only permitted run-to-run variance (R5a acceptance 1).

- **`dedup`** — the R3 acceptance 4 harness: same fixture/sequence, `dedup_recent_observations` on vs off, reporting the context-token estimate and repair outcomes for both settings. Separate from `suite` so R5a's arms stay exactly three.

**`docs/BENCHMARK.md` (new):** metric definitions (verbatim from `_METRIC_DEFINITIONS` plus the new `context_token_estimate_*`, `check_runs`, `repeated_physical_reads`), arm definitions incl. the §6.9 deviation note, reproduction commands (`py -3.13 scripts/benchmark.py suite|dedup|context|recall`), the estimate labeling, and the standing notCovered scope (real 800-warning dataset; 方案 §19 real-data A/B; measured tokens; real clangd) — field names must match script output exactly (R5a acceptance 3).

### 3.5 R5b — model smoke tests (`tests/test_model_smoke.py`)

- **Always-runnable stub.** Stdlib `http.server` on `127.0.0.1` (ephemeral port, daemon thread) serving scripted OpenAI-compatible responses; drives `OpenAICompatibleModel.decide` (models.py:165) through three paths: (a) success — tool-call response with `usage` parsed into `ModelUsage(reported=True)`; (b) HTTP 500 → `ModelError` category `MODEL_HTTP`, `retryable=True`, `usage_unknown=True` (models.py:209-218); (c) malformed (non-JSON / non-object / missing usage) body → `ModelProtocolError` with the exact message contracts of models.py:224-245. No external service, fully deterministic.
- **Env-gated real smoke.** `@unittest.skipUnless(os.environ.get("HEAL_SMOKE_ENDPOINT") and os.environ.get("HEAL_SMOKE_API_KEY_ENV") and os.environ.get("HEAL_SMOKE_MODEL_ID"), "real-endpoint smoke test requires HEAL_SMOKE_* environment")` — one minimal `decide_with_deadline` against the configured endpoint; the credential is read only via the named env-var indirection (`api_key_env`, models.py:172-174); no credential value is logged, asserted on, or embedded in failure messages (any surfaced payload passes `redact_text` first); a skip is reported as skipped by unittest, never as a pass (R5b acceptance 1, 3).
- Smoke-run results are documentation, not gates: nothing in CI depends on the env-gated test executing.

### 3.6 R6 — model review-reason passthrough (field-level)

- **Contract:** `AgentResult` gains appended field `model_reason: str | None = None` (declared with the G2 contract change, §2.4). `AgentLoop._review` (agent.py:429-432) gains `model_reason: str | None = None` and applies the single choke-point transform: `redact_text(str(value))` then hard cap `MAX_MODEL_REASON_CHARS = 1_000` with the `"…[truncated]"` marker when cut.
- **Call sites:** `review_required` (agent.py:305-306) passes `model_reason=decision.reason` while the durable reason remains the fixed `"model requested human review"`; `batch_ready`-failed-proposal (agent.py:300-302) passes it likewise. Budget/protocol/exception paths have no model reason (none was supplied) — `model_reason=None`. `_safe_review_reason` (agent.py:49-89) is **byte-identically unchanged**; the whitelist fallback remains the durable reason (R6 acceptance 3).
- **Artifacts:** `_worker_result_artifact_payload` (orchestrator.py:95-99) stops hard-stripping: `payload["reason"] = result.reason` (whitelist-safe by construction) and `payload["model_reason"] = result.model_reason`; `sanitize` re-redacts everything downstream (runtime/trace.py) — defense in depth, not the primary control.
- **Run report:** `_run_normalized` collects `model_reasons` alongside `reasons` (orchestrator.py:212) and persists `"worker_model_reasons"` in the BATCH_REVIEW transition payload; `_write_report` payload gains the key (orchestrator.py:902-921) and `ReportWriter._markdown` gains a "Worker model reasons (diagnostic)" section in its section list (reporting.py:51-64).
- **Never evidence:** `model_reason` does not appear in `BatchProposal` fields, is not an input to `PatchScopeGuard`, action-map classification, suppression gating (agent.py:489-504), validation classification, or candidate identity. It is diagnostic metadata only; exclusion is enforced by construction (no code path reads it except display/serialization).

### 3.7 R7 — documentation

- **README** — new "Positioning vs mainstream coding agents" section: capability claims each carrying a code citation (in-loop checks → `tools/checks.py` + `agent.py` verdict mapping; task memory → `memory.py` `EvidenceLedger` + `orchestrator.py` injection; dedup → `agent.py` `_prompt_observations`; lexical navigation → `tools/symbols.py`; semantic navigation boundary → `executor.py:103-104`), plus an updated "Deliberate boundaries" list that matches post-R1–R4 behavior (checks execute trusted config binaries; worktree not a sandbox; clangd excluded from chunks if R4b lands; UNSUPPORTED when unconfigured; promotion evidence only from post-freeze validation).
- **docs/security-review.md** — new sections: trusted check execution (argv source = config only, prefix caveat, residual arbitrary-binary risk), clangd adapter IPC (if kept: fixed argv, bounded handshake, per-worker lifecycle, redaction), review-reason redaction (`redact_text` + cap + metadata-only), and the restated worktree-is-not-a-sandbox boundary.
- **docs/implementation-status.md** — one entry per landed requirement (R1–R6, R4b marked kept/cut) with the same implemented/notCovered honesty as existing entries.
- Test coverage for R7 is review-level (docs are prose); the PRD's acceptance is the citation rule, checked by reviewer walkthrough, not by a unit test.

### 3.8 G3 boundary semantics (ToolStatus/complete impact)

| Event | Status/complete | Impact |
|---|---|---|
| `list_symbols` with new kinds, over caps | `TRUNCATED`/`complete=False` (existing `_symbol_payload` behavior, source.py:216-217) | unchanged |
| clangd configured, server hangs/deadline | `ERROR`/`complete=False` | blocks per existing rule — a hung semantic backend is an execution-layer failure, honestly surfaced |
| clangd unconfigured | `UNSUPPORTED`/`complete=False` (byte-identical to today) | blocks only if the model calls an unconfigured tool (existing semantic) |
| Model reason redaction/truncation | n/a (metadata) | none — durable reasons byte-identical |
| Benchmark/suite | no runtime status changes | none |

### 3.9 Compatibility & migration (G3)

- R4a changes emitted names (namespace prefixes) — the recall fixture updates **in the same commit**; `SymbolDecl`/symbol payload changes are additive (`overload_index`), old cached entries replay fine (cache entries hold `SymbolDecl` tuples; a stale cache built pre-change lives only within a worker process).
- R4b absent ⇒ zero delta (config field defaults `None`; both tools untouched).
- R6 is additive metadata; consumers tolerate absence (`.get` patterns); persisted old artifacts without `model_reason` parse unchanged.
- R5a/R5b are additive script/test surfaces; no production behavior changes except `AgentLoop.evidence_ledger_enabled` (default True ⇒ current post-R2 behavior).
- Rollback: R4b is independently revertible (the reason it is split); R6 revert restores the hard-strip; R5 modes are opt-in CLI paths.

### 3.10 Deliberate trade-offs & ceilings (G3)

- **Lexical honesty:** member-variable detection accepts documented false positives/negatives over false semantics; the tool keeps saying "heuristic lexical outline".
- **`overload_index` over name mutation:** keeps `endswith("::"+symbol)` consumers and the recall benchmark stable; consumers wanting display disambiguation combine name+index+signature.
- **clangd v1 bounds:** one server per worker, no batching, no diagnostics, `max_locations` cap; cross-platform process management is the round's heaviest risk — hence the kill criterion and the exclusion from chunks.
- **Token estimate:** byte-based, labeled, never a tokenizer count; the three arms share one scripted decision sequence, so the suite measures mechanisms, not model behavior — stated in `docs/BENCHMARK.md`.
- **Smoke test value:** one provider dialect proves reachability and protocol shape, not repair quality; kept out of default CI for cost and determinism.

### 3.11 Test plan (acceptance → test)

| Acceptance | Test |
|---|---|
| R4a-1 members + namespaces | Fixture class with data members → `member_variable` entries at correct lines; `namespace audio { class Engine { … } }` → `audio::Engine`-qualified names |
| R4a-2 overload disambiguation | Two same-name member-function definitions → two entries with distinct `overload_index` (1, 2), same name |
| R4a-3 false-positive guards | Control keywords, calls, prototypes produce nothing; docstring ceilings present (docstring-content assertion) |
| R4a-4 consumers unchanged | `_probe_symbol_confidence` and recall benchmark run deterministically against extended output; recall fixture updated in same change |
| R4a-5 bounded output | `list_symbols` over a >`MAX_SYMBOL_DECLS` fixture → `TRUNCATED`, count capped |
| R4b-1 unconfigured | Both tools `UNSUPPORTED` exactly as today; full suite unchanged |
| R4b-2 scripted LSP | Fake LSP server (helper script over stdio, stdlib `subprocess`) exercises initialize/definition/references + timeout path → bounded `ERROR` observation, no hang |
| R4b-3 real clangd | Env-gated integration test (skipped unless binary + compile database present) resolves a planted symbol |
| R4b-4 chunk exclusion | Chunk containing the two tools rejected by `BoundaryDetector` with tool-not-eligible reason |
| R5a-1 determinism | `py -3.13 scripts/benchmark.py suite` twice → identical metric values modulo `wall_seconds` (asserted as a unit test over the aggregation; CLI run executed manually per §4) |
| R5a-2 fields + labeling | Report has 3 arms + all fields; `context_token_estimate_basis` present in JSON and `docs/BENCHMARK.md` |
| R5a-3 docs match | Field-name comparison test or documented manual check of `docs/BENCHMARK.md` against script output |
| R5a-4 aggregation units | Pure-function tests for `compute_suite_comparison` (and `compute_dedup_comparison`) |
| R5b-1 default discovery | Full-suite run shows smoke test skipped, stub test passing |
| R5b-2 protocol paths | Stub tests: success / HTTP-error / malformed-response |
| R5b-3 no credentials | Code inspection + redaction reuse; no credential value in any output |
| R6-1 secret redaction | `review_required` with secret-shaped reason → stored `model_reason` redacted |
| R6-2 cap | Over-long reason truncated with explicit marker; clean reason survives verbatim within 1000 chars |
| R6-3 framework reasons byte-identical | Existing reason tests untouched and green (full suite) |
| R6-4 report + artifacts | Run report contains `worker_model_reasons`; worker-result artifact retains `reason` and adds `model_reason` |

---

## 4. Verification executed for this design (this session)

- `py -3.13 -m unittest discover -s tests -p "test_*.py"` → **Ran 109 tests … OK** (73.6 s) — the baseline every group must preserve.
- `grep -rn "max_search_rounds" src/ scripts/` → exactly the eleven budget-key sites cited by requirements §3 R1 (agent.py:119,321,368,385; config.py:79; domain.py:258,261; orchestrator.py:352,683; planning.py:106,126), plus the design-identified usage-side sibling `store.py:202` (`fields` tuple, found by reading `update_worker_budget`).
- Read-through verification of every PRD code citation listed in §0 — all confirmed against the current tree; no contradiction found.

## 5. Implementation order, commit plan, and review gates

1. **Design doc** (this file) — separate commit.
2. **G1/R1**: config → domain/agent budget plumbing → `tools/checks.py` → executor/agent wiring → `tests/test_run_checks.py`; grep audit recorded in the PR description.
3. **G2/R2**: `memory.py` ledger → models.py fields → agent.py wiring → store/orchestrator injection → `tests/test_task_memory.py`.
4. **G2/R3**: `_prompt_observations` dedup + switch → benchmark `dedup` mode → `tests/test_prompt_dedup.py`.
5. **G3/R4a**: scanner → payload/description → recall fixture → `tests/test_progressive_navigation.py` extensions.
6. **G3/R5a**: benchmark `suite` + `docs/BENCHMARK.md` → `tests/test_benchmark.py` extensions.
7. **R4b gate (review decision: keep or cut)** — cut ⇒ skip 7 and note it in implementation-status.
8. **G3/R6**: reason passthrough (small; touches the `AgentResult` contract field declared in step 3).
9. **G3/R7**: README/security-review/implementation-status, documenting what actually landed.

Suite green after every step (109-test baseline; net additions expected, zero regressions tolerated).

## 6. Deviations and escalations from this design

1. **Budget audit widened (vs PRD's "eleven sites"):** the completeness audit covers 13 sites — the 11 `max_search_rounds`-mirroring budget-key sites plus `AgentUsage.check_runs` (new counter) and `store.py:202` `update_worker_budget`'s usage-field tuple, which the budget-key grep pattern structurally cannot find. Not a contradiction — a tightening.
2. **Chunk-path budget gate for `run_checks` deliberately absent** (§1.7 site 6): unreachable because the boundary detector rejects non-read-only tools before the pre-gate runs. Documented so the audit reads as designed, not omissive.
3. **Integrity mismatch status pinned to `ERROR`** where the PRD says only `complete=False`: `ERROR` keeps the blocking review reason self-explanatory (`run_checks/ERROR`) and is the existing execution-layer-failure status; `complete=False` does the blocking either way.
4. **R3 v1 scope narrowed to `read_file`** where the PRD's wording ("same tool, path, hash, range") admits broader tools: `search_code`/`list_symbols` outputs lack the exact `(path, range, hash)` triple; extension is specified as a follow-up ceiling (§2.6).
5. **`latest_worker_ledger` filters in Python, not SQL JSON1:** stdlib-only portability (§2.4).
6. **Two `AgentResult` contract fields (`ledger`, `model_reason`) declared once** in G2's contract change to avoid two breaking-shape edits to a frozen dataclass (§2.4, §3.6).

No PRD/requirements-vs-code contradiction requiring escalation was found; the items above are tightenings and pins, not scope changes.
