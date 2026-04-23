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

**Eval:** Not yet run on v5.

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
