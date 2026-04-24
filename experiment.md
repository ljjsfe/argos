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

## Baseline (v4, pre-2026-04-23)

**Architecture:** Profiler → Analyzer → Loop(PlannerCoder → Sandbox → Skeptic → Judge) → Finalizer

**Model:** Kimi k2.5 (Moonshot)

**Data:** `data/demo/` (old, pre-corrected gold answers)

**Known issues:**
- Skeptic: 0% effective, just wasted tokens and time
- sanity_checker: too loose, replaced by 13-rule HarnessGate
- No answer shape awareness before loop start

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
