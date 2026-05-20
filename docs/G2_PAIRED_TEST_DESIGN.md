# G2 Paired-Test Design — Planner Prompt Aggregation Sanity Hint

**Status**: DESIGN (no code change yet)
**Author**: dataline iteration session 2026-05-20
**Goal**: Add an "aggregation-result sanity" hint to `planner_coder.md` so Planner reflexively double-checks filter literals when an aggregation question would return 0 / null / empty.
**Driving evidence**: G2 trace audit found P1 (`filter_no_effect`) fires too late in iter budget (iter 6-8 of 8) on tasks 352/396/418. Hint at iter 0 would prevent the loss instead of catching it after budget exhaustion.

---

## 1. Hypothesis

**H0 (null)**: Adding the hint changes no per-task scores beyond baseline LLM variance (≤ ±4 task net).
**H1 (proactive prevention)**: At least 1 of {task_352, 396, 418} flips from 0 → ≥0.5 due to Planner pre-checking literals on iter 0. No more than 1 perfect task regresses to <1.0.

If H1 holds → ship to full eval.
If both fail → don't ship; document why.

---

## 2. Proposed prompt change (exact diff)

Insert into `dataline/prompts/planner_coder.md`, **at end of Step 2** (between line 27 "use `LOWER()` for case-insensitive matching" and line 29 "### Step 3 — Write the query"):

```markdown
Filter literal sanity (BEFORE running):
- When the question requires aggregation with a filter ("how many X where Y", "percentage of A among B", "ratio of X to Y"), enumerate every WHERE literal in your draft against the manifest DISTINCT values. If a literal is not in the DISTINCT list (case/spelling/whitespace), the filter will silently match zero rows and your aggregation will return 0 / null / NaN.
- For numeric threshold filters ("above X", "abnormal level"), confirm the threshold value comes from the `Domain Knowledge` section. If no threshold is documented and the question implies one, state the assumption explicitly via `assume_then(...)` before using it.
```

**Word count**: ~80 words. Adds ~6 lines. Estimated prompt-bloat impact: minimal (current prompt is 91 lines, ~700 words; this is ~10% addition).

**Critical constraint**: the hint must NOT mention specific tasks, columns, or domain values. Universal SQL/data hygiene framing only — same discipline as the HarnessGate rule audit.

---

## 3. Paired-test set definition

### Target set (8 tasks — should improve or stay same)

Tasks where the hint's principle directly applies:
| task | difficulty | v95 score | Why this task |
|------|-----------|-----------|---------------|
| task_352 | hard | 0.00 | ratio=0, P1 fires iter 7/8 |
| task_396 | hard | 0.00 | percentage=0, P1 fires iter 1/2 |
| task_418 | extreme | 0.00 | count=0, threshold (creatinine) issue, P1 fires iter 6-8 |
| task_344 | hard | 0.00 | COUNT(*) vs COUNT(DISTINCT) but root is threshold filter (WBC normal range) |
| task_80 | easy | 0.00 | wrong filter value chosen (driver number) |
| task_89 | easy | 0.00 | wrong rank/filter |
| task_180 | medium | 0.00 | filter on "more than 29 per unit" — threshold |
| task_249 | medium | 0.00 | aggregation choice problem (SUM vs AVG with filter) |

**Targeted lift expected**: 2-3 tasks flipping to ≥0.5 (per G2 finding that the iter-budget issue is real).

### Control set (8 tasks — must stay perfect)

Random sample of v95 perfect tasks across difficulty levels:
| task | difficulty | v95 score | Why this task |
|------|-----------|-----------|---------------|
| task_11 | easy | 1.0 | easy lookup |
| task_25 | easy | 1.0 | medium SQL aggregation |
| task_38 | easy | 1.0 | regressed in v96 (LLM variance sentinel) |
| task_140 | medium | 1.0 | standard count |
| task_257 | medium | 0.5 → 1.0* | compound question (partial in v95, perfect in some runs) — sentinel |
| task_283 | medium | 1.0 | filter+aggregation (target shape) |
| task_330 | hard | 1.0 | P2 lifted this in v93+ |
| task_408 | hard | 1.0 | recovered in v93+ |

*task_257 is borderline; if it stays at 0.5 in both arms, that's not a regression.

**Failure criterion**: more than 1 perfect-set task drops below v95 baseline by ≥0.5.

---

## 4. Run procedure

### Arm A: current planner_coder.md (baseline)
```bash
SHA=$(git rev-parse HEAD)  # = ef02ebd or later
STAMP=$(date +%Y%m%d_%H%M)
OUT="results/g2_paired_A_${STAMP}"
mkdir -p "$OUT"; echo "$SHA" > "$OUT/GIT_SHA.txt"
nohup python -u main.py batch --benchmark kdd --data public \
  --output "$OUT" --heavy-mode auto \
  --tasks task_80 task_89 task_180 task_249 task_344 task_352 task_396 task_418 \
          task_11 task_25 task_38 task_140 task_257 task_283 task_330 task_408 \
  > "$OUT/_launch.log" 2>&1 &
```

### Arm B: with G2 hint applied
```bash
# 1. Apply the diff to planner_coder.md
# 2. Run same command with OUT="results/g2_paired_B_${STAMP}"
# 3. Do NOT commit the prompt change until B finishes and gates pass
```

**Wall time**: ~10 min/arm (16 tasks × ~30s avg, parallel=8). Total: ~20 min.
**Cost**: ~$8/arm = ~$16 total.
**Versus**: $44 + 50 min for two full 50-task evals. Saves 64% cost.

### Critical discipline
- Both arms run on identical SHA except the prompt diff. No other changes.
- Both arms run consecutively (back-to-back) on same eval machine to minimize environmental drift.
- Do NOT commit the prompt change before paired-test passes.

---

## 5. Decision matrix

Compute `delta[task] = score_B[task] - score_A[task]`.

| Outcome on Target set (8 tasks) | Outcome on Control set (8 tasks) | Decision |
|---------------------------------|----------------------------------|----------|
| `+net ≥ 2.0` (≥2 tasks lifted) | `-net ≥ -0.5` (≤1 minor regression) | **SHIP** to full v97 eval |
| `+net 0.5 to 2.0` (1 task lifted, marginal) | `-net ≥ -0.5` | **AMBIGUOUS** — run second paired (different seed) before deciding |
| `+net < 0.5` (no lift) | any | **REJECT** — hint had no effect, don't add prompt bloat |
| any | `-net < -0.5` (≥2 perfect regressed) | **REJECT** — variance amplification too high |

**Net definition**: `sum of deltas` (positive = lift; negative = regression).

---

## 6. Numerical predictions (predict-then-verify)

| Metric | Predicted value | Falsification threshold |
|--------|-----------------|-------------------------|
| Target-set net delta | +2.0 to +3.0 (≈2.5 tasks lifted) | < +1.0 → hypothesis falsified |
| Control-set net delta | -0.5 to +0.5 (within noise) | < -1.0 → variance issue |
| Cost vs Arm A | +0-5% (slightly longer iters as Planner adds check step) | > +20% → prompt counterproductive |
| Iter count vs Arm A | similar or fewer (proactive check saves retry iters) | > +30% → prompt confuses Planner |

---

## 7. Risk assessment

### Known risks (mitigation noted)

| Risk | Mitigation |
|------|------------|
| Prompt bloat hurts 3B-active small model | 6-line addition only; control set catches regression |
| Hint overfits to KDD shapes (Cluster A specifically) | Hint uses universal SQL phrasing; no task names/columns. Verifiable via re-read |
| LLM variance dominates 16-task sample | If borderline → second paired run with different seed |
| Hint actually triggers MORE WHERE-literal exploration → more iters → budget exhaustion | Tracked via iter count metric (Section 6) |

### Unknown risks (accept + monitor)

- **Interaction with P1 WARN**: when Planner now pre-checks AND P1 fires, the two signals may overlap or confuse. Inspect 2-3 traces post-run.
- **Cross-task transfer**: prompt change applies to all tasks, not just aggregation. Could subtly shift Planner reasoning on non-target tasks. Sample 3 random perfect tasks for trace audit.

---

## 8. Falsifiable claim (the AHE-borrowed receipt)

Before running Arm B, lock the following commit text:

```
EXPECTED:
- Target set net delta: +2.5 ± 0.5 tasks
- Control set net delta: 0 ± 0.5 tasks
- 2-3 of {task_352, 396, 418} should flip 0 → ≥0.5

WILL REJECT IF:
- Target net < +1.0 → hint has no effect
- Control net < -0.5 → variance amplification (v94-style)
- Iter count rises > +20% → hint counterproductive
```

Result must be inserted as Section 9 below, with actual numbers from Arm A/B.

---

## 9. Results (to be filled after run)

_PENDING — fill after paired-test runs._

| | Predicted | Actual | Hit? |
|---|---|---|---|
| Target net | +2.5 ± 0.5 | TBD | TBD |
| Control net | 0 ± 0.5 | TBD | TBD |
| Decision | TBD | TBD | TBD |

---

## 10. If paired-test passes

1. Apply the prompt diff to `planner_coder.md`.
2. Commit with the locked prediction text from Section 8 + actual results from Section 9.
3. Push.
4. Run full v97 KDD eval ($22, 25 min) — confirm the lift survives at scale (50-task variance can absorb sub-task signals; we need ≥+2 task net to be confident).
5. If full eval shows ≥+2 task → update CLAUDE.md baselines + ship floor.
6. If full eval shows <+1 task → revert; document why paired-test was over-optimistic.

## 11. If paired-test fails

Three failure modes, three responses:

| Failure | Response |
|---------|----------|
| Target net < +1.0 | Don't ship. Hint had no effect. Possibilities: hint phrasing too vague, Planner doesn't read it, or iter budget too tight regardless. Document; consider injecting via ContextManager priority instead of prompt body |
| Control regressed > -0.5 | Don't ship. Prompt bloat hurt small model. Possibilities: try shorter hint (3 lines instead of 6), or move to debugger prompt only |
| Iter count surged | Don't ship. Hint causes over-exploration. Pull back. |
