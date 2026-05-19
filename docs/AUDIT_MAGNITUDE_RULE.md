# Audit — Magnitude Bound Rule (P0-5 trace attribution)

**Generated**: 2026-05-18
**Eval dirs scanned**: v81, v82 (v83/v84 didn't carry the rule; reverted at a2a950e)
**Purpose**: decide whether Phase 3.1 (manifest range/median emit) is worth building.

## Fire frequency

| Eval | Tasks where rule fired | Total fires | Fired & passed | Fired & failed |
|---|---|---|---|---|
| v81 | 0 | 0 | — | — |
| v82 | 4 / 50 (8%) | 9 | 2 | 2 |

Rule only ever ran in v82 (8b8603b...a2a950e window). Fire rate **8%**.

## Per-task breakdown

| task | fires | v80 score | v81 score | v82 score | role of magnitude signal |
|---|---|---|---|---|---|
| task_344 | 2× warn | 0.0 | 0.0 | 0.0 | Fired correctly ("count answer non-integer 23.8") but Planner did not recover — co-fired with qa_column_count:block which dominated |
| task_163 | 1× (Judge cite) | 0.0 | 0.0 | 0.0 | Judge cited rule as "magnitude is plausible" while accepting **wrong** answer 437.19 → **false confirmation** |
| task_259 | 5× warn | 1.0 | 0.0 | 1.0 | Rule fired on a correct answer (`MAX(Score)=90813` vs range `[0,12]`) — **false positive** caused by joining against a coincidentally same-named column. v81 broke this task by unrelated rubric change |
| task_24 | 1× (Judge cite) | 1.0 | 1.0 | 1.0 | Judge cited "magnitude within bounds" — correct, but task was already passing |

## Attribution math

- **Tasks newly fixed by rule**: **0** (no task passed in v82 that didn't pass in v80 because of magnitude signal)
- **Tasks newly broken by rule**: 0 (task_259 broke in v81, not v82-because-of-magnitude)
- **False-positive cases**: 2 of 4 fires (task_259 warning was wrong; task_163 Judge cite confirmed wrong answer)
- **True-positive cases that helped**: 0 (task_344 the warning was true but didn't translate to a fix)
- **Confirmation-bias risk**: Judge uses magnitude signal to *confirm* whatever answer is in front of it (both task_163 wrong-confirm and task_24 right-confirm)

## Decision

**Phase 3.1 (manifest range/median emit) → BLOCKED.**

Reasons:
1. Rule fires on only 8% of tasks. Population is too narrow to justify infrastructure work.
2. Of the fires, FP rate ≈ 50% and TP→recover rate = 0%.
3. The Judge confirmation-bias mode (use range to validate whatever answer is current) is harmful regardless of how good the manifest data is.

Unlock conditions for revisit:
- New evidence path showing ≥5 tasks where magnitude check would catch a wrong answer the Judge *otherwise misses*
- A Judge prompt change that makes magnitude an **adversarial** check rather than a confirmation check
- Both required, in that order
