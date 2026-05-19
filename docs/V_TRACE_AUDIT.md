# V — Production v80 Trace Audit for 6 Train Tasks

**Captured**: 2026-05-18
**Source**: `results/eval_v80_fallback_20260516_0039/task_<id>/{trace.json,workspace/steps/}`
**Question**: Does the production multi-iter agent loop catch the 4 expression-level failure shapes seen in SC1, or is it blind to them?

## 1. Per-task summary

| task | iter | harness flags | judge final | real root cause |
|---|---|---|---|---|
| task_86 | 1 | 0 | finish | semantic mapping: filtered on `races.round`, not `standings.position` |
| task_163 | 5 | 1 (empty_output @ iter 4) | finish | aggregated by `expense.description` instead of `event.type` |
| task_180 | 2 | 1 (qa_column_count @ iter 1) | finish | filter scope wrong: returned 153 rows (gold 9) |
| task_344 | 1 | 0 | finish | data gap — gold (4) unreachable from given data |
| task_352 | **8 (max)** | 1 (qa_column_count @ iter 6) | backtrack (exhausted) | trusted leaked `result.json` artifact (all zeros) on 8/8 iters |
| task_418 | 4 | 0 | finish | trusted leaked `creatinine_age_analysis_result.json` (all zeros) on 4/4 iters |

## 2. Headline findings

### 2.1 HarnessGate is mostly silent on these failures

Across 21 iterations on 6 tasks, only **3** HarnessGate flags fired
(`empty_output` once, `qa_column_count` twice). All three flags were
generic shape-level checks, not expression-correctness signals. Confirms
the SC1 hypothesis: production loop has no signal for these bug shapes.

### 2.2 Production iterates extensively but doesn't recover

task_352 ran 8 iterations and remained stuck. task_163 ran 5. Iteration
count alone does not recover semantic / artifact bugs because each
iteration starts from the same wrong premise and the agent's reflection
narrative ("Prior Guidance") doesn't include a *deterministic* contradicting
signal.

### 2.3 NEW: leaked artifacts in input/ poison the agent

**This is the biggest single root cause** in our 6-task sample (2/6 = 33%).

Smoking-gun verification: Qwen's generated code references leaked filenames
on every iteration of the two affected tasks.

```
task_352 (8 code files): 8/8 reference 'result.json', 5/8 reference 'step_result'
task_418 (4 code files): 4/4 reference 'result.json'
```

Sources of the leaks:
- `public/input/task_352/context/result.json` (output from a prior eval run)
- `public/input/task_352/context/step_result.json`
- `public/input/task_352/context/intermediate_budget_full.pkl`
- `public/input/task_352/context/output/result.json`
- `public/input/task_418/creatinine_age_analysis_result.json` (top-level leak)

All five files were created by a prior run of our agent on these tasks and
got left behind in the input directory.

### 2.4 Failure-cause family breakdown

| family | tasks | % of failures (excluding data gap) |
|---|---|---|
| leaked artifact ingestion | task_352, task_418 | 2/5 = 40% |
| semantic / aggregation choice error | task_86, task_163 | 2/5 = 40% |
| filter scope error | task_180 | 1/5 = 20% |
| (data gap, not fixable) | task_344 | excluded |

## 3. Profiler current behavior

`dataline/profiler/manifest.py::scan` currently:
- skips hidden files (start with `.`)
- skips `task.json`
- includes **all other files**, including agent-output conventions
  (`result.json`, `step_result.json`, `prediction.csv`, `trace.json`,
  `intermediate_*.pkl`, files inside `output/`, etc.)

Because manifest surfaces these files, PlannerCoder treats them as
legitimate data sources and Qwen writes code that reads them.

## 4. Hypothesis A — universal input-hygiene blacklist

Add an output-convention blacklist to the Profiler so the agent never sees
its own prior-run residue. **This is universal hygiene**, not KDD-specific:
no data agent should consume artifacts that look like its own output.

### Candidate blacklist patterns

```
RESERVED_FILENAMES = {
    "task.json",          # already skipped
    "result.json",        # agent output (KDD + general convention)
    "step_result.json",   # our internal intermediate
    "prediction.csv",     # final output
    "trace.json",         # our trace
    "trace_agent.json",
    "status.json",
}

RESERVED_SUFFIXES = (
    "_result.json",       # e.g. creatinine_age_analysis_result.json
    "_analysis_result.json",
    "_prediction.csv",
)

RESERVED_PREFIXES = (
    "intermediate_",      # e.g. intermediate_budget_full.pkl
    "step_",              # workspace step artifacts
)

RESERVED_DIR_BASENAMES = {
    "output",             # contains agent outputs
    "workspace",          # our scratch dir
    "_pred_task_",        # SC1 prediction scratch
}
```

### Generality check

| test | result |
|---|---|
| substitute test (any data-agent project) | yes — every agent has output naming conventions |
| inverse-domain test (non-KDD) | yes — DABstep, image, video would have analogous output convention |
| pre-existence test | partial — gitignore conventions, `.gitignore`, build-system ignore lists are equivalent infrastructure shapes |

### Risk

- Possible false-positive: a legitimate input file happens to match a
  reserved name. Low risk: `result.json` and `step_result.json` are
  output-shaped names not commonly used for input data. Mitigation:
  log a manifest-level INFO when a file is skipped so it's auditable.
- Production submission may already have clean inputs (organizer prepares
  fresh per-task). In that case the blacklist is a no-op on submission
  but still protects local eval and Phase 2 (which we don't control).

### Estimated impact

- task_352: 0 → likely 1.0 (Opus reference path works; Qwen would now see
  budget.md directly). Need to verify with paired eval, not assert.
- task_418: 0 → likely 1.0 (same reasoning).
- Other tasks: no expected change (they don't read leaked files).

Expected eval delta on the 6 train tasks: **+2/6** (from 0/6 to 2/6).
Expected eval delta on full 50-task v80: likely +1-3pp (only tasks with
leaked artifacts benefit).

## 5. Verification done

| step | result |
|---|---|
| grep Qwen code for leak references | 8/8 task_352 + 4/4 task_418 iter — confirmed |
| Profiler skip list inspection | only `task.json` skipped — confirmed gap |
| All leaked filenames identified | 5 files cataloged — confirmed scope |

## 6. Not yet done (deferred until decision)

- Paired eval: run agent on tasks with/without leaked files removed locally.
- Actual blacklist implementation in `dataline/profiler/manifest.py`.
- Holdout (5) regression check.

## 7. UPDATE 2026-05-18 — A2 paired test FALSIFIES Hypothesis A

Ran production agent on `tmp/clean_input/task_352` and `tmp/clean_input/task_418`
with leaked artifacts removed (rsync exclude `result.json`, `step_result.json`,
`intermediate_*`, `output/`, `*_result.json`). Cost: ~$4. Result:

| task | v80 (with leaks) | cleaned input | delta |
|---|---|---|---|
| task_352 | 0.0 | 0.0 (returned "Unknown", all 3 heavy trajectories empty) | **0** |
| task_418 | 0.0 | 0.0 (returned 0, narrative parse still failed) | **0** |

Trace inspection shows that with clean input, Qwen DID look at the correct
data sources (budget.md, Laboratory.md, Patient.md) but failed at the
narrative-to-structured regex extraction. The leaked artifacts were a
*confound* that masked the underlying narrative-parsing failure — the
binding cause is the parsing bug itself.

**Conclusion**: Profiler blacklist would not improve score on these 2 tasks.
It may still have value as cost reduction (preventing wasted iterations) but
the original score-improvement justification is invalid.

**Lesson**: trace-based hypotheses require paired-eval validation. The
"8/8 iter referenced result.json" was a *symptom* of the agent having
no better path forward, not a *cause* of failure.

## 8. Re-categorized failure families (post-A2)

| family | tasks | root cause | tested fix? |
|---|---|---|---|
| Narrative-to-structured parse | task_352, task_418 | regex extraction fails on prose | SC1 + A2 both unable to fix |
| Semantic mapping / aggregation choice | task_86, task_163 | wrong column or aggregation level | not yet probed |
| Filter scope | task_180 | filter doesn't restrict to right subset | not yet probed |
| Data gap | task_344 | gold unreachable from data | not fixable |

No single infrastructure fix covers all 4 families. Awaiting direction.
