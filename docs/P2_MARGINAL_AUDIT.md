# P2 — v80 Marginal Failures Audit

**Captured**: 2026-05-18
**Source**: `results/eval_v80_fallback_20260516_0039/`
**Question**: Of v80's 17 failures, the 13 stable ones (≥80% historical fail rate) cannot be cheaply fixed per Phase 0 elimination. Are the 5 marginal failures (variance failures, sometimes pass) cheaper?

## 1. The 5 marginal failures

| task | historical fail rate | v80 score | iter | judge | shape |
|---|---|---|---|---|---|
| task_200 | 69% | 0 | 1 | finish | scalar-count, wrong scope |
| task_243 | 38% | 0 | 1 | finish | ratio, formula direction |
| task_25 | 69% | 0 | 2 | finish | "lowest cost" wrong source |
| task_408 | 46% | 0 | 8 | continue (exhausted) | DB infra failure |
| task_415 | 55% | 0 | 8 | continue (all_code_failed) | DB infra failure + wrong race |

Note: task_243 historically passes 62% of the time. v80's failure is variance,
not a permanent capability gap. Half of these are "variance failures".

## 2. Per-task failure shape

### task_200 — atom count scope
- **Q**: total atoms with triple-bond molecules containing P or Br
- **Pred 4 vs Gold 1**: pred counted all atoms in qualifying molecules; gold counted only the bonded atom(s) themselves
- **Shape**: semantic mapping — "atom" in question means *the atom in the bond*, not *every atom in molecule containing the bond*
- **Related to stable**: task_86 family ("track number" → wrong column)

### task_243 — ratio inversion + DISTINCT
- **Q**: For user 24, how many times is his posts vs votes
- **Pred 0.103 vs Gold 0.375**
- Pred = posts/votes; Gold = COUNT(DISTINCT votes.Id)/COUNT(DISTINCT posts.Id) = 0.375
- **Shape**: formula direction + missing DISTINCT
- **Related to stable**: none directly

### task_25 — wrong table for "cost"
- **Q**: Which event has the lowest cost?
- **Pred 3 events (Officers meeting Sep/Oct/Nov)** vs **Gold 3 events (September/October/November Speaker)**
- Pred used expense.cost; Gold likely uses budget.amount or different source
- **Shape**: source choice when multiple cost-related tables exist
- **Related to stable**: task_163 family (wrong aggregation source)

### task_408 — DB exploration failure (8 iter)
- **Q**: champion vs last finisher % faster in 2008 Australian GP
- **Iter behavior**: 8 attempts, all hit "no such table: races" or "file is not a database"
- **Root cause**: `races.db` and `circuits.db` are **empty** (size 0); `results.db` has data
- Agent picked wrong DB on every iter
- **Shape**: **discovery — Profiler did not surface that some DBs are empty**

### task_415 — DB infra + wrong filter (8 iter exhausted)
- **Q**: 2009 Singapore GP champion constructor + URL
- **Iter behavior**: 8 attempts, all_code_failed; final pred from earlier iter = "brawn" (2009 Brazilian winner, not Singapore)
- **Root cause**: `races.db` reports as "data" (not SQLite — probably DuckDB or corrupt), agent thrashed between formats
- Plus leaked `step_result.json` at top level
- **Shape**: same DB infra issue as task_408 + wrong race filter

## 3. Pattern grouping

| pattern | tasks | universal-fixable shape |
|---|---|---|
| Semantic mapping / scope | task_200, task_25 | same as stable task_86/163 — hard |
| Formula direction / DISTINCT | task_243 | possibly HarnessGate ratio check |
| DB infra (empty/corrupt DBs not surfaced) | task_408, task_415 | YES — Profiler should report DB rowcount per file |

## 4. NEW finding — Profiler doesn't surface DB-level "this DB is empty"

Profiler currently logs `Found 7 files, 0 relations` for task_408 with no
indication that 2 of the 3 `.db` files are empty (0 bytes). PlannerCoder
sees them as legitimate data sources and Qwen wastes 8 iterations on
empty DBs.

This is exactly the **infrastructure-first** philosophy from CLAUDE.md:
empty/corrupt DB detection should be deterministic in the Profiler, not
LLM-discovered.

## 5. Candidate fixes

| candidate | scope | est. fix rate | risk |
|---|---|---|---|
| **B** Profiler: per-DB rowcount summary in manifest (skip empty DBs from main view, flag corrupt ones) | 5-10 LOC in sqlite_reader.py | task_408 + task_415 = 2/5 marginal | LOW — universal hygiene; no LLM logic change |
| **C** Profiler output-convention blacklist (rejected by A2 for score, retain for cost reduction) | 10 LOC manifest.py | 0/5 score, but saves wasted iter on tasks like task_408/415 (which also have leaked files) | LOW |
| **D** HarnessGate: "answer count seems too high vs question's expected single-entity" rule | 1 new rule | possibly task_200 | MEDIUM — overfitting risk like #4 pending |

## 6. Compared to stable 13

- Stable 13 = mostly hard (narrative parsing, semantic mapping, data gaps)
- Marginal 5 = mix of (a) hard same-family patterns + (b) DB infrastructure issues

The DB infra issue is the cheapest universal fix discovered so far. It would not improve
stable-13 score (those don't have DB issues), but could recover 2 marginal failures and
reduce iteration cost.

## 7. Recommendation

**Option B (Profiler DB rowcount + empty detection)** is the only candidate that survives:
- Universal hygiene (no agent should consume empty DB silently)
- Pre-existence test (DB introspection is standard practice in ETL tooling)
- Substitute test (any data agent handling mixed DB inputs has this need)
- Inverse-domain test (any non-KDD domain with DB inputs benefits)

Estimated impact: +2/5 marginal failures (task_408, task_415) → ~+4pp on full eval.

But: same falsification protocol as A2. Before coding, **verify hypothesis B with paired
test**: temporarily move the empty/corrupt DB files from task_408/415 inputs, rerun
production, see if score recovers.

## 8. Open questions

- Do task_408 / task_415 also have leaked-artifact contributions? (yes — confirmed)
- Would *just* removing leaks help here? Per A2 result on task_352/418: probably not
  alone, but combined with empty-DB detection it might.
- Would Opus + helpers express the correct answer here? (untested for marginals;
  could run SC1-A on these 5 for cross-validation)
