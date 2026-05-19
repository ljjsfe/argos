# SC1-C1 / SC1-C2 Skeleton Rubric

**Frozen 2026-05-18 BEFORE any skeleton is written.** Applies uniformly to all train_8 tasks chosen for SC1. Changing this rubric mid-experiment voids the C1/C2 results.

## Purpose

SC1 probes three independent capability layers:

- **A** = Opus + helpers: shows what's achievable when helpers AND reasoning are both unlimited.
- **B** = Opus no-helper: shows what's achievable when reasoning is unlimited (no helper bias).
- **C1** = Qwen + API skeleton: shows whether Qwen can execute when given imports + helper-invocation template + probe template. Tests **helper discovery / invocation gap**.
- **C2** = Qwen + logic skeleton: shows whether Qwen can fill in expressions when given high-level decomposition. Tests **expression-level implementation gap**.

The contrast (A − C1) measures discovery deficit. (C1 − C2) measures decomposition deficit. (C2 < A) measures residual reasoning/implementation deficit.

## What goes WHERE — explicit boundary

### Allowed in BOTH C1 and C2

- Module imports (`import pandas as pd`, etc.)
- Data file paths and load calls (`pd.read_csv(...)`, `duckdb.connect(...)`)
- One blank `# TODO: ...` comment per logic step
- Final `print()` or DataFrame display showing the answer variable

### ONLY in C1 (API skeleton)

- Helper function names called with **empty/placeholder argument lists** — Qwen must fill the args.
  - `e.g. result = filter_with_diagnostic(df, <TODO: condition>)`
- A standard probe block:
  ```python
  # PROBE
  print(df.columns.tolist())
  print(df.head(3))
  ```
- **NO** decomposition narrative. No subgoal naming. No expression templates.

### ONLY in C2 (logic skeleton)

- High-level decomposition expressed as **named subgoals**, e.g.:
  ```python
  # Step 1: load the patients table
  # Step 2: filter to male AND white-blood-cell-normal
  # Step 3: count age <70
  ```
- Subgoal-level call templates with **TODO placeholders for the actual expression**:
  ```python
  # Step 2 — apply filters
  step2 = df[<TODO: combine male filter AND wbc-normal filter>]
  ```
- **NO** helper function calls (would conflate with C1 signal).
- **NO** revealed column names beyond what PROBE would show.

### Forbidden in BOTH C1 and C2

- Final answer value or any specific numeric/string output
- Exact column-name filter expressions (e.g. `df[df["gender"]=="M"]`) — these belong to Qwen's job
- Domain-specific business logic (e.g. "normal WBC = 4-11" specific thresholds)
- Direct mention of the expected answer shape if it isn't already in the question text

## Skeleton size budget

- C1 length: 15–40 lines (imports + load + probe + one TODO call per logic step)
- C2 length: 20–60 lines (imports + load + decomposition comments + TODO expression placeholders)

Skeletons longer than budget → reject and rewrite. Long skeletons leak too much answer.

## Scoring

For each task and each arm (A, B, C1, C2):

- Run the resulting code against the official scorer.
- Record `score` (0.0 / 1.0) and `dominant_failure` if any.

Per-arm metric: `pass_count / total_tasks_attempted`. Compare against gate `≥4/6`.

## Reproducibility log

For each task, save:

- `eval_split/skeletons/{task_id}_A.py`
- `eval_split/skeletons/{task_id}_B.py`
- `eval_split/skeletons/{task_id}_C1.py`
- `eval_split/skeletons/{task_id}_C2.py`
- `eval_split/skeletons/{task_id}_NOTES.md` (rubric-compliance verification)

All checked in. SC1 results then live in `docs/SC1_RESULTS.md`.

## Selection of 5-6 train tasks

Pick from `train_8.txt` covering diverse shapes. Initial target:

| Slot | task_id | difficulty | rationale |
|---|---|---|---|
| 1 | task_86 | easy | scalar lookup baseline |
| 2 | task_163 | medium | aggregate over filter (DABstep-like) |
| 3 | task_180 | medium | numeric filter + per-unit calc |
| 4 | task_344 | hard | multi-condition filter + count |
| 5 | task_352 | hard | comparison across categories |
| 6 | task_418 | extreme | stress test |

Skip task_169 / task_173 from train to leave room for second SC1 round if needed.
