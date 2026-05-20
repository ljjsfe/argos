# Eval Split — Stable Failures

Frozen 2026-05-18 for Phase 0 SC1 diagnostic.

## Source

Derived from `scripts/mine_failure_patterns.py` cross-run analysis (101 historical eval runs, fail_rate ≥ 0.80).

13 stable failures total → split 8 train / 5 holdout, stratified by difficulty.

## Difficulty distribution

| difficulty | count | train | holdout |
|---|---|---|---|
| easy | 2 | 1 | 1 |
| medium | 6 | 4 | 2 |
| hard | 4 | 2 | 2 |
| extreme | 1 | 1 | 0 |
| **total** | **13** | **8** | **5** |

## Discipline rules

1. **train_8.txt** — may inspect trace, write reference code, derive helpers.
2. **holdout.txt** — task_id list ONLY. Never read trace, never inspect intermediate output, never write reference code. Used solely to validate Phase 1 ship decisions.
3. If holdout regression ≠ 0 after a Phase 1 ship, the phase reverts.
4. To change the split: requires a new freeze commit with reason logged in `experiment.md`.

## Files

- `train_8.txt` — 8 task_ids
- `holdout.txt` — 5 task_ids (read-only context)
