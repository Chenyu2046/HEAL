# HEAL Product Requirements Document (PRD)

Status: product view of the finalized requirements; **`docs/requirements.md` is the normative requirements record** (reviewed and finalized, including the reviewer's blocking findings). This document restates the same scope for stakeholder consumption, adds user stories and release planning, and introduces **no new requirements**. Every section below traces to a requirements.md section; where wording differs, requirements.md wins.

Audience note on identifiers: the leader's draft used F1–F7; review split two of them (F4, F5) into independently verifiable halves, producing the finalized IDs R1–R7 (nine IDs total). This PRD keeps the seven feature groups the draft audiences know as **F1–F7** and maps them explicitly (§4 traceability table).

---

## 1. Background and Problem

HEAL (Harman Code Quality Agent) is a pure-stdlib C/C++ static-alert repair pipeline: findings are normalized, planned into affinity batches, repaired by bounded workers in isolated Git worktrees, integrated serially, frozen as tree-hash-bound candidates, and validated through an independent post-freeze chain (`src/repair_agent/`, Python ≥ 3.11, zero third-party dependencies; branch `codex/harman-agent-harness-reliability`).

Three invariants are non-negotiable (see §5.1): zero dependency, fail-closed (never fabricate a PASS), identity binding (tree hash / commit).

**The problem.** The leader's gap analysis, verified line-by-line against the code (full evidence table in requirements.md §1.2), found that several promises of the narrative are not yet real in the repository:

- The worker loop cannot compile or run tests (`src/repair_agent/tools/executor.py:100-108` — no execution tool); trusted command execution exists only post-freeze (`src/repair_agent/orchestrator.py:747-763`).
- Task Memory (方案 §3.1) is a dead field: `current_hypothesis` is never written (`src/repair_agent/memory.py:24-36`), checkpoints store two booleans (`src/repair_agent/orchestrator.py:334`), and re-planning after CODE_FAIL cold-starts (`new_attempt_id` written at `src/repair_agent/orchestrator.py:566` has no consumer).
- Repeated identical reads inside the context window pay their tokens again in every prompt (`src/repair_agent/tools/source.py:136-154`, `src/repair_agent/agent.py:402-427`).
- `find_definition`/`find_references` are hard-wired `UNSUPPORTED`; `list_symbols` misses member variables and namespace qualification (verified by running `scan_symbols`).
- All numbers come from fixtures plus `ScriptedModel`; no real endpoint has ever been called from tests.
- The model's review reason is collapsed to generic text (`src/repair_agent/agent.py:306,49-89`); the README (49 lines) has no positioning story.

**Why now.** The project is used as an interview showcase. This round's purpose is to make the narrative promises real in code and close the gaps closable inside this repository — not to add new AI modules.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal | Traces to |
|---|------|-----------|
| G1 | Test-driven repair: the worker gets compile/check feedback inside the loop, under the existing trust and budget discipline | F1 |
| G2 | Task Memory is real: deterministic evidence ledger, persisted, and injected into re-plans | F2 |
| G3 | Context tokens stop paying for byte-identical repeated reads inside the window | F3 |
| G4 | Navigation improves honestly: lexical scanner covers members/namespaces/overload disambiguation; semantic navigation stays explicitly cut-eligible | F4 |
| G5 | Evaluation is executable in-repo: three-arm suite, labeled token estimate, check counts, benchmark docs, smoke tests | F5 |
| G6 | Diagnosis survives: model review reasons reach humans redacted and bounded | F6 |
| G7 | The story is in the repo: positioning, security review, implementation status | F7 |

Success is measurable through §6 (suite green, three-arm report fields, deterministic outputs).

### 2.2 Non-Goals

Exactly the eight exclusions of requirements.md §4 — restated, not extended:

1. No MCP server / external agent-tool protocols.
2. No OS-level sandboxing; only an optional trusted command prefix (the worktree remains an edit-isolation mechanism, not a security sandbox — `README.md:41`).
3. No distributed / multi-node orchestration.
4. No real 800-warning dataset and no real-data A/B (dataset not in repo; `scripts/benchmark.py:41-45` scope note stays).
5. No RAG / vector DB / knowledge graph (方案 §1).
6. No tree-sitter or parser dependency (recorded decision, `src/repair_agent/tools/symbols.py:3-4`).
7. No precise mid-loop (per-model-call) resume (方案 §3.1 keeps resume workflow-level).
8. No change to promotion semantics: in-loop check results never replace post-freeze independent validation.

---

## 3. Users and Scenarios

Two honest perspectives; this round's primary "user" is the second one.

### 3.1 Enterprise quality team (operational perspective)

**Who.** Quality/platform engineers operating HEAL against C/C++ repositories with CodeSonar-class static alerts, Gerrit-style review, and enterprise CI.

**Scenarios.**

- *Bulk triage*: a scan produces hundreds of warnings; the team needs proposals that arrived with in-loop check evidence (F1) and worker state that survives re-plans (F2), so reviewers see "what was checked, what failed, what the hypothesis was" — not a guess-only diff.
- *Diagnosis*: a proposal lands in REVIEW_REQUIRED; the team needs the model's actual reason (redacted), not a generic string (F6), to decide human effort.
- *Cost control*: long repair runs burn tokens re-reading the same file ranges (F3) and re-deriving prior state (F2); the team needs bounded, budgeted mechanisms.
- *Trust & audit*: every check execution is integrity-bound and verdicts are deterministic (F1); evidence is never model-claimed (F2); anything unconfigured is explicit `UNSUPPORTED`, never a silent guess.

**Honest boundary.** Enterprise use requires configuration this repo does not ship (trusted check commands, clangd/compile_commands, Gerrit/CI endpoints — all explicit `NOT_CONFIGURED`/`UNSUPPORTED` boundaries today, `docs/implementation-status.md:27-29`). The features are built so that configuring them is data, not code.

### 3.2 Interview showcase (reviewer perspective)

**Who.** An interviewer or reviewer reading the repository to judge architecture honesty and engineering discipline.

**Scenarios.**

- *Claim verification*: every capability claim in the README positioning section (F7) carries a code citation; the reviewer can diff narrative against source.
- *Reproducible evaluation*: the reviewer runs `py -3.13 scripts/benchmark.py suite` and gets a deterministic three-arm report with a labeled token estimate and an explicit notCovered note (F5) — no hand-waved numbers.
- *Discipline check*: the suite is green before and after every feature (109 tests baseline, §6); boundaries (`UNSUPPORTED`, fail-closed blocking, identity binding) are visible in tests, not prose.

---

## 4. Functional Requirements (F1–F7)

Traceability — the seven feature groups onto the finalized requirement IDs (one-to-one with requirements.md; no new requirements):

| PRD | requirements.md | Priority | Title |
|-----|-----------------|----------|-------|
| F1 | R1 | P0 | `run_checks`: trusted in-loop check execution |
| F2 | R2 | P0 | Task Memory made real |
| F3 | R3 | P1 | In-window observation deduplication |
| F4 | R4a + R4b | P1 (+ P1 stretch, cut-eligible) | Symbol navigation |
| F5 | R5a + R5b | P1 (+ P2 smoke) | Evaluation |
| F6 | R6 | P2 | Model review-reason passthrough |
| F7 | R7 | P2 | Documentation |

### F1 (R1, P0) — `run_checks`: trusted in-loop check execution

**Motivation.** Largest gap vs mainstream test-driven repair agents: the worker proposes patches it has never exercised. The safe pattern already exists post-freeze (`LocalValidator`, `src/repair_agent/validation/local.py:24-70`); F1 brings it inside the loop without weakening any boundary.

**User stories.**
- As a quality engineer, I want the worker to run the project's trusted build/check commands before proposing, so that proposals arrive pre-validated and check outcomes are part of the audit trail.
- As a reviewer, I want the model to be able to *invoke* but never *define* a check, so that arbitrary command execution cannot enter the system through the model.

**Functional description** (normative: requirements.md §3 R1). A new `run_checks` tool: check argv comes exclusively from configuration; execution reuses the trusted pattern (fixed argv, `shell=False`, deadline-bounded timeout, output truncated and `redact_text`-ed); exit code is the sole verdict source (`0 → PASS`, non-zero → `FAIL`, timeout/OS error → `INFRA_FAIL`), never text-parsed. Explicit status/complete mapping — PASS/FAIL/INFRA_FAIL → `OK`/`complete=True` (the verdict lives in content); blocking is reserved for execution-layer failures (integrity mismatch → `complete=False`; uncaught exception → `ERROR` via `src/repair_agent/tools/executor.py:140-141`). **INFRA_FAIL stance: non-blocking** at worker level — fail-closed means never fabricating a PASS, and an `INFRA_FAIL` verdict is explicitly not-PASS, persisted in the ledger and surfaced in proposal review notes; the alternative (timeout → blocking) was rejected because the blocking list never clears within a run (`src/repair_agent/agent.py:178`) and one flaky tool would end every worker in REVIEW_REQUIRED. The handler pre-trims its own content below the executor's downgrade threshold (`src/repair_agent/tools/executor.py:142-145`) so long logs cannot corrupt the verdict's completeness. Integrity is bound via existing `tree_hash`/`git_tree_oid` before/after the check — gitignore-aware by construction (`src/repair_agent/runtime/workspace.py:48-96`), so build products don't corrupt identity while untracked non-ignored writes do. New `max_check_runs` budget plumbed at every manual enumeration site (eleven sibling sites across six files, requirements.md §3 R1); each check also consumes a tool call. Not chunkable (non-read-only, `src/repair_agent/tools/chunking.py:51`). Optional trusted command prefix (convenience, not an OS sandbox). Unconfigured → `UNSUPPORTED`.

**Acceptance criteria** (carried from R1; requirements.md §3 R1 is normative).
1. Scripted `run_checks` call returns the deterministic verdict plus truncated, redacted output; exit code is the sole verdict source.
2. A legitimate FAIL — including one whose raw output exceeds `max_output_chars` — yields `OK`/`complete=True`, never enters `_blocking_tool_failures`, and a subsequent `batch_ready` is accepted; the FAIL verdict appears in proposal review notes.
3. Integrity mismatch (untracked non-gitignored file created by the check) → `complete=False` and `batch_ready` rejected; gitignored-path writes do not trip integrity; unavailable identity inputs (git failure) → `ERROR`/`complete=False` and block.
4. Timeout and OS-error paths yield `INFRA_FAIL`, non-blocking, recorded in ledger and proposal review notes.
5. Budget completeness audit: `max_check_runs` handled at every sibling budget-enumeration site; settable from config JSON, survives CLI overrides, exhaustion produces the budget review reason, counted in slot allocation and `resume()`'s `budget_remaining`.
6. `run_checks` never appears in an accepted action chunk.
7. Unconfigured → `UNSUPPORTED`/`complete=False`; the full suite stays green.
8. Model arguments can select only configured check names; anything else is schema-rejected pre-execution.

**Risks & tradeoffs.** Wall-clock consumption (per-check timeout defaults well below the task budget); arbitrary trusted binaries (R7 security note); a worker may propose while its last check timed out — mitigated by honest surfacing, revisit with an N-strikes rule if needed. A FAIL is never promotion evidence.

### F2 (R2, P0) — Task Memory made real

**Motivation.** 方案 §3.1's promise is dead code today: hypothesis never written, no `next_questions`, checkpoints carry two booleans, CODE_FAIL re-plans cold-start. The deterministic value is mid-loop (the model shouldn't re-derive state); the model-contributed value is persistence and the next attempt.

**User stories.**
- As a worker agent, I want a distilled, hash-carrying ledger of what I have already checked and edited, so that I stop re-deriving prior state from raw observation dumps.
- As a quality engineer re-planning after a CODE_FAIL, I want the new attempt to start from the previous attempt's evidence (read-only), so that re-plans don't cold-start.

**Functional description** (normative: requirements.md §3 R2). An **EvidenceLedger** per worker, derived *only* from Observations (files+hashes, check verdicts, bounded failed attempts) and injected into the model-facing `state` every turn; two loops fed the same observations produce byte-identical ledgers. `batch_ready` (only) may carry optional `hypothesis`/`next_questions`/`attempt_summary` — length-bounded, `redact_text`-ed, parsed backwards-compatibly (`src/repair_agent/models.py:100-104`). Honest visibility note: `batch_ready` terminates the loop (every branch at `src/repair_agent/agent.py:293-303` returns), so these fields are persistence and next-attempt artifacts, never same-worker state. The ledger is persisted in the worker checkpoint payload (`src/repair_agent/runtime/store.py:311`; keyed by `checkpoint_id`/`run_id`, `store.py:93-94`). **Re-plan injection with an explicit wiring point**: today `new_attempt_id` has no consumer and the real re-entry is `resume()` → `replan_required` → caller opens a new run with a different `run_id`; therefore — (a) `resume()` output carries the prior run id; (b) `RepairOrchestrator` accepts `replan_of_run_id` on the new run and persists it; (c) `_run_worker` (`src/repair_agent/orchestrator.py:316`, the AgentLoop construction point) loads the prior run's latest worker-checkpoint ledger via a new store read and injects it **read-only**, labeled as prior-attempt evidence; (d) absent/unreadable → cold start with `prior_ledger_loaded: false` recorded. `new_attempt_id` stays bookkeeping, deliberately not the linkage key. Workflow-level re-plan only (方案 §3.1) — no per-model-call resume claim.

**Acceptance criteria** (from R2).
1. Ledger determinism: identical observation sequences → byte-identical ledgers.
2. `batch_ready`-carried fields appear, bounded and redacted, in the serialized checkpoint ledger; absence leaves behavior unchanged; no same-worker state visibility claimed.
3. Over-long or secret-bearing fields are truncated/redacted before ledger, checkpoint, or injected state.
4. Two-level injection wiring: (a) unit — helper injects prior ledger read-only; mutations don't leak; (b) orchestrator — a run with `replan_of_run_id` has the prior ledger in its worker's `state`; missing key/run/ledger → cold start with `prior_ledger_loaded: false`.
5. `resume()` output exposes the re-plan linkage; existing resume flows unchanged.

**Risks & tradeoffs.** Ledger size bounded or R3's savings are cancelled; stale-evidence risk mitigated by read-only labeling and content hashes; the injection depends on callers passing `replan_of_run_id` — the orchestrator-level test and `prior_ledger_loaded` marker make absence observable rather than assumed. No per-model-call resume.

### F3 (R3, P1) — In-window observation deduplication

**Motivation.** The ContextCache removed duplicate *physical reads* but not *tokens*: a cache hit still replays full content (`src/repair_agent/tools/source.py:136-154`), and every prompt re-serializes the recent window (`src/repair_agent/agent.py:402-427`).

**User story.** As a worker agent, when I re-read a byte-identical range inside the same window, I want a compact hash reference instead of a second full copy, so that my prompt budget buys new information.

**Functional description** (normative: requirements.md §3 R3). During prompt construction, a recent observation that repeats an earlier recent observation **exactly** — same tool, path, content hash, range, both `complete=True`/`OK` — is replaced by a reference (path, range, content hash, first `tool_call_id`). Anything less than byte-identical (`TRUNCATED`, `PARTIAL`, `VERSION_CHANGED`, differing hash/range/status) is never deduplicated. References are computed per prompt; tools are unchanged; the model can always re-read explicitly. Out-of-window repetition is already summarized (`historical_summary`/`pinned_evidence`, `src/repair_agent/agent.py:405-418`) and is out of scope.

**Acceptance criteria** (from R3).
1. Identical `(path, range)` read twice in-window → full text appears exactly once plus one resolvable reference.
2. Re-read after file change (different hash) → never deduplicated.
3. Either occurrence `TRUNCATED`/`PARTIAL` → never deduplicated.
4. Two direct harness runs (dedup on/off) show the context-token estimate decreasing with identical repair outcomes.

**Risks & tradeoffs.** Small protocol addition (self-describing references); pays only inside the window, which is where the cost was measured; no change to tools or evidence freshness.

### F4 (R4a P1; R4b P1 stretch, cut-eligible) — Symbol navigation

**Motivation.** `list_symbols` is the Level-3 navigation primitive (方案 §8) with verified lexical gaps (member variables not captured — `_try_function` requires a `(`, `src/repair_agent/tools/symbols.py:314-317`; namespace names not propagated — `symbols.py:400`); overload definitions already emit separate entries (verified by running `scan_symbols`), so the gap is disambiguation, not separation. True semantic navigation is the biggest single capability gap, currently honest `UNSUPPORTED` (`src/repair_agent/tools/executor.py:103-104`).

**User stories.**
- As a worker agent, I want member variables and namespace-qualified names in a file outline, so that warning-symbol navigation reaches data members and namespace-scoped functions.
- As a quality engineer, I want semantic navigation only when a real clangd is configured, so that the tool never pretends capability it does not have.

**Functional description** (normative: requirements.md §3 R4a/R4b). **R4a**: `member_variable` kind for data members in class/struct bodies; compounding namespace prefixes (`a::b::`) mirroring the existing class mechanism; stable disambiguation of same-name entries; templates best-effort with documented ceilings; all consumers keep working (`SymbolCache`, `_probe_symbol_confidence` at `src/repair_agent/memory.py:269-276`, Symbol Recall@5 at `scripts/benchmark.py:399-402`). **R4b** (only if kept): clangd LSP-over-stdio adapter behind the existing tool specs; per-worker server, fixed argv, deadline-bounded handshake, bounded/redacted results; absent config → exactly today's `UNSUPPORTED`. Excluding the two tools from chunks is a real code change (`BoundaryDetector.READ_ONLY_TOOLS` currently admits them, `src/repair_agent/tools/chunking.py:35`). **Pre-agreed kill criterion**: a deterministic, deadline-bounded, stdlib-only handshake that cannot be demonstrated within the round cuts the requirement; a half-working adapter must not ship.

**Acceptance criteria** (from R4a/R4b).
1. Fixture class yields `member_variable` entries; the namespace fixture yields qualified names.
2. Two same-name definitions yield entries distinguishable by the disambiguation mechanism, not line numbers alone.
3. False-positive guards (control keywords, calls, prototypes produce nothing); ceilings stay documented.
4. Confidence probe and recall benchmark run unchanged and deterministically; `list_symbols` output stays bounded.
5. (R4b) Without clangd → `UNSUPPORTED` exactly as today, suite unchanged; unit tests drive a scripted fake LSP server over stdio including a timeout path; env-gated real-clangd test resolves a planted symbol; a chunk containing the tools is rejected by the boundary detector after the change.

**Risks & tradeoffs.** Lexical member detection has documented false-positive/negative ceilings — accepted over false semantics; namespace prefixing changes emitted names (recall fixture updated in the same change). R4b is the heaviest item — the decoupling exists precisely so the reviewer can cut it without touching R4a.

### F5 (R5a P1; R5b P2) — Evaluation

**Motivation.** All numbers come from fixtures plus `ScriptedModel`; the harness covers cache on/off and recall only (`scripts/benchmark.py:41-45`); no real model endpoint has ever been exercised from tests.

**User stories.**
- As an interviewer, I want one command that produces a deterministic three-arm comparison with a labeled token estimate, so that the evaluation story is executable, not narrated.
- As a maintainer, I want the OpenAI-compatible client exercised against a socket in tests, so that protocol bugs surface before an enterprise integration.

**Functional description** (normative: requirements.md §3 R5a/R5b). **R5a**: a `suite` mode with three arms — baseline (cache off, no ledger injection), cache (cache on), ledger (cache on + R2 injection) — reporting model calls, per-tool counts, check runs (once F1 lands; reported absent before that, never faked), physical/repeated reads, cache statistics, wall time, and a **byte-based context-token estimate explicitly labeled an estimate** (stdlib has no tokenizer; `_prompt_token_reserve` pattern, `src/repair_agent/models.py:305-309`); plus `docs/BENCHMARK.md` with metric/arm definitions, reproduction commands, and the standing notCovered note. Honest labeling: the arms isolate mechanism contributions and are **not** the 方案 §19 A/B (whose third arm is the full HEAL bundle) — a recorded deviation (requirements.md §6.9). **R5b**: an always-runnable localhost stub test (stdlib `http.server`) covering success/HTTP-error/malformed-response paths of `OpenAICompatibleModel`, plus an env-gated smoke test against a configured real endpoint (skipped by default; a skip is reported as skipped, never a pass; credentials never logged).

**Acceptance criteria** (from R5a/R5b).
1. `py -3.13 scripts/benchmark.py suite` runs deterministically (two consecutive runs identical, wall time aside).
2. Report contains all three arms and the fields above; the token metric is labeled an estimate in JSON and in `docs/BENCHMARK.md`.
3. `docs/BENCHMARK.md` exists, matches output field names, repeats the notCovered scope.
4. Suite-mode aggregation functions are unit-tested (pure-function style of `compute_recall_stats`).
5. Default discovery run shows the smoke test skipped and the stub test passing; stub tests cover the three protocol paths; no credential value appears in any output.

**Risks & tradeoffs.** One shared decision sequence measures mechanisms, not model behavior — stated in the doc; the token figure is an approximation and must never be quoted as measured tokens; the smoke run validates one provider dialect, not repair quality.

### F6 (R6, P2) — Model review-reason passthrough

**Motivation.** The model's own reason is discarded (`src/repair_agent/agent.py:306`) and unrecognized reasons collapse to generic text (`src/repair_agent/agent.py:49-89`); worker artifacts strip reasons (`src/repair_agent/orchestrator.py:98`). Safe, but reviewers diagnose blind.

**User story.** As a quality engineer triaging a REVIEW_REQUIRED run, I want the model's actual reason (redacted, bounded) alongside the durable framework reason, so that I can decide human effort without re-running the worker.

**Functional description** (normative: requirements.md §3 R6). Model-supplied `reason` on `review_required`/`batch_ready` is passed through `redact_text` (`src/repair_agent/domain.py:455-459`) and a hard character cap, then preserved as **additive diagnostic metadata**; the whitelist behavior for framework-generated reasons is byte-identically unchanged; the reason is never evidence, never influences validation classification, never enters a proposal's authoritative fields.

**Acceptance criteria** (from R6).
1. A reason containing a secret-shaped token is stored redacted.
2. Over-long reasons are truncated with an explicit marker; clean reasons survive within the cap.
3. Framework reasons behave byte-identically to today (no regression).
4. The run report includes the diagnostic field; worker-result artifacts no longer hard-strip it.

**Risks & tradeoffs.** Arbitrary model text becomes durable metadata — mitigated by redaction, the cap, and exclusion from all decision/evidence paths.

### F7 (R7, P2) — Documentation

**Motivation.** The architecture story must be in the repo, claim-by-claim auditable; the new trust surfaces need security-review coverage.

**User story.** As an interviewer, I want a positioning section that says which mainstream-agent capabilities HEAL now has, where it deliberately differs, and cites code for every claim.

**Functional description** (normative: requirements.md §3 R7). README gains "Positioning vs mainstream coding agents" (capability claims backed by citations; "Deliberate boundaries" updated post-F1–F4); `docs/security-review.md` covers trusted check execution (argv source, prefix caveat), clangd adapter IPC (if kept), review-reason redaction, and the worktree-not-a-sandbox boundary; `docs/implementation-status.md` records every landed outcome with its implemented/notCovered honesty.

**Acceptance criteria** (from R7).
1. Positioning section present with at least one code citation per claimed capability; boundaries list matches post-F1–F4 behavior.
2. Security review covers each new execution/redaction surface.
3. Implementation status lists F1–F6 outcomes with the same implemented/notCovered honesty as existing entries.

**Risks & tradeoffs.** Positioning prose ages; binding claims to citations keeps it auditable.

---

## 5. Non-Functional Requirements

### 5.1 The three invariants (per-feature summary; normative: requirements.md §5)

- **Zero dependency.** F1/F4b execute configured external binaries as fixed-argv subprocesses (the established `LocalValidator`/ripgrep patterns), explicit `UNSUPPORTED` when absent; F5 stays stdlib (token metric is a labeled byte-based estimate, not a tokenizer); F2/F3/F6 are pure in-process logic; F4a extends the stdlib lexer. No new package enters `pyproject.toml`.
- **Fail-closed.** F1: explicit verdict→status mapping; integrity/execution failures block; INFRA_FAIL never reads as PASS and is surfaced; unconfigured → `UNSUPPORTED`. F2: ledger derived from Observations, never model claims; missing prior ledger recorded (`prior_ledger_loaded: false`), not silently skipped. F3: dedup only for hash-verified byte-identical complete observations. F4a/F4b: documented ceilings; `UNSUPPORTED` until genuinely configured; R4b's kill criterion. F5: estimates labeled; a skipped smoke test is reported as skipped. F6: durable fail-closed reason unchanged; model reason additive. Nothing converts absence of evidence into success.
- **Identity binding.** F1 binds check execution to `tree_hash`/`git_tree_oid` (gitignore-aware, so build products don't corrupt identity). F2's ledger carries content hashes; re-plan injection carries them for cheap re-verification; checkpoints stay run-bound. F3 references carry the exact retained read's hash. F5 arms run over one fixture commit. F6's diagnostic reason never crosses into candidate or validation identity.

### 5.2 Performance and budget

- New budget `max_check_runs` with the same discipline as existing budgets: config-settable, CLI-override-safe, enforced pre-execution and in chunks, split across worker slots, and reported in `resume()`'s remaining calculation (F1 acceptance 5).
- Per-check timeout defaults well below `max_wall_seconds`; every subprocess is deadline-bounded (existing pattern: `src/repair_agent/tools/source.py:346-350`).
- The EvidenceLedger is entry-capped (mirroring the 50-path/8-entry caps of `_prompt_observations`) so memory never cancels F3's token savings.
- `list_symbols` output stays count- and char-bounded including new declaration kinds (`MAX_SYMBOL_DECLS` discipline, `src/repair_agent/tools/symbols.py:22-23`).
- The token metric is an explicitly labeled estimate; wall time is reported but never used as a correctness signal.

### 5.3 Security

- **Trusted command execution (F1):** argv is configuration data, never model input; `shell=False`; output redacted before storage; the optional prefix is convenience, not containment — the "worktree is not a security sandbox" boundary is restated in the security review (F7).
- **IPC (F4b, if kept):** clangd over stdio with fixed argv, deadline-bounded handshake, bounded/redacted results, per-worker lifecycle; a hung server yields a bounded error observation, not a hang.
- **Redaction (F1/F2/F6):** all model-contributed and command-output text passes `redact_text` (`src/repair_agent/domain.py:455-459`) before any artifact; credentials never appear in smoke-test output (F5b).
- **Metadata discipline (F6):** model-supplied reasons are diagnostic only — never evidence, never classification inputs.

---

## 6. Metrics and Benchmark Plan

**Baseline (measured this session).** `py -3.13 -m unittest discover -s tests -p "test_*.py"` → **Ran 109 tests … OK** (executed before and after the requirements work; re-confirmed after the PRD was written). No requirement may regress it.

**Existing harness.** `py -3.13 scripts/benchmark.py context|recall` — fixture-based, deterministic, with the explicit notCovered scope note (`scripts/benchmark.py:41-45`).

**Planned (F5/R5a).** `suite` mode, three arms (baseline / cache / ledger), reporting: model calls, per-tool counts, check runs (post-F1), physical and repeated physical reads (方案 §18 "Duplicate Physical File Reads / Task"), cache statistics, wall time, and the labeled context-token estimate. Deterministic: two consecutive runs identical apart from wall time. `docs/BENCHMARK.md` records metric definitions, arm definitions, reproduction commands.

**Planned (F5/R5b).** Smoke-test status reporting: localhost stub test always green; real-endpoint test skipped by default, reported as skipped when run without environment.

**Explicit notCovered (standing).** Real 800-warning dataset experiments; the 方案 §19 three-arm A/B on real data; measured (non-estimated) tokens; real clangd behavior without the binary/compile database. These remain notCovered until the dataset/infrastructure exists — the harness builds the measurement, not the data.

**Success signals for this round.** Suite green after every landed feature; three-arm report produced by one command; `docs/BENCHMARK.md` matches output; README positioning claims each carry a code citation.

---

## 7. Risks and Mitigation

| # | Risk | Traces | Mitigation |
|---|------|--------|------------|
| 1 | In-loop checks consume wall clock; long builds starve the budget | F1 | Per-check timeout defaults well below `max_wall_seconds`; deadline-bounded subprocess; check runs also budgeted (`max_check_runs`) |
| 2 | One flaky check tool permanently blocks workers (blocking list never clears in-run) | F1 | Explicit status mapping: verdicts non-blocking; blocking reserved for integrity/execution-layer failures; INFRA_FAIL surfaced honestly; N-strikes revisit path documented |
| 3 | Trusted binaries are an execution surface | F1, F7 | argv from config only; schema-rejected names; redaction; residual risk recorded in security review |
| 4 | Stale prior-attempt evidence misleads a re-plan | F2 | Read-only, labeled injection; content hashes for cheap re-verification; `prior_ledger_loaded` observability |
| 5 | Callers never pass `replan_of_run_id` → cold start silently persists | F2 | Wiring at a named function with orchestrator-level test; `resume()` exposes the linkage; absence is recorded, not assumed |
| 6 | Ledger growth cancels token savings | F2, F3 | Entry caps mirroring existing observation caps |
| 7 | Model misreads hash references | F3 | Self-describing references (hash + range + call id); tools unchanged; re-read always available |
| 8 | Lexical scanner false positives/negatives | F4a | Documented ceilings in the module's established style; fixtures with false-positive guards; honesty over false semantics |
| 9 | clangd adapter complexity ships half-working | F4b | Pre-agreed kill criterion; `UNSUPPORTED` default unchanged; cut affects nothing else |
| 10 | Token estimate quoted as measured | F5a | "Estimate" labeling in JSON and docs; metric definitions in `docs/BENCHMARK.md` |
| 11 | Smoke test against one provider dialect overfits | F5b | Stub tests cover protocol paths deterministically; smoke run is documentation, not a gate |
| 12 | Arbitrary model text becomes durable metadata | F6 | Redaction + hard cap + exclusion from evidence/classification paths |
| 13 | Positioning prose drifts from code | F7 | Citation-per-claim rule; boundaries list must match post-F1–F4 behavior |

---

## 8. Release and Rollback

### 8.1 Release order (normative: requirements.md §7)

R1 → R2 → R3 → R4a → R5a → R4b (reviewer decision: keep or cut) → R6 → R7. Each step keeps the full suite green (109-test baseline). R2's ledger consumes R1's verdicts when present; R5a comes after R1/R2 so check counts and the ledger arm are real; R7 lands last, documenting what actually shipped.

### 8.2 Rollout properties

- **Every feature degrades to an explicit boundary when unconfigured**: F1/F4b → `UNSUPPORTED`; F2 injection → cold start with a recorded marker; F3 → prompt construction change only; F5a → additive script mode; F5b → skipped test; F6 → additive metadata field. None requires migration.
- **Backwards compatibility is an acceptance criterion**: existing decisions parse unchanged (F2), framework reasons byte-identical (F6), `batch_ready`/resume flows unchanged (F2), the suite green after every step (all).
- **Feature switches follow repo precedent** (`context_cache_enabled`, `chunking_enabled` default-off): new behavior is switchable where it changes prompt semantics (F3's dedup switch is also the F5a comparison mechanism).

### 8.3 Rollback

- **Config rollback**: removing the `checks` section (F1) or clangd configuration (F4b) returns the tools to `UNSUPPORTED` — no code path changes.
- **Code rollback**: each requirement is an independent, separately revertible change (the R4a/R4b split exists for exactly this); F2's fields carry defaults so older persisted decisions and episodes remain readable; F3's reference construction is per-prompt with no persisted state to migrate; F6's diagnostic field is additive and consumers tolerate its absence.
- **What rollback never touches**: the post-freeze independent validation chain is the only promotion evidence throughout (§2.2 non-goal 8) — rolling back any worker-loop feature cannot affect candidate identity, approval, or validation semantics. Reverted workers simply return to today's behavior: no in-loop checks, cold-start re-plans, full-content reads — all of which are safe, merely less capable.

---

*Normative source: `docs/requirements.md` (§1 evidence, §3 requirements R1–R7, §4 out of scope, §5 invariants, §6 deviations, §7 order). This PRD adds user stories, consolidated NFR/risk views, and release planning only.*
