# Experiment Log

Tracks eval runs, model changes, and architectural decisions with results.

---

## v5 Architecture (2026-04-23)

**Changes from v4:**
- Removed Skeptic agent (experimentally proven 0% effective)
- Removed sanity_checker (replaced by HarnessGate)
- Added HarnessGate: 13 deterministic rules, zero LLM cost, block/warn severity
- Added QuestionAnalyzer: answer shape inference (1 LLM call pre-loop)
- Native SQL execution path: raw SQL auto-wrapped, CSV→DuckDB views, .db→SQLite auto-attach
- PlannerCoder now receives `qa_guidance` from QuestionAnalyzer
- Judge receives harness warn flags as high-priority context
- Langfuse Cloud tracing: session/trace/observation hierarchy, post_score() for eval metrics

**Model:** Kimi k2.5 (Moonshot) → Qwen3.5-35B-A3B (DashScope/Alibaba Cloud)

**Hypothesis:** HarnessGate + QuestionSpec guidance should reduce shape mismatch failures.
HarnessGate blocks prevent Judge from seeing garbage results, saving LLM budget.

**Smoke test (task_163):** Pipeline ran end-to-end in 5 iterations, 276s, $0.62, 150K tokens.
- QuestionAnalyzer: correctly inferred table/2-col/multiple-rows ✓
- HarnessGate: correctly blocked iterations 0-3 (empty_output), passed iter 4 ✓
- Judge: correctly said finish ✓
- **Bug found & fixed**: Qwen wraps code in double fences → `SyntaxError: invalid syntax` on line 9. Fixed by stripping residual fence lines from extracted candidates.
- **Bug found & fixed**: Qwen adds `summary` key to `save_result()` → extra column hurts score. Fixed in finalizer (drop metadata keys) + added explicit prompt instruction.
- **Semantic error** (not a code bug): Agent answered expense descriptions + per-item totals, gold expects event type + aggregate total. Judgment/join issue.

**Round 2 smoke test (task_24, task_25, task_169) — before harness fixes:**
- All 3 tasks hit max_iterations=8, never finished. HarnessGate was over-blocking.
- Root causes: `agg_type:block` fired on valid intermediate SUM ops; `output_shape`+`qa_row_count` counted diagnostic stdout lines instead of actual answer rows.

**Harness fixes applied:**
- `agg_type` downgraded block→warn (can't distinguish intermediate vs final aggregation)
- `output_shape` + `qa_row_count` now use `structured_json` row count, fall back to stdout only if no structured output
- Added `_count_answer_rows()` helper

**Round 3 re-run (task_24, task_169) — after harness fixes:**
- task_24: 5 iters, answer=17 ✓ (matches gold), $0.45
- task_169: 1 iter, answer=82M ✗ (gold=459.96) — reasoning error: agent summed total consumption/12, but gold wants average per customer per month. Not a pipeline bug.

**Token cost per task:** ~$0.4-0.6, ~2-3min/task → 50 tasks ≈ $25, ~2.5h

**v5 Baseline eval (2026-04-23): 26/50 = 52%**
- easy 10/15, medium 11/23, hard 5/11
- Cost: $29.38, avg $0.59/task

---

## v5c — P0 + P1a + P2 (2026-04-23)

**Changes:**
- P0: HarnessGate `qa_row_count`/`output_shape` skip when structured_json absent (no stdout fallback)
- P1a: Judge prompt — B1 result count plausibility, B2 tie awareness, B3 format/unit check; iter-0 more skeptical
- P2: PlannerCoder prompt — heterogeneous source Mixed strategy hint (.db + .csv → prefer mixed candidates)

**Result: 28/50 = 56.8% (+4.8%)**
- easy 10/15 (flat), medium 14/23 (+3), hard 4/11 (-1)
- Net: +6 gained, -2 lost
- Cost: $34.21, avg $0.68/task (tokens up due to more iterations)

**Gained:** task_243, task_249, task_250, task_283 (full), task_38/task_259 (partial)
**Lost:** task_196 (P1a B3 made it re-compute correct answer → wrong), task_330 (P1a made it merge correct two-column answer → wrong format)

**Key insight:** P1a is double-edged — helps first-iter false-finishes but breaks correct first-iter answers. P0 and P2 are clean wins with no regression. Core remaining problem (22 failures) is wrong_computation — LLM doesn't understand data semantics (units, column meaning, aggregation grain).

**Next: P1b — Analyzer semantic layer to fix wrong_computation root cause.**

---

## v5d — P1a refinement (2026-04-23)

**Changes:**
- B2: no longer auto-fail; adds guidance note only (fails only if question explicitly asks "all tied values")
- B3: fires only on two specific signals (time question + large integer >100K; percentage question + value outside 0-100)
- Iter-0 skepticism: removed blanket rule; extra skeptical only when QuestionSpec shape mismatches output

**Result: 26/50 = 52.0% (-4.8% vs v5c)**
- easy 8/15 (-2), medium 14/23 (flat), hard 4/11 (flat)
- Cost: $32.45, avg $0.65/task

**Gained:** task_196 ✓ (P1a regression fixed), task_257 ✓, task_330 ✓ (hard, P1a regression fixed)
**Lost:** task_11 (Judge B1 hallucinated "18 records" when gold=3, DATA_PROFILE empty due to Analyzer path bug), task_22/task_249 (LLM code variance), task_350/task_194 (Finalizer extracted stdout text instead of structured JSON — scalar answer not wrapped in list)

**Root cause analysis:**
- task_11: B1 triggered LLM hallucination — Judge had no grounding data (DATA_PROFILE empty), B1 asked it to estimate expected row count, LLM filled void with parametric memory
- task_350/194: `save_result(answer={'count': 7})` — scalar value (not list) failed `_try_structured_extract` validation → fell back to raw stdout string
- task_22/249: LLM code generation variance, unrelated to Judge changes

**Fixes applied for v5e:**
- B1: added grounding rule — any count claim must cite explicit number from Data Profile or stdout; if absent, write "count unknown" and do not fail
- Finalizer: scalar values in answer dict now auto-wrapped in list `[v]` before validation
- Analyzer path bug identified (task_11 only): LLM-generated profiling code used `task_dir/json/` instead of `task_dir/context/json/`

---

## v5e — B1 grounding + Finalizer scalar fix (2026-04-23)

**Changes from v5d:**
- Judge B1: grounding rule — count claims must cite context; "count unknown" if absent
- Finalizer: scalar answer values auto-wrapped in list
- (Analyzer path bug not yet fixed — separate issue)

**Hypothesis:** Recover task_11 (B1 grounding), task_350 (Finalizer fix). task_22/249 remain LLM variance.

**Result: 28/50 = 56.5% (back to v5c level)**
- easy 8/15 (-2 vs v5c), medium 14/23 (flat), hard 5/11 (+1), extreme 1/1 (+1)
- Cost: $30.31, avg $0.61/task

**Gained vs v5c:** task_257, task_330, task_418 (extreme!), task_194 (Finalizer fix), task_350 (Finalizer fix)
**Lost vs v5c:** task_11, task_22, task_249 — confirmed LLM code generation variance, not Judge-related

**Key insight:** Judge/Finalizer layer now stable. Remaining 22 failures split: ~9 code_error (debugger bottleneck), ~11 partial_result (wrong_computation). Next target: P1b — Analyzer semantic layer to reduce wrong_computation.

---

## v5f — P3a (Debugger context) + P3b (empty_output diagnostic) — ROLLED BACK (2026-04-23)

**Changes attempted:**
- P3a: Debugger prompt + signature — add `question`, `plan_intent`, `judge_guidance` to context
- P3b: Orchestrator — `consecutive_empty_output` counter, inject diagnostic guidance after ≥2 consecutive empty_output blocks

**Result: 23/50 = 46.0% (-10pp vs v5e) — rolled back**
- Gains (+2): task_11 (P3b diagnostic worked), task_259
- Losses (-7): task_19, task_257, task_269, task_330, task_350, task_418, task_64

**Root cause analysis:**
- 4/7 losses (task_19/330/257/64) had NO Debugger involvement — pure LLM variance between runs
- 2/7 losses (task_269/350) had empty prediction.csv despite judge=finish — Finalizer extraction failed, likely code did not call save_result() properly after Debugger rewrote logic
- 1/7 (task_418) had empty_answer, possibly P3b diagnostic loop disrupted flow

**Critical finding: LLM variance ±4-5 tasks at temperature=0 across runs**
Changes of ±2 tasks are within noise floor. Only improvements >5 tasks are statistically meaningful on this 50-task eval set.

**P3a revised direction (v5g, also rolled back):**
- v5g tested: pass `question` + `plan_intent` only (no `judge_guidance`) → 23/50 = 46%, same as v5f
- 7 losses in v5g: only 2 had Debugger involvement, 5 had zero crashes — pure LLM variance
- Conclusion: P3a gains are real but insufficient to overcome ±5 task run-to-run variance on 50-task eval
- The 50-task eval set has too low signal-to-noise ratio for changes affecting <5 tasks

**Definitive finding: eval noise floor is ±4-5 tasks**
Any improvement targeting fewer than 5 tasks cannot be reliably measured on this benchmark.
Next meaningful changes must either: (a) target ≥5 tasks, or (b) use repeated runs to average out variance.

---

## v5h — P1b: Deterministic Value Distributions (2026-04-24)

**Changes from v5e:**
- `analyzer.py`: Added `_deterministic_value_distributions()` — always runs after LLM profiling, zero LLM cost
- For each CSV/SQLite/Excel/Parquet: string/categorical columns → all values with counts (≤100 unique), else top-20
- Low-cardinality numeric columns (≤30 unique) also profiled
- Appended as `## DETERMINISTIC VALUE DISTRIBUTIONS` section in data_profile
- Guarantees PlannerCoder sees exact filter values even if LLM profiling code was incomplete

**Hypothesis:** Fixes "filter returns 0 rows" pattern where PlannerCoder guesses wrong categorical values.

**Result: 29/50 = 58.0% (+2pp vs v5e)**
- easy 6/15 (-4 vs v5e), medium 16/23 (+2), hard 6/11 (+1), extreme 1/1 (flat)
- Cost: $33.21, avg $0.664/task

**Gained (+5):** task_199 (city filter fix — direct P1b), task_249, task_259, task_379, task_420
**Lost (-4):** task_330 (timeout/LLM hang — infrastructure, not P1b), task_257/27/64 (LLM variance)

**Key insight:** task_199 confirmed as direct P1b fix (city filter). Other 4 gains likely indirect (more context → better code generation). Losses are noise/infrastructure, not regressions from this change. P1b is a clean win.

**Remaining failures (21 tasks):**
- partial_result: 13 (62%) — wrong computation, Judge finishes too early
- code_error: 7 (33%) — crashes Debugger can't fix
- empty_answer: 1 (5%) — pipeline failure

---

## v5i — Layered Robustness: QuestionSpec fields + HarnessGate Rule 14 + Finalizer stdout-leak (2026-04-24)

**Changes from v5h (six files):**
- `types.py`: QuestionSpec gains `tie_possible: bool` and `computation_type: str` fields
- `question_analyzer.md`: Output JSON extended with `tie_possible`, `computation_type`, `expected_row_count="one_or_more"` option; Rules 7+8 added
- `question_analyzer.py`: Parse and validate new QuestionSpec fields
- `harness_gate.py` Rule 14 (NEW): WARN when tie_possible=True + code uses LIMIT 1 + 1-row result (catches silent tie-drop)
- `harness_gate.py` Rule 12 FIX: No longer blocks multi-row results when `spec.tie_possible=True` (was incorrectly reducing tied min/max results to 1 row)
- `finalizer.py`: `_has_stdout_leak()` guard — skip structured_json step if values contain newlines or >300 chars (catches save_result(raw_stdout) antipattern)
- `planner_coder.md`: Added "Critical Computation Rules" section (ratio≠difference, min/max→avoid LIMIT 1, keep name columns separate)

**Hypothesis:** Prompt guides are probabilistic; deterministic rules enforce contractually. Each v5i change converts a known partial_result root cause into either: (a) a guaranteed structural check that blocks bad output, or (b) structural LLM guidance with spec-verified enforcement.

**Result: 29/50 = 58.0% (flat vs v5h, within noise floor)**
- Scores from eval_report_kdd.json: overall_accuracy ~0.563

**Gained (+5):** task_11, task_25 (tie_possible fix — Rule 12 allowed 3 tied rows through), task_64, task_196, task_214
**Lost (-6):** task_418/task_420 (still running at eval collection — timing artifact, not regressions), task_199/259/283/379 (LLM variance)

**Key insight:** task_25 confirms tie_possible fix worked — 3 tied "Officers meeting" rows returned without being collapsed to 1. task_214 confirms wrong-shape fix (structured 7-row table no longer suppressed). Net change is 0 excluding timing artifacts (task_418/420 counted as lost because no prediction file at eval time).

**Root cause of apparent regressions:**
- task_418/420: `status.json` shows `"status": "running"` at eval time — batch eval collected results before tasks completed
- task_199/283/379: LLM variance — same question answered differently across runs (P1b context still present, just different LLM path)

**Remaining failures (21 tasks):**
- partial_result: 12 tasks (50%)
- empty_answer: 8 tasks (33%) — 5 genuine failures + 3 timing artifacts
- code_error: 4 tasks (17%)

---

## v6 — Candidate Consensus (Full Execution + Disagreement Signal) — REVERTED (2026-04-24)

**Changes from v5i:**
- Orchestrator: run ALL code candidates (not break-on-first-success)
- Compare structured answers across successful candidates
- Disagreement → WARN flag injected into Judge context
- Agreement → logged as confirmation

**Hypothesis:** Running all candidates and comparing answers would catch computation errors through disagreement signal.

**Result: 27/47 = 57.4% (identical to v5i on matched tasks) — REVERTED**
- 3 tasks stuck (task_330/418/75 — infrastructure, likely API timeout from increased load)
- Gained (+4): task_27, task_283, task_352, task_420 (all LLM variance)
- Lost (-4): task_214, task_24, task_25, task_250 (all LLM variance)

**Why it failed:**
- Consensus signal fired on only 3/47 tasks — candidates almost never both succeed
- When 2+ candidates succeed, they agree (0 disagreements detected)
- In v5i historical data: only 5/44 second-candidate attempts succeeded
- Root cause: candidate parsing issues (LLM markdown leaks into code) + most tasks generate only 1 candidate
- Added execution cost with zero signal value

**Key learning:** Multi-candidate voting requires high candidate success rate to be useful. With current ~11% 2nd-candidate success rate, this approach is dead on arrival. To make it viable would require: (a) fixing candidate parsing, (b) prompting for more independent candidates, (c) using completely separate LLM calls per candidate (like Best-of-N). Not worth the complexity at this stage.

---

## v7–v80 Condensed Log (2026-04-24 to 2026-05-16)

Detailed per-run entries skipped during a 3-week burst. Reconstructed from `git log` + `results/eval_*/eval_report_kdd.json`. Scores below are `overall_accuracy` from the eval report. Full data in `results/`.

**Eval noise floor ±4-5 tasks reconfirmed throughout.** Many "regressions" within this band are LLM variance, not code regressions.

### Phase A — Post-v6 stabilization (v7–v23, 2026-04-24 to 27)
- v7–v9 (NaN BLOCK, finalizer column trim, gate tuning) — ~58%
- **v10–v12 DuckDB unification**: CSV/JSON/Parquet auto-registered as views, single SQL dialect across formats. v11=64%, v12=59% (shape rule regression, fixed in v13=62%)
- **v15–v16 disaster**: aggressive prompt/rule changes collapsed to 38% then 22%. Rolled back via v17/v18 (61%)
- v19–v23: recovery + `feat: v23 stagnation detection + missing save_result guard` (`4062f05`) → **65%**

### Phase B — Surgical fixes (v24–v28, 2026-04-27 to 28)
- **v24 FK detection fix** (`9c065f5`): camelCase columns + sequential int guard → **66%**
- **v25 extra-column block** (`dc0a2a8`): QuestionSpec + HarnessGate upgrade → **66%** (clean)
- v27 narrow ratio hint reverted (`dbec8c9`→`0ee9377`): overfitted to ratio questions
- v28 confirms post-revert baseline: 62%

### Phase C — Model + prompt restructure (v29–v35, 2026-04-28)
- **Qwen 3.6 experiment**: 6258078→0299a9f reverted. Hard task collapse 63%→18%. **Locked in qwen3.5-35b-a3b**
- v31–v34: PlannerCoder 3-step decision sequence (`f1bbc8a`), Judge formula step elevation (`3612b5f`), QuestionSpec injection (`2678f8e`) → 62-64%
- **v35 Judge trust HarnessGate WARN rules** reverted (`728c8a3`→`09fff68`): made Judge inconsistent

### Phase D — Manifest enrichment (v36–v42, 2026-04-29)
- v36 join cardinality (1:1/1:N/N:1/N:N) in manifest (`0b7217f`) → 64%
- v37 paired cardinality+COUNT(DISTINCT) rule reverted (`f036b06`→`b059246`) — over-constrained
- v38 "thick Python" helpers/EDA encouragement → regression 52%
- v39 save_result auto-expand (`bb301c8`) → recovery 60%
- v40 targeted fixes → 65%
- **v41 JSON view as DuckDB registration** (`9d61180`) → **peak 68%**, but v41b confirm dropped to 62% — variance
- v42 metric_idx top-of-doc promotion (`52e56dc`) → 65%

### Phase E — Voting + REPL experiments (v43–v50, 2026-04-30 to 05-01)
- **v43 adaptive voting** reverted (`a5f5ab2`→`c7db47a`): same multi-candidate flaw as v6
- **v44/v45 stateful Python REPL saga**: v44=16% (race conditions), v45=71% (one-off), v45b=18% (proved variance + REPL fragility), full revert in `b871de2`/`09eab`
- v46–v48: REPL v3 + introspection + visibility, all eventually reverted (`b6d949a`)
- **v49 column-count rule** (`fef…`) → **64%**, retained
- v50 prompt compression reverted (`ee9aab8`→`48ed740`): lost critical guidance

### Phase F — Routing + extract + audit (v51–v60, 2026-05-01 to 02)
- v51 stdout-leak BLOCK + narrative-doc routing (`05e8ccb`) → 62%
- **v52 EXTRACT_MAPPINGS** (`1c47a1e`→`b9690a2`) — **reverted on 1-task validation only, NO full eval**. Open question
- v53 skeptic reintroduction reverted — confirmed still 0% effective
- v56 OCR for safe_read_image + scanned-PDF fallback (`f7d07d0`)
- v57 multimodal helpers + dict-string flag + stdout_leak (`cdf375b`)
- **v58 dead code removal** (`81ee81f`): decomposer, second_opinion, error_context — net positive
- **v59 playbook framework** (`a4c8bbf`): empty entries, fail-soft. See CLAUDE.md "Reverted Experiments" for why we don't populate it
- v60 prompt audit cleanup (`9d100cd`) → had 2 sub-reverts; final state v60.2 = 61%

### Phase G — Vision + harness verification (v61–v68, 2026-05-02 to 06)
- **v61–v62 vision integration** (`e2248e0`+`7b53682`): `LLMClient.chat_with_image()`, integrated into PlannerCoder. First attempt collapsed (v62 vision=14%), retry v62b=66%
- **v65 Invariant Verifier** (`dbc200b`): 3 rules added into `HarnessGate.check()` (I1 pct-range, plus 2 others) — not a separate file → 64%
- v66 invariant pct scope + consensus severity downgrade (`8b3492b`) → 56%/60% across re-runs
- **v68 self-initiated re-profile** (`4c3f746`): empty_output + DISTINCT probe MVP → 65%

### Phase H — Heavy mode build (v69–v80, 2026-05-14 to 16)
The big architectural shift. Built failure-triggered K-trajectory ensemble.

| Commit | What | Status |
|---|---|---|
| `c13fb5f` | HeavyTrajectory + HeavyDecision dataclasses | retained |
| `cf20aaa` | `is_confident()` signal | retained |
| `836dc06` | Deliberator agent + prompt | retained |
| `31387df` | `run_task_heavy()` wrapper + `with_temperature` | retained |
| `ac64b40` | CLI `--heavy-mode` flag + `config.heavy_mode` | retained |
| `cbed054` | K-1 trajectories parallel (ThreadPoolExecutor) | retained |
| `4ae2f9b` | Persist per-trajectory artifacts for debug/fallback | retained |
| `743c30e` | Deliberator trust-baseline bias | **REVERTED** `b424ea8` — defeats the point |
| `37e3cb1` | Fix is_confident reads correct flat judge_action field | retained |
| `bbc1655` | Real wall time (not sum of parallel) | retained |
| `f4869d7`/`11c8f3a` | Share manifest + domain_rules across trajectories | retained |
| `65a5d12` | Disable in-process REPL — pandas SIGSEGV under parallelism | retained |
| `f2ae46e` | Answer sanity confidence signals | **REVERTED** `63995fc` — over-triggered |
| `288a9a9` | Empty-list low-confidence trigger | **REVERTED** `f2dede0` — same family |
| `97490ea` | identify-X-and-Y multi-column inference | **REVERTED** `6377e0b` — over-inferred 2-col |

**Score landings**:
- v70 (heavy first run, sequential) = **66%**
- **v70b (heavy parallel) = 71%** — peak so far
- v70c (variance check) = 66%
- v71 trust-baseline experiment = 63% (reverted)
- v72–v75 = 62-64% (judge fix, shared ctx tuning, subprocess sandbox)
- v76 (answer-sanity signals) = 62% with `heavy_deliberator=4` bottleneck — signals caused over-spawn
- v77 (revert v76) = **67%**
- v78 (narrow fixes) = 59% (regression, reverted)
- v79 (v77 stability re-run) = 64%
- **v80 (fallback hardening, current head) = 66%** — `9d14f8c` adds 2-layer prediction.csv fallback

### Phase I — Submission infrastructure (parallel to Phase H)
Not eval-scored. Built during 2026-05-14 to 16:
- `3ce23b3` KDD Cup Docker submission package
- `4e6e8dd` parallel=8 config
- `15344ed` remove hardcoded base_url (KDD §5.2)
- `537cf8a` add missing critical deps to requirements.txt
- `6694967` build_submission.sh hardening
- `1d5a638` test_submission_locally.sh — auto-clamp CPU/mem, cp not symlink
- `cc984df` wire heavy mode + LPT into KDD Docker entry point
- `9d14f8c` 2-layer fallback — never ship 0-byte prediction.csv

### Lessons Carried Forward (consolidate)

1. **±4-5 task noise floor** holds across the entire 3-week span. Every "small win" inside that band must be assumed LLM variance until repeated.
2. **Multi-candidate at the planner level produces no diversity** (v6, v43) — same LLM call same temp. True diversity needs independent calls = heavy mode.
3. **Stateful Python REPL is hostile** to parallelism (pandas C ext SIGSEGV under threads). Don't retry without process isolation.
4. **Qwen 3.6 is not a drop-in upgrade** — hard tasks collapse. Stay on qwen3.5-35b-a3b.
5. **Most "guidance" prompt additions over-constrain** the planner (v27 ratio, v37 cardinality+COUNT(DISTINCT), v50 compression). Add structural enforcement (HarnessGate rules) instead of prose.
6. **Heavy mode confidence signal is delicate** — multiple over-trigger reverts (v76, v288a9a9, v97490ea). Default to less-aggressive triggering.
7. **EXTRACT_MAPPINGS revert is data-thin** — only 1-task validation. Worth re-trying with proper full eval.
8. **Playbook is intentionally empty** — fill only from our own eval-validated patterns, never from external "universal rules" lists.

---

## v81 / v82 — Rubric Judge experiment series (2026-05-17) — ALL REVERTED

**Goal**: Validate whether prompt-engineering Judge to be more rigorous
(rubric-style critique with citation requirement) can lift Phase 0.3's
measured silent-failure rate (57.5% of all eval failures).

**Setup** (one full session, ~$45 LLM, 8 commits, all reverted):
- Phase 0.1 trace mining: 4393 (eval, task) records across v5-v80
- Phase 0.2 synthetic capability probe: P1-P5 = 100/100/40/80/80%
- Phase 0.3 production replay harness: 80-case silent-failure dev set
  + 40-case clean-success false-positive set (NEW: lasting asset)
- 4 hygiene commits: stripped KDD scorer formula, fixed BLOCK contradiction,
  centralized ESCALATABLE_RULES, neutralized KDD-named test fixture
- Rubric prompt iterations v1 → v2

**Three experiments tried and reverted**:

### v81 — Wholesale `rubric_v2` swap to judge.md
- Replay dev catch +9pp (36 → 45%), clean FP 12.5%
- Full eval: **52.3% (-14pp vs v80 = 66%)**
- Reverted (`cb45772`).
- Root cause: replay set had no clean-case control, missed iteration-cost
  amplification. False positives on clean tasks consumed all 8 iterations.

### v82 — Selective rubric routing (#1) + magnitude rule (#2)
Selective routing: `RUBRIC_PROMPT_SHAPES = {count, aggregate, ratio}`,
non-whitelisted shapes use baseline judge.md. Magnitude rule: deterministic
BLOCK on count<0 / non-integer, WARN on AVG/MIN/MAX out of column range.

- Replay dev catch 50% (+14pp), clean FP 5% (both gates passed)
- Full eval: **58% (-8pp vs v80)**
- Reverted (`e0fb29b` + `a2a950e`).

### Real root cause (the lesson that has lasting value)
Diagnostic after v82 failure showed:
- `magnitude_bound` rule **never fired** on any of 50 production tasks
- 3 of 5 "regressions" were LLM variance (tasks unrelated to our changes)
- 2 real regressions (task_250 aggregate, task_420 ratio) reproduced
  rubric_v2's Mode B failure: 8 iterations all `continue`, Judge gives
  CORRECT specific critique, Planner cannot converge
- Same tasks in v80 baseline: **score 1.0** (baseline judge succeeded)

→ The bottleneck is **Planner-Judge coupling**, not Judge quality alone:
  - Rich, structured critique (rubric) + weak Planner = Planner fixates
    on the specific path implied by the critique and never escapes
  - Sparse critique (baseline) + weak Planner = Planner explores more,
    sometimes lucky
  - Concrete evidence: task_420 had code_validator warnings naming
    correct close-match columns; Planner ignored them and submitted
    code with the wrong names anyway

### What we now know about Qwen 3B Judge capability (from replay data)
- Can: integer/range/bound checks with crisp criteria → reliable
- Cannot: judgment calls requiring evidence-weighing → either marks
  everything n/a (cargo cult citation) or rigidly applies one rule
  and over-rotates
- Prompt complexity → MORE noise, not less

### Decisions / DO-NOT-RE-TRY
- Rubric-style Judge prompts (any variant) on Qwen 3B: dead-end on this
  benchmark UNLESS Planner's response discipline improves first
- Magnitude rule via SQL AST parsing on data_profile: too narrow to fire
  in production (only catches obvious AVG/MIN/MAX out-of-range; real
  failures are wrong column / wrong join / wrong filter)
- Replay-set-only validation: insufficient. Always pair with clean-case
  FP measurement before declaring improvement

### Permanent assets from the session (NOT reverted)
- `scripts/mine_failure_patterns.py` + `docs/FAILURE_PATTERN_LIBRARY.md`
- `scripts/probe_qwen_capability.py` + `docs/QWEN_CAPABILITY_PROFILE.md`
- `scripts/build_replay_set.py` + `scripts/build_clean_case_set.py`
- `scripts/run_replay.py` + `replay_set/cases.jsonl` + `clean_cases.jsonl`
- 4 hygiene commits in HarnessGate (KDD-specificity removal)
- This experiment log entry

### Next direction (proposed, not yet started)
**Planner-side response discipline** — instead of improving Judge:
- A. code_validator warnings → BLOCK (Planner must fix, not just warn)
- B. Reflexion-style mandatory natural-language reflection on retry
- C. Planner-side N-temp with disagreement detection
- D. Inject close-match suggestions as priority context for next attempt
- E. "Stuck detection" — same error 2× → force backtrack
- Audit first (0 LLM): count v80 tasks where code_validator gave actionable
  signal that was ignored. If ≥5, direction is justified.

---



**Architecture:** Profiler → Analyzer → Loop(PlannerCoder → Sandbox → Skeptic → Judge) → Finalizer

**Model:** Kimi k2.5 (Moonshot)

**Data:** `data/demo/` (old, pre-corrected gold answers)

**Known issues:**
- Skeptic: 0% effective, just wasted tokens and time
- sanity_checker: too loose, replaced by 13-rule HarnessGate
- No answer shape awareness before loop start

---

## v83 — D7 stuck-loop detection (2026-05-17) — REVERTED

**Hypothesis**: Phase 0.4 direction audit identified D7 (Planner submits
≥97% similar code across iterations) as the only candidate with ≥5
v80-task evidence (7 affected, 5 failed). Forcing pivot on detection
should recover 2-4 tasks.

**Implementation** (commit `f137381`):
- `_is_stuck_candidate(candidate, prior_codes, threshold=0.97)` helper.
- In orchestrator iteration loop, BEFORE candidate execution: if ALL
  candidates match a prior winning_code ≥0.97 similarity, skip
  execution, inject pivot guidance into `state.judge_guidance`.
- 8 unit tests; 291/291 + 1 skipped pass.

**Result**: **60.7%** (-6pp vs v80=66%). Reverted (`7266760`).

**Gains (+3)**: task_243 (also won under v82 — LLM variance, not D7),
task_257 (D7-flagged but stuck never triggered in v83; pure variance),
task_408 (not D7-related).

**Losses (-6)**: task_11, task_173, task_259, task_355, task_38, task_80
— all previously-passing tasks newly failing; bottlenecks spread across
unknown / sandbox / orchestrator / debugger / finalizer.

**Recovery on D7 audit-flagged tasks**:
| Task | D7 fired | v80→v83 | Outcome |
|------|----------|---------|---------|
| task_25  | 2 iters | 0 → 0 | No recovery despite trigger |
| task_89  | 1 iter  | 0 → 0 | No recovery |
| task_180 | 0       | 0 → 0 | Stuck not detected |
| task_257 | 0       | 0 → 1 | Variance, not D7 |
| task_352 | 0       | 0 → 0 | Stuck not detected |

D7 fired correctly on task_25 + task_89 but pivot guidance did not help
Planner find a new path. Forcing "try something different" does not
equip Planner with WHAT to try.

**Lesson — diffuse-failure hypothesis confirmed**:
| Experiment | Mechanism | Δ vs v80 |
|------------|-----------|----------|
| v81 rubric wholesale | Judge prompt change | -14pp |
| v82 #1+#2 selective | Judge routing + magnitude rule | -8pp |
| **v83 D7** | Orchestrator control flow | **-6pp** |

Three independent directions, each audit/research-validated, all net
negative on full eval. Loss tasks shift each time (no consistent
"regression set") — pattern consistent with LLM-variance amplification
under system perturbation.

**Conclusion**: v80 66% is the realistic ceiling for current architecture
(single-trajectory baseline) + Qwen 3B. Single-point optimizations cannot
break through. Productive next directions are architectural (heavy mode
default-on, K-trajectory ensemble already proven 71% on v70b) — not more
prompt or rule tweaks.

---

## v84 — B-fix 1 + 2 validator enforcement (2026-05-18) — REVERTED

**Hypothesis**: Phase 0.7+0.8 audit identified 5/7 v80 "persistent
struggle" failures as Planner ignoring deterministic signals. B-fix 1
BLOCKs execution on close-match column typos (5 audit cases); B-fix 2
emits soft warning + helper recipe when Planner uses raw json.load
(3 audit cases). Predicted recovery: 2-4 of these 7 tasks.

**Implementation** (commits `4c86aca` + `8299b75`):
- `validate_column_references` returns `(annotated_code, all_warnings,
  blocking_warnings)`. Orchestrator skips candidates with blocking
  warnings; if all candidates blocked, skips debugger and injects
  close-match suggestions into `state.judge_guidance`.
- New `_check_raw_json_load()` detects raw json.load on .json files
  without `safe_read_json_df` import; emits SOFT warning with helper
  recipe in code annotation.
- 11 new tests, 294/294 + 1 skipped pass.

**Result**: **60.5%** (-5.5pp vs v80=66%). Reverted (`9b19ffc` + 1 more).

**B-fix actual fires in v84**:
| B-fix | Audit predicted | Actually fired on | Recovered |
|-------|----------------|-------------------|-----------|
| 1 (close-match BLOCK) | task_199 | task_352, task_379 | 0 of those |
| 2 (json recipe) | task_163, 352, 418 | task_173 | 0 of those |

**Audit target recovery**: 0/4 (task_199, 163, 352, 418 all still failed).

**Gains**: +1 (task_408 — unrelated to B-fix targets, LLM variance).
**Losses**: -4 (task_11, 259, 355, 38 — all previously passing,
diverse bottlenecks, pure LLM variance).

**THE LESSON — audit-driven-tuning is fundamentally limited on
small evals**:
The v80-derived audit targeted Planner patterns specific to that
single run's LLM sampling. In v84, Planner produced DIFFERENT code on
the same tasks, so the validator's pattern-matchers didn't fire on the
expected targets. The 4 losses are pure LLM variance.

Pattern across 5 consecutive experiments:
| Experiment | Mechanism | Δ vs v80 |
|------------|-----------|----------|
| v81 rubric wholesale | Judge prompt | -14pp |
| v82 #1+#2 selective | Judge routing + magnitude | -8pp |
| v83 D7 stuck-loop | Orchestrator control flow | -6pp |
| **v84 B-fix 1+2** | **Validator enforcement** | **-5.5pp** |

5 different mechanism types (prompt / routing / orchestrator / validator),
5 carefully designed and tested, 5 net negative. **Diffuse-failure
hypothesis is now strongly supported by 5 independent data points.**

**Methodology constraint discovered**:
- N=1 trace analysis (audit on a single eval) cannot reliably predict
  intervention effect (Planner code varies across runs).
- To use audit reliably, would need N≥3 baseline runs per direction to
  identify which patterns reproduce.
- That's 3x cost per audit, prohibitive at our budget.
- Alternative: stop chasing single-point fixes on 50-task eval and pivot
  to (a) larger eval (DABstep + KDD Phase 2 hidden), (b) heavy-mode
  default-on (architectural override of noise), or (c) accept v80=66% as
  the stable ceiling and ship.

---

## v85/v86 — Input Hygiene + True Baseline (2026-05-19) — SHIPPED

### Discovery

Phase 0 V trace audit found Qwen reading `result.json`, `step_result.json`,
`intermediate_*.pkl` etc. from `public/input/task_*/` directories — files left
behind by prior eval runs that had written into TASK_DIR. N1 scan: 23/50
tasks had such artifacts.

Smoking gun: greps over `results/eval_v32*+/task_*/workspace/steps/`
showed Qwen code calling `open('.../public/input/.../output/result.json', 'w')`
since v32 (Apr 28). Self-reinforcing pollution loop confirmed.

### What shipped

| layer | commit | purpose |
|---|---|---|
| L1 prompt declares TASK_DIR read-only | `f2539de` | prevention |
| L2 Sandbox snapshot+detect+delete TASK_DIR writes | `20e4f8f` | enforcement |
| L3 Profiler output-convention blacklist + empty-DB detection | `d009c1d` | containment |
| L4 one-shot clean-up of 37 leaked files across 23 tasks | `20e4f8f` (script) | reset |

24 unit tests; full suite 307 pass.

### True baseline measurement (CRITICAL)

| eval | input | code | avg | ≥0.9 pass |
|---|---|---|---|---|
| v80 | polluted (had leaks) | original | **0.660** | 33/50 |
| v85 | polluted | + S1 (L3) only | 0.630 | 31/50 |
| v86 | **clean (post-L4)** | + L1+L2+L3 | **0.620** | 31/50 |

**The true baseline of our agent on clean Phase-2-equivalent input is 62%,
not 66%.** v80 = 66% was inflated by ~4pp from leak-derived signals
(filename hints like `severe_thrombosis_patients.csv` told Qwen the filter
condition).

### Implication for past reverted experiments

Every prior experiment was paired-evaluated against v80's inflated 66%.
A real +2pp improvement showed as -2pp when it conflicted with the
leak benefit. **Several reverted experiments may have been false negatives**:

| candidate for re-eval | revert SHA | why suspect |
|---|---|---|
| EXTRACT_MAPPINGS | `1c47a1e` → `b9690a2` | only 1-task validation, no full eval; same direction as H1 |
| identify-X-and-Y multi-column | `97490ea` → `6377e0b` | could've conflicted with leak schema hints |
| Cardinality+COUNT(DISTINCT) | `f036b06` → `b059246` | constrained SQL, leak benefit guided different SQL |

These deserve re-test against the v86 clean baseline before being permanently
dismissed.

### Lift signals (v80 → v86)

- task_408 0→1.0: empty-DB metadata works (Profiler now tags races.db /
  circuits.db / etc. as `empty=True` so PlannerCoder skips them).
- task_243 0→1.0: possible variance, not necessarily structural.

### Regression sources (v80 → v86)

- task_11 1→0: filename hint loss (`severe_thrombosis_patients.csv` previously
  told Qwen to filter `Thrombosis = severe`).
- task_173 1→0: v80's score came from heavy_t2 saving a timed-out baseline;
  v86 didn't get the same lucky variance.
- task_196 1→0, task_22 1→0: pure LLM variance (no leaks affected, also
  passed in v85). Within ±4-5 noise band.

### Honest position

True capability ≈ 62%, not 66%. Phase 2 submission expectation should be
calibrated against 62%. Any new direction needs to lift above this baseline.

Next-iteration substrate is in `docs/LEAK_TO_HONEST_INFO_MAP.md`:
- H1 (question entity → manifest match) — prototype works on task_352
- knowledge.md term-column binding — surfaces "Thrombosis=2 means severe"
  etc. that filename hints used to imply
- QuestionSpec output-column-names inference

---

## Phase 0 v3 Plan — Helpers / Decomposition Diagnostic (2026-05-18)

Adopted after red-team review of v3 matrix plan. **No code change yet** — this is the diagnostic gate that decides whether Phase 1 helper investment is justified.

### Frozen artifacts (locked before any experiment)

| Artifact | Path | Purpose |
|---|---|---|
| Train set (8) | `eval_split/train_8.txt` | May inspect trace + derive helpers |
| Holdout set (5) | `eval_split/holdout_5.txt` | Names only; never read trace |
| Taxonomy snapshot | `eval_split/TAXONOMY_SNAPSHOT.md` → SHA `43ad212` | Phase 1B may only reference this SHA |
| Skeleton rubric | `eval_split/SKELETON_RUBRIC.md` | Locks C1/C2 boundary before skeletons are written |

### Phase 0 items

| Item | What | Cost | Risk | Gate |
|---|---|---|---|---|
| P0-1 SC1-A | Opus + helpers reference code on 5-6 train tasks | Opus, 3h | 🟢 | Score≥0.9 of ≥4/6 |
| P0-2 SC1-B | Opus pure SQL/pandas (no helpers) | Opus, 1h | 🟢 | gap vs A judges helper value |
| P0-3 SC1-C1 | Qwen + API skeleton (per rubric) | ~$2, 1h | 🟢 | C1≈A → discovery gap |
| P0-4 SC1-C2 | Qwen + logic skeleton (per rubric) | ~$2, 1h | 🟡 | C2≈A & C1<<A → decomp gap |
| P0-5 | Trace attribution v81/v82/v83 magnitude rule | 0, 1h | 🟢 | Unlocks/blocks P3.1 |

### Phase 0 Decision Matrix

| A | B | C1 | C2 | Interpretation | Next |
|---|---|---|---|---|---|
| ≥4/6 | <A | ≈A | — | discovery gap | P1A + P1C |
| ≥4/6 | <A | <<A | ≈A | decomposition gap | P1A + scoped P2A |
| ≥4/6 | <A | <<A | <<A | reasoning/impl gap | P2A first |
| ≥4/6 | ≈A | — | — | helpers convenience only | skip P1B |
| ≥4/6 | ≥A | — | — | helpers actively hurt | audit existing helpers |
| <4/6 | — | — | — | taxonomy insufficient | redesign taxonomy |
| mixed | — | — | — | unclear | small P1A + P2A probe |

### Ship rule (each Phase 1+ ship)

- paired eval: `newly_fixed - regressions ≥ 2` OR overall Δ ≥+1.5pp
- holdout regression = 0 (else flag overfit, no ship)
- latency/cost: no unacceptable growth
- per-phase rollback: independent commit/flag

### Stop-loss

Phase 0 + Phase 1 total budget ≤ **$30** + ≤ **5 working days**. Over budget with holdout flat → abandon helper direction; re-audit from Phase 0' (trace attribution + new direction).

---

## Template (copy for each new run)

```
## Experiment: <name> (YYYY-MM-DD)

**Model:** <model>
**Data:** public/ (50 tasks, corrected gold answers)
**Config changes:** <what changed>

**Score:** X/50 tasks passing, avg score = X.XX
**Failures:** X tasks below 0.5

**Analysis:**
- Top failure pattern: <pattern>
- HarnessGate blocks: X total
- Avg iterations: X.X

**Next action:** <what to try next>
```
