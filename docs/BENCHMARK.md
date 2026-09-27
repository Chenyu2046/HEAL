# Benchmark harness (方案 §18 落地部分)

`scripts/benchmark.py` is a fixture-based harness over deterministic synthetic
C/C++ repositories. Fixtures are generated at runtime into temporary
directories; nothing is read from a dataset. The harness is standard-library
only and lives outside the packaged `src/` tree on purpose.

**notCovered scope (standing):** the real 800-warning dataset experiments and
the three-arm A/B comparison of 方案 §19 (whose third arm is the full HEAL
bundle) — those require the warning dataset. Measured tokenizer counts are also
notCovered; all context sizes here are byte-based estimates, never measured
tokens.

## Reproduction commands

```bash
py -3.13 scripts/benchmark.py context   # cache on/off efficiency (R-Phase2)
py -3.13 scripts/benchmark.py dedup     # in-window read dedup on/off (R3)
py -3.13 scripts/benchmark.py suite     # baseline/cache/ledger three-arm comparison (R5a)
py -3.13 scripts/benchmark.py recall    # File Recall@3/@5 + Symbol Recall@5 (R-Phase2/R4a)
```

## Metric definitions (verbatim from the script output)

- `physical_evidence_reads`: in-process file content reads (`Path.read_text`/`read_bytes`) performed by the tools layer during the repair loop; version-check hashing (`refresh`/`mark_read`) is excluded, `edit_file`'s single hash-verification read is included and identical in both arms.
- `repeated_physical_reads`: 方案 §18 "Duplicate Physical File Reads / Task": `physical_evidence_reads` minus distinct paths.
- `logical_cache_hits`: logical tool calls served from ContextCache, counted separately per §18 (0 when the cache is off).
- `context_token_estimate_basis`: sum of UTF-8 bytes of each serialized prompt payload (task+state+observations), same construction as models.py `_prompt_token_reserve`, with observation `elapsed_ms` normalized to 0 during measurement (time-borne field, excluded so arm comparisons are deterministic); an ESTIMATE — stdlib-only, no tokenizer; never quote as measured tokens.
- `context_token_estimate_bytes`: the measured value under the basis above (suite and dedup reports; suite also carries per-arm values).
- `check_runs`: number of checks executed through the `run_checks` tool in the arm (suite; real count since R1 landed — reported as `null` before R1 by design, never faked).

Run-to-run variance ceiling (documented, not hidden): `wall_seconds` is wall
time and never a correctness signal. With `elapsed_ms` normalized out of the
estimate, the remaining byte-count variance across runs is zero to within the
fixed-width temporary repo path contents; only the *deltas between arms of the
same report* are meaningful, and those are what the comparison functions assert.

## Suite arms (R5a) — honest labeling per requirements §6.9

| Arm | `cache_enabled` | `ledger_enabled` | Isolates |
|-----|-----------------|------------------|----------|
| `baseline` | off | off | raw loop |
| `cache` | on | off | ContextCache effect |
| `ledger` | on | on | Task-Memory (EvidenceLedger) injection effect |

These arms isolate single mechanism contributions over one shared scripted
decision sequence on the extended context fixture (a deterministic `run_checks`
self-check enters the sequence). They are **not** the 方案 §19 A/B; navigation
is common to all arms and chunking (default-off) is not an arm.

Per-arm fields: `cache_enabled`, `ledger_enabled`, `model_calls`,
`tool_calls_by_name`, `check_runs`, `physical_evidence_reads`,
`distinct_files_read`, `repeated_physical_reads`, `logical_cache_hits`,
`cache_stats`, `wall_seconds`, `repair_completed`, `review_required`,
`review_reason`, `context_token_estimate_bytes`.

`comparison` fields: `estimate_baseline`, `estimate_cache`, `estimate_ledger`,
`delta_cache_vs_baseline`, `delta_ledger_vs_cache`,
`estimate_decreases_cache_vs_baseline`,
`physical_reads_decrease_cache_vs_baseline`,
`repeated_reads_decrease_cache_vs_baseline`, `model_calls_equal`,
`both_repairs_completed`.

Interpretation note: the cache arm replays *identical* content with zero
physical reads, so its prompt-byte estimate is structurally equal to the
baseline arm — `estimate_decreases_cache_vs_baseline` is reported as an honest
boolean, and the ContextCache mechanism signal is the physical/repeated-reads
comparison. The ledger arm pays its evidence-injection cost in prompt bytes and
is expected to measure higher.

## Dedup mode (R3)

Same fixture/sequence with `dedup_recent_observations` on vs off. Per-arm
fields: `dedup_enabled`, `context_token_estimate_bytes`, `model_calls`,
`repair_completed`, `review_required`, `review_reason`. `comparison` fields:
`estimate_off`, `estimate_on`, `estimate_decreases`, `model_calls_equal`,
`repair_outcomes_identical`.

v1 dedup covers byte-identical in-window `read_file` observations only; the
reference items are prompt-construction concerns and change no tool semantics.

## Recall (R-Phase2/R4a)

`file_recall_at_3`, `file_recall_at_5`: expected file within the first K
aggregated search-result files. `symbol_recall_at_5`: expected function within
the first 5 `list_symbols` declarations. The fixture includes planted symbols
across namespaces and one `frames_` member-variable case exercising the R4a
namespace-qualified outline. Static fixture and queries; no random source.
