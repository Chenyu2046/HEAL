# HEAL Requirements — Agent Harness Reliability Round

Status: proposed for review (requirements analysis of the leader's gap analysis and feature draft)
Branch baseline: `codex/harman-agent-harness-reliability` at `51075a2`
Test baseline: `py -3.13 -m unittest discover -s tests -p "test_*.py"` → **Ran 109 tests … OK** (executed in this session, 78.3 s). No requirement in this document may regress that suite.

---

## 1. Background and Goals

HEAL (Harman Code Quality Agent) is a pure-stdlib C/C++ static-alert repair pipeline (`repair_agent` package, src layout, Python ≥ 3.11, zero third-party dependencies). It normalizes findings, plans affinity batches, runs bounded workers in isolated Git worktrees, integrates proposals serially, freezes candidates bound to tree hashes, and classifies post-freeze validation through an independent validator.

The design document of record is `C:/Users/26561/Desktop/检索与分层记忆方案.md` (referenced below as 方案 §N, matching the convention already used in `docs/implementation-status.md`).

Three invariants are non-negotiable and every requirement below is checked against them (§5):

1. **Zero dependency** — external capability is either an external-binary subprocess with fixed argv, or an explicit `UNSUPPORTED`/`NOT_CONFIGURED` boundary.
2. **Fail-closed** — the system never fabricates a PASS; incomplete or untrusted evidence is marked as such and blocks the automated path.
3. **Identity binding** — candidates, approvals, validations, and evidence are bound to tree hash / commit; identity mismatches invalidate rather than inherit.

### 1.1 Why this round

The project is used as an interview showcase. The leader's gap analysis (objective comparison against mainstream coding agents) concluded that several claims in the narrative are not yet real in the repository. This round's goal is to **make the narrative promises real in code and close the gaps that can be closed inside this repository** — not to add new AI modules.

### 1.2 Verified current-state evidence

Each gap below was verified against the current source in this session (the reader can re-check every citation):

| # | Gap | Evidence |
|---|-----|----------|
| ① | Worker loop has no execution feedback (no compile/test) | The tool registry offers only read/search/outline/edit/diff tools (`src/repair_agent/tools/executor.py:100-108`); the loop (`src/repair_agent/agent.py:198-329`) can call nothing that builds or runs tests. Trusted command execution exists only **post-freeze** (`validate_local`, `src/repair_agent/orchestrator.py:747-763`), never inside the loop. |
| ② | Task Memory (方案 §3.1) not real | `TaskStateMemory` has a `current_hypothesis` field that nothing ever writes (`src/repair_agent/memory.py:24-36`); the loop creates it and never mutates anything but observations (`src/repair_agent/agent.py:181`, only mutation is `memory.add` inside `_record`, `src/repair_agent/agent.py:331-333`); the model-facing `state` dict carries no hypothesis/evidence (`src/repair_agent/agent.py:190-196`); checkpoints store only two booleans (`src/repair_agent/orchestrator.py:334`); after CODE_FAIL, `receive_ci` records a `new_attempt_id` (`src/repair_agent/orchestrator.py:565-567`) but nothing feeds prior-attempt evidence to a re-run — re-planning cold-starts. |
| ③ | No in-window content deduplication | The ContextCache removes duplicate *physical reads* but a cache hit still replays full content into the observation (`src/repair_agent/tools/source.py:136-154`); each prompt re-serializes the last `max_recent_observations` observations in full (`src/repair_agent/agent.py:402-427`), so repeated identical reads inside the window pay their tokens again in every subsequent prompt. |
| ④ | Semantic navigation absent; lexical scanner gaps | `find_definition`/`find_references` are hard-wired to `UNSUPPORTED` (`src/repair_agent/tools/executor.py:103-104`, `src/repair_agent/tools/source.py:435-436`). Probing `scan_symbols` in this session confirmed: member variables are not captured, and a function inside `namespace audio` is emitted as bare `function|helper` (namespace names are not propagated as enclosing prefixes — `src/repair_agent/tools/symbols.py:400` propagates only `class`/`struct`). Overload *definitions* already emit separate entries (same name, distinct ranges). |
| ⑤ | Evaluation absent against reality | `scripts/benchmark.py` is fixture-based with an explicit notCovered scope note (`scripts/benchmark.py:41-45`); no real model endpoint has ever been called from tests (all tests use `ScriptedModel`); no context-token metric, no three-arm comparison, no `docs/BENCHMARK.md`. |
| ⑥ | Model review reason collapsed | A model `review_required` decision's own reason is discarded and replaced by the fixed string `"model requested human review"` (`src/repair_agent/agent.py:306`); `_safe_review_reason` collapses any unrecognized reason to a generic string (`src/repair_agent/agent.py:49-89`); worker-result artifacts strip `reason` entirely (`src/repair_agent/orchestrator.py:98`). |
| ⑦ | README lacks positioning | README (49 lines by `wc -l`) has no "positioning vs mainstream agents" section (`README.md`); `docs/security-review.md` and `docs/implementation-status.md` exist but do not cover this round's features. |

---

## 2. Requirement Index

| ID | Priority | Title | Source in draft | Deviation from draft |
|----|----------|-------|-----------------|----------------------|
| R1 | P0 | `run_checks`: trusted in-loop check execution | F1 | Budget plumbing and integrity binding made explicit (§3 R1) |
| R2 | P0 | Task Memory made real (ledger, hypothesis, checkpoint, CODE_FAIL injection) | F2 | Ledger derivation rules and read-only injection contract made explicit |
| R3 | P1 | In-window observation deduplication | F3 | Scoped to hash-verified identical reads inside the window |
| R4a | P1 | Lexical symbol scanner: member variables, namespace qualification, overload disambiguation | F4 | "Overloads separated" reframed — verified already separate; gap is disambiguation |
| R4b | P1 (stretch, cut-eligible) | clangd LSP-over-stdio adapter for `find_definition`/`find_references` | F4 stretch | Split out as an independently cuttable requirement with a kill criterion |
| R5a | P1 | Benchmark suite: three-arm comparison, token estimate, check counts, `docs/BENCHMARK.md` | F5 | Split; token metric is an explicitly labeled byte-based estimate |
| R5b | P2 | Real-model smoke test (env-gated) + localhost stub test | F5 | Demoted from P1 — acceptance depends on external endpoint availability |
| R6 | P2 | Model review-reason passthrough (redacted, capped) | F6 | Whitelist fallback retained unchanged |
| R7 | P2 | Documentation: positioning, security review, implementation status | F7 | Itemized |

---

## 3. Requirements

### R1 (P0) — `run_checks`: trusted in-loop check execution

**Motivation.** The largest gap vs mainstream test-driven repair agents: the worker cannot compile or run tests, so it proposes patches it has never exercised (evidence ①). Everything needed for a safe version already exists post-freeze in `LocalValidator` (`src/repair_agent/validation/local.py:24-70`) — the requirement is to bring that trust pattern inside the loop without weakening any boundary.

**Functional description.**

- A new `run_checks` tool in the registry. The model can **invoke** a check by name only; it can never define argv. Check argv comes exclusively from configuration (a new config section, e.g. `checks: {name: argv[]}`, loaded and validated with the existing `Config` discipline in `src/repair_agent/config.py`).
- Execution reuses the `LocalValidator` pattern: fixed argv, `subprocess.run(shell=False)` under the workspace root, timeout bounded both per-command and by the remaining wall-clock deadline, stdout/stderr truncated and passed through `redact_text` before becoming observation content.
- Exit-code mapping is deterministic in the tool: `0 → PASS`, non-zero → `FAIL`, timeout/OS error → `INFRA_FAIL`. The agent never parses output text to decide success.
- **Status/complete mapping (explicit, because it decides blocking).** The blocking mechanism is unforgiving: `_record` appends every not-complete or ERROR/PARTIAL/TRUNCATED/… observation to `_blocking_tool_failures` (`src/repair_agent/agent.py:338-342`), that list is only cleared at run start (`src/repair_agent/agent.py:178`), and any entry rejects `batch_ready` (`src/repair_agent/agent.py:294-296`). Therefore: **PASS → `OK`/`complete=True`; FAIL → `OK`/`complete=True` with the verdict in content; INFRA_FAIL → `OK`/`complete=True` with verdict `INFRA_FAIL` in content.** Rationale: `ToolStatus` describes the tool call's execution, the verdict field describes the check's outcome — `OK`/`complete=True` truthfully means "the check executed and this is its complete verdict", the same shape as `search_code` returning `EMPTY`/complete. Blocking is reserved for execution-layer failures: an integrity mismatch yields `complete=False`, and an uncaught handler exception falls into the executor's catch → `ERROR`/`complete=False` (`src/repair_agent/tools/executor.py:140-141`) — both block, fail-closed. Mapping a legitimate FAIL to any blocking status would let the first failing check permanently end every worker in REVIEW_REQUIRED, self-destructing the feature.
- **INFRA_FAIL stance (explicit).** Timeouts and OS errors are **non-blocking** at the worker level. Fail-closed is about never fabricating a PASS: an `INFRA_FAIL` verdict is explicitly not-PASS, is persisted in the ledger and surfaced in proposal review notes, and the post-freeze independent validation (§4.8) remains the only promotion evidence. The alternative — timeout → blocking — was considered and rejected: the blocking list never clears within a worker run, so one flaky build tool would convert every worker into REVIEW_REQUIRED, making R1 strictly worse than having no checks. The counterweight is honesty, not blocking: batch_ready must surface the per-check last verdicts.
- **Output bounding.** The handler pre-trims its own content below the executor's downgrade threshold (arithmetic pre-trim in the style of `_symbol_payload`, `src/repair_agent/tools/source.py:199-214`, against the check at `src/repair_agent/tools/executor.py:142-145`), so a long build log can never downgrade a legitimate verdict to `TRUNCATED`/`complete=False` and thereby block the worker.
- **Integrity binding:** tree hash and tree OID are computed before and after the check using the existing `tree_hash`/`git_tree_oid` (`src/repair_agent/runtime/workspace.py:48-96`). These already enumerate via `git ls-files --exclude-standard`, so build products written to gitignored paths do not perturb identity. Any mismatch is a failed integrity observation with `complete=False` and blocks `batch_ready` through the existing blocking-failure path (`src/repair_agent/agent.py:293-296`).
- **Budget:** a new `max_check_runs` field plumbed at *every* site that manually enumerates its sibling budget keys — verified by repo-wide grep for `max_search_rounds` (eleven sites across six files): `Budget` field + validation (`src/repair_agent/domain.py:258,261`), `AgentUsage.within` (`src/repair_agent/agent.py:119`), the exhausted-reasons list (`src/repair_agent/agent.py:321`), pre-execution enforcement in both the single-call and chunk paths (`src/repair_agent/agent.py:368,385`), config-JSON parsing in `_budget` (`src/repair_agent/config.py:79`), normalizer defaults and CLI-override resolution (`src/repair_agent/planning.py:106,126`), and slot allocation plus resume-remaining (`src/repair_agent/orchestrator.py:352,683`). Missing the config site would make the budget unsettable from JSON; missing the planning site would silently drop it on CLI overrides. Acceptance 5 is therefore a completeness audit over all of these sites, not a spot check. Each check invocation also consumes a normal tool call.
- **Chunking:** `run_checks` is not read-only and is therefore never admitted into action chunks (gate at `src/repair_agent/tools/chunking.py:51`).
- **Sandbox (optional):** an optional trusted command *prefix* (e.g. a container invocation) may be configured and prepended to every check argv. This is convenience, not an OS-level sandbox; the documented boundary "the worktree is an edit isolation mechanism, not a security sandbox" (`README.md:41`) stays.
- **Unconfigured:** with no checks configured the tool returns explicit `UNSUPPORTED` (same shape as `memory_retrieve`, `src/repair_agent/tools/executor.py:161-163`). Every existing test and flow is unchanged.

**Acceptance criteria.**

1. With checks configured, a scripted decision sequence that calls `run_checks` receives an observation whose content includes the deterministic PASS/FAIL/INFRA_FAIL verdict plus truncated, redacted output; exit code is the sole verdict source (unit test with real subprocesses, e.g. a trivial echo/exit-code fixture).
2. **Non-blocking direction (the FAIL contract):** a legitimate FAIL — including one whose raw output exceeds the executor's `max_output_chars` — yields `status=OK`/`complete=True`, never enters `_blocking_tool_failures`, and a subsequent `batch_ready` is accepted; the FAIL verdict appears in the proposal's review notes (unit test).
3. **Blocking direction preserved for execution-layer failures:** a check that creates an untracked, non-gitignored file yields an integrity-failed observation marked `complete=False`, and a subsequent `batch_ready` is rejected with a review reason naming the failure; a check that writes only into gitignored paths does not trip integrity; when identity inputs are unavailable (git failure), the observation is `ERROR`/`complete=False` and blocks (unit tests).
4. **INFRA_FAIL stance:** a timeout (fixture command sleeping past the per-check timeout) and an OS error (configured binary not found) each yield verdict `INFRA_FAIL` with `status=OK`/`complete=True`, do not block `batch_ready`, and are recorded in the ledger and surfaced in proposal review notes (unit test).
5. **Budget completeness audit:** a repo-wide grep audit shows `max_check_runs` handled at every site that enumerates the sibling budget keys (the eleven `max_search_rounds` sites listed in the budget bullet); behaviorally, the budget is settable from config JSON, survives CLI budget overrides, exhaustion produces the budget review reason, and the count appears in slot-allocation splits and `resume()`'s `budget_remaining` (grep audit + unit tests).
6. `run_checks` never appears in an accepted action chunk's executed actions (unit test).
7. With no checks configured, `run_checks` returns `UNSUPPORTED`, `complete=False`, and the 109-test suite still passes unchanged (full suite run).
8. Model-supplied arguments can select only a configured check name; any other name or extra argument is rejected by schema validation before execution (unit test).

**Risks & tradeoffs.** A build inside the worker consumes wall clock against `max_wall_seconds`; the per-check timeout must default well below the task budget. Checks run arbitrary trusted-configured binaries — the security review (R7) must record the residual risk. The model may iterate on FAIL results; that is desired (test-driven repair), but a FAIL is never itself promotion evidence — the post-freeze independent validation chain is unchanged and remains the only validation source. The INFRA_FAIL non-blocking stance is a deliberate tradeoff: a worker may propose while its last check attempt timed out; the mitigation is honest surfacing (ledger + review notes), not silence. If review experience shows models ignoring repeated INFRA_FAIL verdicts, revisit with an explicit N-strikes rule rather than the current never-clears blocking list.

---

### R2 (P0) — Task Memory made real: EvidenceLedger, hypothesis fields, checkpoint persistence, CODE_FAIL injection

**Motivation.** 方案 §3.1 promises Task Memory ("记录当前任务做到哪里"); today `current_hypothesis` is a dead field, `next_questions` does not exist, the model receives no distilled task state between turns, checkpoints carry two booleans, and CODE_FAIL re-planning cold-starts (evidence ②). This is the second narrative-vs-code gap and it directly hurts long-horizon repair quality.

**Functional description.**

- **EvidenceLedger (worker-level, deterministic).** Maintained by the agent loop *from Observations only* — never from model claims — recording: files checked with their last observed content hash (from `Observation.source_paths`/`file_hashes`), checks run with their deterministic verdicts (feeds from R1 once present; empty before), failed attempts (bounded list of edit failures with short redacted errors), and the current hypothesis/next questions (model-contributed, see below). It lives per worker and is exposed to the model in the `state` mapping each turn (`src/repair_agent/agent.py:190-196`), so decisions no longer re-derive prior state from raw observation dumps.
- **Model-contributed fields.** `batch_ready` (and only `batch_ready`) may carry optional `hypothesis`, `next_questions`, and `attempt_summary`; each is length-bounded and passed through `redact_text` before storage (`src/repair_agent/domain.py:455-459`). Parsing extends `ModelDecision.from_mapping` (`src/repair_agent/models.py:100-104`) with defaults that keep every existing decision valid (backwards compatible). Honest visibility note: `batch_ready` terminates the worker loop (every branch at `src/repair_agent/agent.py:293-303` returns), so the same worker never reads these fields back in `state` — their value is persistence and the next attempt. The mid-loop value of Task Memory comes from the deterministic ledger injected into `state` every turn (previous bullet); the hypothesis fields pay off on the re-plan injection path and in review artifacts.
- **Checkpoint persistence.** The worker checkpoint payload (free-form mapping at `src/repair_agent/runtime/store.py:311`; currently two booleans at `src/repair_agent/orchestrator.py:334`) additionally persists the serialized ledger. Worker checkpoints are rows keyed by `checkpoint_id`/`run_id` (`src/repair_agent/runtime/store.py:93-94`), so reading a prior run's ledger requires an explicit cross-run lookup — which does not exist today and is specified below.
- **Re-plan injection with an explicit wiring point.** Verified current state: `new_attempt_id` is written on CODE_FAIL (`src/repair_agent/orchestrator.py:566,757`) but has no consumer anywhere (repo-wide grep; `src/repair_agent/runtime/store.py:619` only derives a default when the field is absent), and the actual re-entry path is `resume()` returning action `replan_required` (`src/repair_agent/orchestrator.py:653,657`), after which the caller opens a **new run** with a different `run_id` — a worker checkpoint saved under the old run (`src/repair_agent/orchestrator.py:334`) is unreachable today. R2 therefore specifies the wiring: (a) `resume()`'s output carries the prior run id the caller should pass on re-plan; (b) `RepairOrchestrator` accepts `replan_of_run_id` on the new run and persists it in the run payload; (c) `_run_worker` (`src/repair_agent/orchestrator.py:316`, the AgentLoop construction point) loads the prior run's latest worker-checkpoint ledger through a new store read over the checkpoints table and injects it **read-only**, labeled as prior-attempt evidence, never merged into or writable through the new ledger; (d) absent or unreadable prior ledger → cold start exactly as today, with `prior_ledger_loaded: false` recorded in the run payload. `new_attempt_id` remains bookkeeping and is deliberately not the linkage key — runs, not attempts, are what the store and CLI operate on. Scope stays workflow-level re-plan per 方案 §3.1 — no claim of resuming a specific mid-loop model call.

**Acceptance criteria.**

1. A scripted observation sequence deterministically produces the expected ledger (files+hashes, failed attempts, verdicts); two loops fed the same observations produce byte-identical ledgers (unit test).
2. A `batch_ready` decision carrying `hypothesis`/`next_questions`/`attempt_summary` produces a ledger whose **serialized form** (the checkpoint payload) contains the three fields, bounded and redacted; a decision without the fields leaves behavior identical to today. Because `batch_ready` terminates the worker loop, no same-worker `state` visibility is claimed or tested — state visibility is asserted only on the injection path (criterion 4) (unit tests).
3. Over-long or secret-bearing model fields are truncated and redacted before they reach the ledger, any checkpoint payload, or any injected state (unit test using the existing secret patterns).
4. **Injection wiring, two levels:** (a) unit — the injection helper places a prior ledger into `state` under a read-only prior-attempt key, and mutations of the new ledger never alter the injected copy; (b) orchestrator-level — a second `RepairOrchestrator` run constructed with `replan_of_run_id` pointing at a first run whose worker checkpoint carries a ledger has its worker's `state` containing that ledger; with the key absent, the prior run missing, or the ledger unreadable, the run cold-starts exactly as today and the run payload records `prior_ledger_loaded: false` (unit + orchestrator-level test).
5. `resume()`'s output exposes the re-plan linkage (the prior run id) so an external caller can actually pass `replan_of_run_id`; existing resume/`replan_required` flows are otherwise unchanged (unit test; full suite run).

**Risks & tradeoffs.** Ledger size must be bounded (entry caps, mirroring the existing 50-path/8-entry caps in `_prompt_observations`) or the token savings of R3 are cancelled. Injection adds one more surface where stale evidence could mislead a new attempt — mitigated by read-only labeling and by the ledger carrying content hashes so the new worker can re-verify cheaply. The injection also depends on the caller actually passing `replan_of_run_id`; if an integration forgets it, cold start silently persists — which is exactly why the wiring is specified at a named function (`_run_worker`) with an orchestrator-level acceptance test and a `prior_ledger_loaded` marker, so "no ledger" is observable rather than assumed. Honest limit: this does **not** deliver per-model-call resume; checkpoint restore remains workflow-level re-plan.

---

### R3 (P1) — In-window observation deduplication

**Motivation.** The ContextCache made repeated reads cheap in I/O but not in tokens: a cache hit still emits a full-content observation (`src/repair_agent/tools/source.py:136-154`), and every prompt re-serializes the whole recent window (`src/repair_agent/agent.py:402-427`), so the same `(path, hash, range)` text is paid once per read per prompt (evidence ③).

**Functional description.**

- Window = the recent observations included in each prompt (`max_recent_observations`, default 10, `src/repair_agent/config.py:53-54`). Older observations already collapse to counts and pinned hashes (`src/repair_agent/agent.py:405-418`) and are out of scope.
- During prompt construction, when a recent observation repeats an earlier recent observation **exactly** — same tool, same path, same content hash, same requested range, both `complete=True`, status `OK` — the later occurrence is replaced by a compact reference: path, range, content hash, and the `tool_call_id` of the occurrence whose content is retained. The model can always re-read explicitly; the tool itself is unchanged and still returns full content.
- Anything less than byte-identical (`TRUNCATED`, `PARTIAL`, `VERSION_CHANGED`, differing range or hash, non-OK status) is never deduplicated. References are computed fresh per prompt; nothing is persisted across prompts.

**Acceptance criteria.**

1. A scripted loop that reads the identical `(path, range)` twice inside the window produces a prompt payload containing the full text exactly once plus one reference resolving to the first `tool_call_id` (unit test on `_prompt_observations` output shape).
2. A repeated read after the file changed (different hash) is never deduplicated (unit test).
3. A repeated read where either occurrence is `TRUNCATED`/`PARTIAL` is never deduplicated (unit test).
4. Two direct harness comparison runs of the same scripted decision sequence with deduplication on and off (a harness switch exercised directly; R5a's arms are unaffected) show the context-token estimate decreasing with dedup on and identical repair outcomes (benchmark check).

**Risks & tradeoffs.** The model must understand references — a small protocol addition, mitigated by keeping the reference self-describing (hash + range + call id). Dedup only pays inside the window; that is where the measured cost is. No behavior change to tools or evidence freshness: references are informational; the underlying observations are untouched.

---

### R4a (P1) — Lexical symbol scanner: member variables, namespace qualification, overload disambiguation

**Motivation.** `list_symbols` is the Level-3 navigation primitive (方案 §8). Verified gaps (probe run in this session against `src/repair_agent/tools/symbols.py`): member variables are not captured (`_try_function` requires a following `(`, `symbols.py:314-317`); namespace names are not propagated as enclosing prefixes (`symbols.py:400` propagates only `class`/`struct`), so `namespace audio { void helper() {} }` yields bare `helper`.

**Functional description.**

- **Member variables:** capture data-member declarations inside class/struct bodies as a distinct declaration kind (e.g. `member_variable`) with the file's line position; function-local variables are not captured. Lexical false-positive ceilings (macros, typedefs) are documented in the module docstring, extending the existing documented-ceiling style (`symbols.py:3-13`).
- **Namespace qualification:** nested namespaces compound (`a::b::`), namespace membership prefixes contained symbols, mirroring the existing class/struct mechanism; out-of-class `Class::method` definitions keep working.
- **Overloads:** same-name definitions already emit separate entries (verified); the requirement is **stable disambiguation** in the output so downstream consumers (`SymbolCache`, `_probe_symbol_confidence` at `src/repair_agent/memory.py:269-276`, the recall benchmark) can tell same-name entries apart — e.g. a disambiguated name or explicit overload index alongside the existing signature field.
- **Templates:** best-effort recognition with the documented-ceiling comment; template-heavy constructs may still be missing or mis-bounded, and the tool description must keep saying so.
- All existing consumers keep working: the `SymbolDecl` contract, symbol cache replay, the confidence probe, and the recall benchmark's Symbol Recall@5 (`scripts/benchmark.py:399-402`).

**Acceptance criteria.**

1. Fixture with a class containing member variables yields `member_variable` entries at correct lines; the fixture from the motivation (`namespace audio { class Engine { ... } }`) yields namespace/class-qualified names (unit test).
2. Fixture with two same-name member-function definitions yields two entries distinguishable by the disambiguation mechanism, not by line numbers alone (unit test).
3. False-positive guards: control-flow keywords, calls, and prototypes still produce no declarations; the existing documented ceilings remain documented (unit test + docstring check).
4. `_probe_symbol_confidence` and the recall benchmark run unchanged and deterministically against the extended output (existing tests pass; full suite run).
5. `list_symbols` output stays bounded (`MAX_SYMBOL_DECLS`, char budget) with the new kinds included (unit test).

**Risks & tradeoffs.** Lexical member-variable detection has inherent false-positive/negative ceilings (macros, `typedef`s); the requirement accepts documented imperfection over false semantics — the tool keeps saying "heuristic lexical outline, not semantic navigation". Namespace prefixing changes emitted names; the confidence probe's `endswith("::" + symbol)` check is compatible, but the recall fixture expectations must be updated in the same change.

---

### R4b (P1, stretch — cut-eligible) — clangd LSP-over-stdio adapter for `find_definition`/`find_references`

**Motivation.** True semantic navigation is the biggest single capability gap vs mainstream agents, and the existing tools are honest placeholders (`UNSUPPORTED`, `src/repair_agent/tools/executor.py:103-104`). The zero-dependency rule permits an external-binary subprocess; ripgrep already set the pattern (`src/repair_agent/tools/source.py:325-361`).

**Functional description (only if kept after review).**

- Configuration names the `clangd` binary and `compile_commands.json` location; absent configuration → tools remain exactly `UNSUPPORTED` (current behavior, no regression).
- One clangd process per worker workspace, spawned with fixed argv over stdio; a bounded, deadline-aware initialize handshake; requests/results bounded and redacted like every other tool output; returned paths normalized to workspace-relative and paired with content hashes.
- `find_definition`/`find_references` are read-only tools but are **excluded from action chunks in v1** (server lifecycle state makes chunk-replay semantics unsafe). This is a real code change, not just a note: `BoundaryDetector.READ_ONLY_TOOLS` currently admits both tools (`src/repair_agent/tools/chunking.py:35`) and their specs allow chunks (`src/repair_agent/tools/executor.py:103-104`); exclusion means removing them from that set or flipping `allowed_in_chunk` — recorded as a deliberate boundary.
- **Kill criterion (pre-agreed):** if a deterministic, deadline-bounded, stdlib-only handshake cannot be demonstrated within the round, the requirement is cut and the tools stay `UNSUPPORTED`. A half-working adapter must not ship.

**Acceptance criteria.**

1. Without clangd configured, both tools return `UNSUPPORTED` exactly as today; the full suite passes unchanged.
2. Unit tests drive a scripted fake LSP server over stdio (stdlib `subprocess` against a helper script) exercising initialize, definition, and references, including a timeout path that yields a bounded error observation rather than a hang.
3. An env-gated integration test with real clangd (skipped unless the binary and a compile database are present) resolves a planted symbol end-to-end.
4. After the change, an action chunk containing `find_definition`/`find_references` is rejected by the boundary detector with a tool-not-eligible reason (unit test), and the exclusion is documented in README "Deliberate boundaries": per-worker server, no chunk admission, v1 result bounds.

**Risks & tradeoffs.** The heaviest item in the round (the leader flagged it as review-cutable for exactly this reason): LSP handshake/lifecycle complexity, `compile_commands.json` availability in target repos, and cross-platform process management. Cutting it does not affect any other requirement's acceptance criteria — that decoupling is why it is split out of R4a.

---

### R5a (P1) — Benchmark suite mode: three arms, token estimate, check counts, `docs/BENCHMARK.md`

**Motivation.** All current numbers come from fixtures plus `ScriptedModel`, and the harness covers only cache-on/off and recall (`scripts/benchmark.py:41-45`, `README.md:43-45`). The 方案 §18/§19 story (arms, token metrics, check counts) is not executable in-repo yet.

**Functional description.**

- A `suite` mode in `scripts/benchmark.py` running the deterministic scripted repair fixture in **three arms**: baseline (cache off, no Task-Memory injection), cache (cache on), ledger (cache on + R2 injection). Once R1 lands, the scripted decision sequence includes `run_checks` and the report adds check-run counts; before that, the field is reported as absent rather than faked. Honest labeling: this arm set isolates mechanism contributions and is **not** the 方案 §19 A/B — §19's third arm is the full HEAL bundle (Context Memory + Progressive Navigation + Action Chunking), whereas here navigation is common to all arms and chunking (default-off) is not an arm; the deviation is recorded in §6.
- Reported per arm: model calls, tool calls by name, check runs, physical evidence reads, repeated physical reads (方案 §18), cache statistics (`ContextCache.stats()`, `src/repair_agent/context.py:180-192`), wall time, and a **context-token estimate** computed byte-based like `_prompt_token_reserve` (`src/repair_agent/models.py:305-309`) and explicitly labeled an estimate — stdlib-only means no real tokenizer, and the doc must say so.
- `docs/BENCHMARK.md` documents metric definitions, arm definitions, reproduction commands, and the standing notCovered scope (real 800-warning dataset, real-model A/B).

**Acceptance criteria.**

1. `py -3.13 scripts/benchmark.py suite` runs to completion deterministically (two consecutive runs produce identical metric values, wall time aside).
2. The report contains all three arms plus the fields above; the token metric is labeled as an estimate in both the JSON output and `docs/BENCHMARK.md`.
3. `docs/BENCHMARK.md` exists, matches the actual output field names, and repeats the notCovered scope note.
4. The benchmark unit tests cover the suite mode's aggregation functions (pure-function tests, as the existing `compute_recall_stats` pattern does).

**Risks & tradeoffs.** The three arms share one scripted decision sequence; differences therefore measure mechanism effects, not model-behavior effects — an honest limitation that the doc states. Token estimation is an approximation and must never be quoted as measured tokens.

---

### R5b (P2) — Real-model smoke test (env-gated) plus always-runnable localhost stub test

**Motivation.** No test has ever exercised `OpenAICompatibleModel` against a live socket (`src/repair_agent/models.py:154-338`); the leader's gap ⑤. *Deviation from draft:* the draft placed this inside P1 F5, but its acceptance depends on an external endpoint and credentials, which cannot be a verifiable P1 deliverable in this repository; it is therefore P2, and a stub-server test keeps the client path covered deterministically.

**Functional description.**

- An always-runnable unit test that serves a scripted OpenAI-compatible response from a stdlib `http.server` on localhost and drives `OpenAICompatibleModel` through a single `decide_with_deadline` call — covering HTTP, usage parsing, and protocol error mapping without external services.
- An env-gated smoke test (skipped unless explicit environment variables name endpoint, credential env-var, and model id) that performs one minimal decision round against the configured real endpoint. It never logs or asserts on credential values, redacts all output, and a skip is reported as skipped — never as a pass.
- Results of the smoke run are recorded as documentation, not as regression gates.

**Acceptance criteria.**

1. Default `py -3.13 -m unittest discover -s tests -p "test_*.py"` shows the smoke test skipped and the localhost stub test passing.
2. The stub test exercises success, HTTP-error, and malformed-response paths of the client (unit tests).
3. No credential value appears in any test output or trace (code inspection + redaction helper reuse).

**Risks & tradeoffs.** A live-endpoint run validates the client against one provider's dialect only; it proves reachability and protocol shape, not repair quality. Kept out of CI-by-default for cost and determinism.

---

### R6 (P2) — Model review-reason passthrough (redacted, capped)

**Motivation.** The model's own review reason is discarded (`src/repair_agent/agent.py:306`) and any unrecognized reason is collapsed to a generic string (`src/repair_agent/agent.py:49-89`); worker artifacts strip reasons entirely (`src/repair_agent/orchestrator.py:98`). Safe, but diagnosis is lost — reviewers see "worker requires review" with no signal.

**Functional description.**

- When the model supplies a `reason` on `review_required` (and `batch_ready`), that reason is passed through `redact_text` (`src/repair_agent/domain.py:455-459`) and a hard character cap, then preserved in the worker result and run payload as **diagnostic metadata alongside** (not instead of) the framework's fail-closed reason.
- `_safe_review_reason`'s whitelist behavior for framework-generated reasons is unchanged — the generic fallback remains the durable reason; the model reason is an additional field.
- The reason is never treated as evidence, never influences validation classification, and never reaches a proposal's authoritative fields.

**Acceptance criteria.**

1. A scripted `review_required` decision with a reason containing a secret-shaped token yields a stored diagnostic reason with the secret redacted (unit test using the existing patterns).
2. An over-long reason is truncated with an explicit marker; a reason with no secrets survives verbatim within the cap (unit tests).
3. Framework-generated reasons (budget exhaustion, protocol errors) behave byte-identically to today — no regression in the existing reason tests (full suite run).
4. The run report includes the diagnostic reason field, and worker-result artifacts no longer hard-strip it (`src/repair_agent/orchestrator.py:98` updated consistently).

**Risks & tradeoffs.** Arbitrary model text becomes durable metadata — mitigated by redaction, the length cap, and keeping it out of all decision/evidence paths. The two-field design (durable framework reason + diagnostic model reason) preserves today's fail-closed semantics exactly.

---

### R7 (P2) — Documentation: positioning, security review, implementation status

**Motivation.** The interview narrative needs the architecture story in the repo: how HEAL compares to mainstream coding agents and where it deliberately differs (evidence ⑦). The new trust surfaces from R1/R4b/R6 need security-review coverage.

**Functional description.**

- README gains a "Positioning vs mainstream coding agents" section: what mainstream test-driven repair agents do (in-loop build/test feedback, semantic navigation, task memory, compaction), which of those HEAL now has (after R1–R4), and where HEAL deliberately differs (controlled workflow, fail-closed boundaries, identity binding, zero dependencies, explicit `UNSUPPORTED` instead of pretend capability). Claims in this section must be backed by code or by this document.
- `docs/security-review.md` gains: trusted check execution (argv source, prefix caveat), clangd adapter IPC (if R4b is kept), review-reason redaction, and the continued worktree-is-not-a-sandbox boundary.
- `docs/implementation-status.md` gains entries for every landed requirement and keeps its honest notCovered section current.

**Acceptance criteria.**

1. README contains the positioning section with at least one code citation per claimed capability, and its "Deliberate boundaries" list matches the post-R1–R4 behavior.
2. `docs/security-review.md` covers each new execution/redaction surface listed above.
3. `docs/implementation-status.md` lists R1–R6 outcomes with the same implemented/notCovered honesty as existing entries.

**Risks & tradeoffs.** Positioning prose ages fast; binding every claim to a citation keeps it auditable.

---

## 4. Out of Scope

The following are explicitly out of scope for this round. Keeping them written down prevents scope creep and keeps the notCovered story honest:

1. **MCP server / external agent-tool protocols.** The tool surface is the internal registry; no protocol server is built.
2. **OS-level sandboxing.** Only an optional trusted command prefix (R1). The worktree remains an edit-isolation mechanism, not a security sandbox (`README.md:41`).
3. **Distributed / multi-node orchestration.** Concurrency stays bounded in-process workers (`src/repair_agent/concurrency.py`).
4. **The real 800-warning dataset and real-data A/B experiments (方案 §19).** The dataset is not in the repository; the benchmark scope note stays (`scripts/benchmark.py:41-45`). R5a builds the harness, not the dataset.
5. **RAG / vector DB / knowledge graph.** Excluded by design (方案 §1); retrieval stays structured scoring.
6. **tree-sitter or any parser dependency.** Explicit prior decision recorded in `src/repair_agent/tools/symbols.py:3-4`; R4a extends the stdlib lexer.
7. **Precise mid-loop (per-model-call) resume.** 方案 §3.1 keeps resume workflow-level; R2 does not change that.
8. **Changing promotion semantics.** In-loop check results (R1) never replace post-freeze independent validation; a worker-observed PASS is evidence for the model, never a validation classification.

---

## 5. Compatibility with the Three Invariants

**Zero dependency.** R1 executes configured external binaries as fixed-argv subprocesses (the established `LocalValidator`/ripgrep pattern); R4b likewise via clangd, with explicit `UNSUPPORTED` when absent; R5a/R5b stay stdlib (token metric is a labeled byte-based estimate, not a tokenizer); R2/R3/R6 are pure in-process logic; R4a extends the stdlib lexer. No new package enters `pyproject.toml`.

**Fail-closed.** R1: every verdict maps to an explicit status/complete pair — PASS/FAIL/INFRA_FAIL → `OK`/`complete=True` (the observation truthfully reports a complete verdict, never a fabricated PASS); integrity mismatches and uncaught execution failures → `complete=False`/`ERROR` and block; exit-code verdicts are deterministic and never text-parsed; INFRA_FAIL is persisted in ledger and proposal review notes, and independent post-freeze validation remains the only promotion evidence; unconfigured → `UNSUPPORTED`. R2: the ledger is derived from Observations, never model claims; model-contributed fields are redacted and bounded; prior-attempt injection is read-only, and a missing prior ledger is recorded (`prior_ledger_loaded: false`) rather than silently skipped. R3: dedup only for hash-verified byte-identical complete observations. R4a/R4b: heuristic outputs keep their documented ceilings; semantic tools stay `UNSUPPORTED` until genuinely configured; R4b's kill criterion prevents shipping a half-adapter. R5a/R5b: estimates are labeled estimates; a skipped smoke test is reported as skipped. R6: the durable fail-closed reason is unchanged; the model reason is additive diagnostic metadata. No requirement anywhere converts absence of evidence into success.

**Identity binding.** R1 binds check execution to `tree_hash`/`git_tree_oid` before/after, already gitignore-aware so build products don't corrupt identity. R2's ledger records content hashes, and CODE_FAIL injection carries those hashes so re-verification is cheap; checkpoints remain run-bound. R3 references carry content hashes of the exact retained read. R5a arms run over the same fixture commit. R6's diagnostic reason is metadata and never crosses into candidate or validation identity.

---

## 6. Deviations from the Leader's Draft (summary)

Each deviation, with its reason:

1. **F4 split into R4a + R4b.** The leader already marked the clangd adapter "heaviest, reviewer may cut". Merging it into the scanner work makes one requirement's acceptance criteria untestable as a unit; splitting lets the reviewer cut R4b without touching R4a, with a pre-agreed kill criterion.
2. **F5 split into R5a (P1) + R5b (P2).** The real-model smoke test cannot be an acceptance-gated P1 deliverable because its verification depends on external credentials/endpoints that the repository cannot guarantee; a localhost stub test was added so the client path is still deterministically covered.
3. **F4's "overloads separated" reframed.** Verified by running `scan_symbols` in this session: two same-name overload definitions already emit separate entries; the real lexical gaps are member variables and namespace qualification. R4a therefore requires *disambiguation* of same-name entries rather than their separation.
4. **F1's "build artifacts excluded via gitignore" bound to the existing mechanism.** `tree_hash` already enumerates via `git ls-files --exclude-standard` (`src/repair_agent/runtime/workspace.py:48-60`), so the requirement binds to that instead of inventing a new exclusion, and adds acceptance tests proving both directions (gitignored writes don't trip integrity; untracked non-ignored writes do).
5. **F1 gains explicit budget-plumbing acceptance.** Budget keys are enumerated manually at every site that handles them, and the full set is larger than a spot check suggests: a repo-wide grep for `max_search_rounds` shows eleven sites across six files (`domain.py:258/261`, `agent.py:119/321/368/385`, `config.py:79`, `planning.py:106/126`, `orchestrator.py:352/683`). Missing the config site would make `max_check_runs` unsettable from JSON; missing the planning site would drop it on CLI overrides. Acceptance 5 is therefore a completeness audit over all enumerated sites plus behavioral tests, not a list of "four places".
6. **F2's ledger constrained to deterministic derivation.** "Maintained by observation" is tightened to a testable rule (byte-identical ledgers from identical observation sequences; model claims enter only via the three bounded, redacted fields), and CODE_FAIL injection is pinned to read-only, hash-carrying, workflow-level semantics per 方案 §3.1's own scope statement.
7. **F3 scoped precisely.** Out-of-window repetition is already summarized (`historical_summary`/`pinned_evidence`, `src/repair_agent/agent.py:405-418`); the requirement covers only in-window duplication, with hash-verified byte-identical content as the dedup precondition.
8. **F6 keeps the whitelist untouched.** The generic fail-closed reason remains the durable reason and the model text becomes additive redacted metadata — regression safety for the existing suite outweighs replacing the mechanism.
9. **R5a's three arms deviate from 方案 §19's A/B.** §19's third arm is the full HEAL bundle (Context Memory + Progressive Navigation + Action Chunking); R5a's third arm isolates Task-Memory (ledger) injection instead, because navigation is common to all arms here and chunking is default-off. The suite therefore measures mechanism contributions, not the §19 bundle, and R3's acceptance uses direct dedup on/off harness runs rather than a fourth arm.

## 7. Suggested Order

R1 → R2 → R3 (each independently shippable; R2's ledger consumes R1's verdicts when present) → R4a → R5a (after R1/R2 so check counts and the ledger arm are real) → R4b (reviewer decision) → R6 → R7 (last, documenting what actually landed). The suite must remain green after every step; the current baseline is 109 passing tests.
