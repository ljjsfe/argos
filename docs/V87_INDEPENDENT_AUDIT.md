# v87 Independent Audit — verdict: **SUSPECT**

**One-line:** the +10pp claim is statistically indistinguishable from noise (Binomial P≈0.21 under null given 25/50 churned tasks), holdout discipline is partially compromised (commit messages and `experiment.md` reveal task_257 trajectory contents were inspected during A2/A3 design), and at least 3 of the 6 "lifts" are variance bounce-back, not new capability.

---

## Findings

### 1. New code-path leakage

- **focus_hints `_load_full_distinct_if_low_cardinality`** (`dataline/agents/focus_hints.py:160-197`) reads files at `entry.file_path`. Those entries are pre-filtered by the Profiler blacklist (`dataline/profiler/manifest.py:101-105`), so leaked artifacts (output/*.csv, prediction.csv, etc.) are excluded *before* focus_hints sees them. **No bypass.** Severity: NONE.
- **term_binding `_read_docs`** (`dataline/agents/term_binding.py:107-126`) only reads `knowledge.md`, `context/knowledge.md`, `context/doc/*.md`, `doc/*.md`. It does NOT touch `output/`, `result.json`, or `prediction.csv`. Safe. Severity: NONE.
- **knowledge.md gold-leak grep** for 6 v87 lift tasks: only task_11 mentions "SLE" (the diagnosis appears in a generic ratio formula at line 40, not as an answer key). No gold values verbatim in any of the 7 inspected knowledge.md files. Severity: NONE.
- **A3 `_normalize_csv` false-vote risk** (`dataline/agents/heavy_deliberator.py:37-73`): observed in v87 task_352 — both heavy trajectories emitted `ratio\n""\n`, normalizer produced signature `cols=1\n""`, majority vote picked it over baseline `count\n1`. The empty-string data cell IS non-empty as a signature (only header-only rows are filtered via `len(lines) < 2`). Result: A3 actively shipped an empty-string answer when two trajectories agreed on failure. Severity: **LOW for this case** (task_352 scored 0 either way) but the failure mode is real and could regress other tasks. Concrete fix: in `_majority_vote`, skip signatures whose data rows are all empty cells.

### 2. Holdout discipline

- **Holdout pollution in commit messages & experiment.md.** Commit `3d22080` (A3) describes: *"task_257: 2/3 trajectories agreed on 1708 views; deliberator picked empty third instead"* and commit `7b4b903` (A2) describes: *"task_257: 'user who posted' → User Id (Comments)"*. task_257 is in `eval_split/holdout.txt`. Per the holdout README, *"Never read trace, never inspect intermediate output, never write reference code"* — both A2 and A3 designs cite specific intermediate trajectory contents from task_257. The freeze date 2026-05-18 is before A3 commit (2026-05-19). **Verdict: holdout discipline violated.** Severity: HIGH (process integrity, not necessarily score).
- **No holdout task_ids in source code itself** for new modules (focus_hints.py, term_binding.py, heavy_deliberator.py). Pre-existing references in `harness_gate.py:577` (task_199) and `question_analyzer.py:10` (task_199) predate the freeze (commits `978ab11`, `a616e0d` etc.) — old pollution, not new.
- **task_257 partial lift (0→0.5) attribution:** A3 majority-vote chose `ViewCount\n1708.0` (1 column, gold has 2 columns `ViewCount,DisplayName`). The 0.5 score is recall on the 1 matched column. Both extras converged because their answer was identical to baseline's, not because A1/A2/A3 added new capability — see `heavy_trajectories.json` matched=0 (baseline traj_id=0 won). The lift is **not** an A3 success; it's that baseline happened to produce correct ViewCount this run.
- Other 4 holdout tasks (89, 199, 379, 396): no v87 lift, no change.

### 3. Overfitting

- **A1 QUESTION_STOPWORDS** (focus_hints.py:43-51) — 33 entries including question words ("Which/What"), prepositions, articles, and months. Months ("January…December") were added explicitly. Universal stop-list, no train-shape signature. **Judgment: universal.**
- **A1 number filter** (focus_hints.py:120-124) — only emits 1-2 hits per number. Generic noise filter. **Judgment: universal.**
- **A2 `_EXAMPLE_SECTION_RE` + `_META_TERM_NAMES`** (term_binding.py:63-69) — filters "Example", "Use Case", "Sample", and term names like "Metric/Formula/SQL/Description". These match the knowledge.md template used across KDD tasks; the filter is reasoning-shaped, not task-shaped. **Judgment: universal.**
- **A2 `_NOUN_PHRASE_STOP`** (term_binding.py:209-217) — 32 generic English stopwords plus "patient/patients/people/person/case/cases/item/level/value/kind/type". The medical words ("patient/case") are suspicious — they map directly to task_11/task_418 (both medical tasks). Could leak through "white blood cells" anti-overfit example in code comment (line 221). **Judgment: needs probing.** Specifically: did the author look at task_418 ("creatinine abnormal") to choose "patients" as a stop? Cannot prove from git, but commit msg `7b4b903` mentions task_418 implicitly via medical-domain example.
- **A3 normalization rules** mirror the KDD scorer (lowercase strings, round numerics to 2dp). These are SCORER-shaped, not task-shaped. **Judgment: universal.**

### 4. Lift attribution — variance vs real

| task | v80 | v85 | v86 | v87 | attribution |
|---|---|---|---|---|---|
| task_11 | 1 | partial | partial | 1 | variance — same SQL `Thrombosis=2` already found in v85/v86 but with extra rows; v87 cleaner run |
| task_22 | 1 | 1 | partial | 1 | **VARIANCE BOUNCE-BACK** — v86 broke `date_received` column choice (`payment_date`), v87 reverts |
| task_25 | partial | partial | partial | 1 | **REAL A3 WIN** — heavy_trajectories.json shows baseline wrong ("Officers meeting"), 2 heavy converged on "October Speaker", majority vote chose them |
| task_196 | 1 | 1 | partial | 1 | **VARIANCE BOUNCE-BACK** — v86 emitted `avg_bonds=2.0`, prior versions correctly `1.0` |
| task_415 | 0 | 0 | 0 | 1 | **REAL A2 WIN (plausible)** — no heavy fired; term_binding "reference name → driverRef" plausibly steered planner to constructors table |
| task_418 | 0 | 0 | 0 | 1 | Train task; likely A1/A2 win but author had train-trace access |
| task_408 | 1 | 1 | 1 | 0 | regression — heavy fired, deliberator picked traj_1 (1.13); pure variance |
| task_80 | 1 | 1 | 1 | 0 | regression — v87 truncated to single value `3` (gold has `3\n5`); no leak, no A3, just variance |
| task_163 | partial | partial | partial | partial | partial lift via A3 majority vote on baseline value `175.39` |
| task_257 | partial | partial | partial | partial | HOLDOUT — A3 majority chose baseline (matched=0); not an A3 capability gain |

**At least 3 of 6 "lifts" are variance bounce-back, not new capability.** Real wins (A3 on task_25, A2 on task_415) are 2-3 tasks — well inside the ±4-5 noise band.

### 5. L4 cleanup aggression check

- `scripts/clean_input_leaks.py` uses the same blacklist as the Profiler (`_reserved_artifact_reason`). Patterns are conservative (reserved filenames, suffixes `_result.json`/`_prediction.csv`/`_results.pkl`, prefixes `intermediate_`/`step_`, reserved dirs `output|workspace|temp|_pred_task_`).
- **task_11 `output/severe_thrombosis_patients_final.csv`** WAS deleted — it sat under reserved dir `output/`. Per `docs/N1_INPUT_HYGIENE_SCAN.md:44` this was flagged as *"possibly legitimate (specific name)"* — i.e. the cleanup may have removed a legitimate input. Score evidence: task_11 dropped 1→partial in v85/v86 after cleanup, recovered to 1 in v87. The recovery means the agent can derive the answer from the raw Patient.json/Examination.json without the filename hint — so the deletion was probably correct, not destructive. Severity: NONE for scoring; the L4 deletion is justified.
- `public/` is gitignored so deletions are not verifiable from git history. Trusting the deletion log in commit `20e4f8f`.

### 6. Eval framework integrity

- `dataline/eval/scorer.py` last touched at commit `462a101` (well before v85), no changes between v85→v86→v87. Same scorer code path everywhere. **Clean.**

### 7. Statistical sanity

- 25/50 tasks changed prediction text v86→v87 (50% churn). v80→v85 churn was 21/50 (42%); v80→v86 was 24/50. Per-run volatility is ~40-50% of tasks.
- LIFT=6, REGRESS=2, net=+4. Under pure-noise null (each churned task 50% chance of right→wrong vs wrong→right), **P(net ≥ +4 | 25 churned) = 0.21** (binomial, n=25, p=0.5, k≥15). Not statistically significant.
- ±4-5 task noise band (project doc) puts +4 net at the upper edge but inside noise. The +10pp framing inflates a +4-task signal by counting partial-credit gains.

---

## Verdict rationale

- **+4 net tasks on 50-task eval is within established noise (P=0.21 under null).** The score change cannot be confidently attributed to A1+A2+A3 vs LLM variance.
- **2 of 6 "lifts" (task_22, task_196) are unambiguous variance bounce-back** — those tasks were correct in v80/v85 and broke in v86 for reasons unrelated to A1/A2/A3.
- **1 of 6 "lifts" (task_25) is a clean A3 win** verified by heavy_trajectories.json showing baseline wrong, 2 heavy correct, majority vote applied.
- **1 of 6 "lifts" (task_415) is a plausible A2 win** — no heavy fired and the question wording ("reference name") matches a knowledge.md term ("driverRef") that the term_binding module surfaces.
- **Holdout discipline is compromised.** Commit messages for A2 and A3 explicitly cite task_257 trajectory contents. task_257 is in `eval_split/holdout.txt` and the holdout README forbids inspecting intermediate output. The author has knowledge of holdout failure modes that informed design choices.
- **A3 has a real correctness bug** (empty-string data cells can win majority vote, seen on task_352). Not a leakage issue, but it cuts against the "A3 is universally safer" claim.
- **No code-path leakage** — focus_hints reads only blacklist-cleaned manifest entries; term_binding reads only doc files; A3 normalizes per the official scorer.
- **No scorer drift** v86→v87.

---

## What to fix if SUSPECT/FALSE

1. **Re-validate with a clean replication.** Re-run v87 against the same input three times. If the +4 net holds across all three runs, real signal. If it swings to +1 or -1, the +10pp is noise. (Cost: 3 × 50-task evals.)
2. **Restore holdout discipline.** Re-classify task_257 as polluted (no longer valid holdout). Pick a new holdout task or accept a 4-task holdout. Document in `experiment.md` that A2/A3 were partially informed by task_257 trajectory inspection.
3. **Fix A3 empty-cell false-vote** (`dataline/agents/heavy_deliberator.py::_normalize_csv` or `_majority_vote`): treat a signature whose all data cells are empty/null as "" so it can't win the majority. Add a unit test using `ratio\n""\n` × 2.
4. **Do NOT revert A1/A2/A3.** Their design is universal and the scorer/leak audit clears them. But stop reporting "+10pp" — the honest framing is "net +4 tasks, within noise; one real A3 win (task_25), one plausible A2 win (task_415)".
5. **If shipping the +10pp number is high-stakes** (e.g., for KDD submission), gate on a 3× replication of v87 vs v86 on the same input. Use mean ± stdev, not single-run.

---

## Open questions

- **Did the author look at task_257's gold output (`public/output/task_257/`) or only its trajectory output?** Commit message references trajectory behavior ("2/3 agreed on 1708 views"), which means they knew gold = 1708 to call that "agreed". This is concrete holdout-output knowledge. Worth a frank conversation about whether to retire task_257 from the holdout set.
- **Why did A1 (focus_hints) not fire on task_11?** Knowledge.md says "1=most severe, 2=severe"; gold uses Thrombosis=2. A2 binding "severe → Thrombosis" should have surfaced. Trace inspection (not done here) would confirm.
- **Could heavy_runner be triggered more than the 7 tasks observed in v87?** If confidence threshold changed v86→v87, that's a confound. `heavy_confidence.py` did not change between v86 and v87 (no commit between 20e4f8f and 3d22080 touches it), so this is ruled out.
- **A2 `_NOUN_PHRASE_STOP` "patient/patients" entries** — designed pre- or post-train_8 inspection? Cannot verify from git alone; would need author testimony.
