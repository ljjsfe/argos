# SC1 Results — Phase 0 Diagnostic

**Generated**: 2026-05-18
**Gate**: Score ≥0.9 on ≥4/6 tasks

## SC1-A — Opus + helpers (this commit)

| task | difficulty | score | helpers used | helper-gap observation |
|---|---|---|---|---|
| task_86 | easy | **1.0** | safe_read_csv, safe_read_json_df | "track number" → `position` semantic mapping is non-obvious from column names; no helper for column-semantic probe |
| task_163 | medium | **1.0** | safe_read_csv, safe_read_json_df | event→budget→expense join graph; nothing exotic |
| task_180 | medium | **1.0** | safe_read_csv | Amount=0 division-by-zero must be filtered explicitly; no helper for "safe per-unit calc" |
| task_344 | hard | **0.0** | safe_read_csv | **DATA GAP** — gold expects 4 distinct male patients, data only has 3. Failure is data-completeness, not helper-insufficiency |
| task_352 | hard | **1.0** | safe_read_csv | Narrative budget.md parse (sentence-level current-budget tracking + revised-amount last-wins); no helper for narrative-to-structured extraction |
| task_418 | extreme | **1.0** | (pure markdown parse) | Same narrative-to-structured pattern across Lab.md + Patient.md; no helper |

### Result

**5/6 tasks (83%) achieved Score ≥0.9 with Opus + current helpers.**

Gate `≥4/6` → **PASS**. Helpers are sufficient to express correct answers for 5 of 6 known-failure tasks. The 1 failure (task_344) is data-completeness, not helper insufficiency.

### Observations on helper gaps (from where Opus had to write substantial code)

Three patterns recurred but were NOT served by helpers:

1. **Narrative-to-structured extraction**
   - task_352, task_418 (and possibly others)
   - Per-paragraph regex with sentence-level current-entity tracking + "revised value overrides provisional"
   - Universal pattern but not in helper lib

2. **Sex-specific / domain-specific medical ranges**
   - task_344, task_418 (creatinine, WBC, FG)
   - Standard medical ranges treated as common knowledge, not data
   - May not be helper material (taxonomy issue rather than infrastructure)

3. **Semantic-mapping from question word → column**
   - task_86 ("track number" → `position`)
   - Hard to automate; requires schema-content alignment that small models miss
   - Possibly addressable by feature router (Phase 1C)

### Reproducibility

All A.py files + prediction.csv in `eval_split/skeletons/`.

```bash
for t in 86 163 180 344 352 418; do
  python3 eval_split/skeletons/task_${t}_A.py
done
```

## SC1-B — Opus, NO helpers (next)

Same 6 tasks, pure SQL/pandas/regex. Compares A vs B to determine if helpers are *binding* or *convenience*.

## SC1-C1 — Qwen + API skeleton (after B)

Per rubric.

## SC1-C2 — Qwen + logic skeleton (after C1)

Per rubric.
