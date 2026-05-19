# N1 — Input Hygiene Scan of All 50 Public Tasks

**Captured**: 2026-05-18
**Source**: `public/input/task_*/` recursive scan
**Output report**: by task, leaked-artifact files + empty/corrupt DBs

## Headline numbers

| metric | count | % of 50 |
|---|---|---|
| Tasks with any leaked artifact | **23** | 46% |
| Tasks with any empty/corrupt DB | **6** | 12% |
| Tasks with both | 4 | 8% |

| metric within 17 v80 failures | count |
|---|---|
| Failed tasks WITH leak | 10 |
| Failed tasks WITH empty DB | 3 |
| Failed tasks WITH neither (truly model-ceiling) | 5 |

**Among 23 leak-containing tasks, 13 PASS in v80**. So leaks don't always cause failure — Qwen sometimes ignores the leaked file. This is critical for regression-risk assessment.

## Detailed task list

### v80 failed tasks WITH leak (10) — Profiler hygiene candidates

| task | leak files | empty DB | tested? |
|---|---|---|---|
| task_25 | result.json (2 locations) | — | not tested |
| task_89 | output/result.json | — | not tested |
| task_169 | step_*_result.json (3) | — | not tested |
| task_199 | result.json + 4 retry variants | — | not tested |
| task_257 | result.json + exploration_result.json | — | not tested |
| task_352 | result.json + step + intermediate (4) | cards.db (DuckDB empty) | **A2 → 0 fix** |
| task_379 | temp/step_0_results.pkl | — | not tested |
| task_408 | output/result.json | races.db, circuits.db, results.db, results.sqlite | **B2 → +1.0 ✓** |
| task_415 | result.json + step | races.db (DuckDB empty) | **B2 → 0** (finalizer bug) |
| task_418 | creatinine_age_analysis_result.json | — | **A2 → 0 fix** |

### Currently-passing tasks WITH leak (13) — regression risk

| task | leak | concern |
|---|---|---|
| task_11 | output/severe_thrombosis_*.csv | possibly legitimate (specific name) |
| task_173 | context/output/countries_june_2013.json | possibly legitimate |
| task_194 | context/result.json | output convention — agent may currently ignore |
| task_218, task_249, task_26, task_283, task_287, task_292, task_420, task_64, task_75, task_80 | output/result.json or context/output/result.json or top-level | output convention; agent ignores |

Risk: if Profiler blacklist hides a file that some PASSING task secretly reads as legitimate data, score drops.

### Empty/corrupt DB cases (6)

| task | empty DBs | v80 score |
|---|---|---|
| task_173 | transactions_1k.db (DuckDB empty) | 1.0 (passes despite) |
| task_214 | card_games.db (zero bytes) | 1.0 |
| task_250 | users.db, users.db.db (zero bytes) | 1.0 |
| task_352 | cards.db (DuckDB empty) | 0.0 |
| task_408 | 4 empty DBs | 0.0 |
| task_415 | races.db (DuckDB empty) | 0.0 |

4 out of 6 fail in v80. Empty-DB awareness signal is more concentrated on failures.

## Tested-evidence summary

| hypothesis | tested | recovery rate |
|---|---|---|
| Leak removal alone | A2 (task_352, 418) | 0/2 |
| Leak + empty DB removal | B2 (task_408, 415) | 1/2 |
| Empty DB only (untested isolation) | — | unknown |

## Decision matrix

Three ship options + one no-ship:

| option | scope | risk | est lift | rationale |
|---|---|---|---|---|
| **S1** Conservative blacklist (top-level + `output/` only, not `context/`) + empty-DB metadata flag in manifest | LOW | +1 to +3 tasks | preserves all `context/` reads; empty DB now visible to PlannerCoder so it can avoid |
| **S2** Aggressive blacklist (any matching name anywhere) | MEDIUM | regression risk on 13 currently-passing-with-leak tasks | covers more cases but could break legitimate input |
| **S3** Empty-DB metadata only (no blacklist) | VERY LOW | +1 task (task_408) | minimal change, just metadata |
| **S4** No ship — accept ceiling, focus other axes | 0 | 0 | preserves budget |

## Regression-risk analysis (S1 vs S2)

Currently-passing tasks with leak are 13. Of these:
- 9 use `output/result.json` or `context/output/result.json` (output convention dir)
- 2 use `context/result.json` (top-level in context/)
- 2 use `*.csv` named like task-specific intermediates

**S1 — conservative**: skip `output/` subdir + top-level `result.json/step_result.json` + suffix patterns. Does NOT skip files inside `context/csv/`, `context/json/`, `context/doc/`, `context/db/`. Cannot hide intermediate `*.csv` files unless they match a strict pattern.

**S2 — aggressive**: same as S1 but also skips `context/result.json`, etc. Bigger regression risk.

The conservative S1 path probably safe; S2 needs paired regression eval.

## Cost-reduction value (not just score)

23/50 tasks have leaks. If Profiler blacklist saves even 1-2 wasted iters per affected task → 50-100 saved Qwen calls across full eval → meaningful $ savings on production runs.

For local eval at temp=0, cost savings might be 5-15% of total LLM bill.

## Recommendation

**S1 — Conservative blacklist + empty-DB metadata** is the lowest-risk meaningful change:

```python
# dataline/profiler/manifest.py::scan()

RESERVED_FILENAMES_TOP = {"result.json", "step_result.json", "prediction.csv",
                          "trace.json", "trace_agent.json", "status.json"}
RESERVED_SUFFIXES = ("_result.json", "_prediction.csv")
RESERVED_PREFIXES = ("intermediate_", "step_")
RESERVED_DIRS = {"output", "workspace", "_pred_task_", "temp"}

# In walk loop:
#   1. skip if any path segment in RESERVED_DIRS
#   2. skip if filename in RESERVED_FILENAMES_TOP and at TOP LEVEL or in context/
#   3. skip if matches RESERVED_SUFFIXES or RESERVED_PREFIXES
#   4. for .db/.sqlite files: still include, but tag empty-or-corrupt in manifest
```

Plus: emit INFO log per skip for auditability.

### Verification before ship

Run full v80 paired eval (with vs without blacklist) — cost ~$50. Or, do a smaller paired test on the 10 v80-failed-with-leak tasks (~$10) + a sample of 5 currently-passing-with-leak tasks for regression (~$5).

If lift ≥ +2 tasks AND regression = 0, ship. Else revisit.
