# v90 Interpretation Audit — verdict: MARGINAL (lean REAL-but-fragile)

## TL;DR

1. The DABstep task_49 lift is **causally attributable to B2** — the cache extracted a "Fraud Ratio" formula from `manual.md` and the v90 Planner's plan paraphrases it verbatim. Mechanism confirmed, not coincidence. But N=10 with +1 task is one event; we cannot exclude that the same Planner-variance that produced this lift could have produced it without B2 on another seed.
2. The KDD v88→v90 -2 task delta is **pure variance**: B2 broaden did not change which `.md` files were collected on KDD (knowledge.md was already covered), so any KDD score movement v88→v90 is not attributable to the v90 patch.
3. The holdout (0/4 → 0/4) gives **zero discriminative signal**. It is a guardrail that did not trigger, not evidence of non-overfit.
4. Developer claims "universal-SAFE confirmed, universal-LIFT marginal" — that framing is approximately right but the LIFT evidence is weaker than implied (N=1 task on N=10 benchmark).

---

## Findings

### F1 — DABstep task_49 lift IS causal, not coincidence  [Severity: positive, high-confidence]

**Evidence:**
- `data/dabstep/context/.dataline_cache/doc_glossary_857971a7ea15cab21cbd2b703c977282.json`: 22 terms, 2 formulas, 8 rules extracted from `manual.md`. Includes term `"Fraud" → "Ratio of fraudulent volume over total volume"` and formula `Fraud Ratio = fraudulent_volume / total_volume`.
- `results/eval_v90_dabstep_20260519_2110/49/trace.json`: trace event `doc_glossary | 22 terms; 5 hints` confirms hints emitted into PlannerCoder context.
- v90 Planner plan (trace.json): *"Calculate fraud ratio (fraudulent volume / total volume) by ip_country from payments.csv"* — direct paraphrase of B2 formula.
- v90 winning code (`workspace/steps/step_0_code.py`): computes `SUM(fraud_volume) / SUM(total_volume)` per `ip_country`, returns BE = gold.
- v88 winning code (`eval_v88_dabstep_ood_20260519_2027/49/workspace/steps/`): `COUNT(*) WHERE has_fraudulent_dispute` → returned NL. v88 had no DABstep B2 cache because the old hardcoded glob did not match `manual.md`.

**Conclusion:** B2 hints changed the Planner's metric choice from count to ratio. The lift is mechanism-driven, not a coincidence.

### F2 — KDD v88→v90 deltas are pure variance, not B2 effect  [Severity: high]

**Evidence:**
- `0285609` commit body explicitly states "KDD coverage unchanged" — the broadened glob is a superset of the old KDD-targeted glob; every KDD task had `knowledge.md` already covered in v88.
- Scoring computed locally with `dataline.eval.scorer.score_task` over `public/output/*/gold.csv`:

| Run | Passed (binary) | Sum-score | Avg |
|---|---|---|---|
| v86 clean | 31/50 | 31.00 | 0.620 |
| v87 (A3+A1+A2) | 35/50 | 36.00 | 0.720 |
| v88 (+B2) | 37/50 | 37.90 | 0.758 |
| v90 (B2 broaden + A2 FN fix) | 35/50 | 35.33 | 0.707 |

- v88→v90 binary task diffs:
  - Regressions (1→0): task_173, task_25, task_352, task_218 (1→0.33)
  - Lifts (0→1): task_196, task_330 (0.40→1)
- All six tasks have `knowledge.md` already present in v88; only task_352 and task_330 add `context/doc/*.md` — those were also covered by the old v88 glob (`context/doc/*.md` was in the hardcoded list). So **no KDD task gained or lost B2 coverage in v90**.
- The 4-vs-2 swing must be Heavy-mode LLM variance (different deliberator picks at temps 0.7/0.7), not the B2/A2 patch.

### F3 — Statistical posture: nothing is significant; v86→v90 is the only directional signal worth tracking  [Severity: high]

Two-proportion z-tests (50-task binomial):

| Comparison | Δ | z | p-value | Conclusion |
|---|---|---|---|---|
| v90 (35) vs v86 (31) | +4 | 0.84 | 0.40 | Not significant. At the edge of CLAUDE.md's "±4-5 noise band" |
| v90 (35) vs v88 (37) | -2 | -0.45 | 0.66 | Inside noise band; no real regression |
| v88 (37) vs v86 (31) | +6 | 1.28 | 0.20 | Suggestive but not significant |

The clean A3+A1+A2+B2 stack (v87→v88) shows +6 vs the v86 hygiene baseline — directionally encouraging — but at α=0.05 we cannot reject H0 that all four runs (31, 35, 37, 35) are draws from the same underlying success rate. Per CLAUDE.md noise floor (±4-5 tasks at temp=0; heavy adds more), **no single ≤6-task delta on this eval is decision-grade**.

DABstep: 1/10 → 2/10 has nominal p ≈ 0.50 under Fisher exact — meaningless statistically, but F1 establishes the lift is mechanism-driven, so the right reading is "first transfer evidence of correct *kind*, sample size grossly insufficient for *magnitude*".

### F4 — Holdout has zero discriminative power right now  [Severity: high]

- All 4 holdout tasks score 0 across v86, v87, v88, v90 (aggregate only, per discipline).
- Zero-variance holdout is a **guardrail that did not trigger**, not a positive non-overfit signal. It rules out catastrophic-regression on these specific 4, nothing more.
- Cause is structural: the 2026-05-19 shrinkage to N=4 plus a difficulty mix where all 4 are currently 0-on-baseline means the holdout cannot show a positive lift OR a negative regression unless a change is large enough to move a 0-task across the threshold. This is consistent with the "Accepted N=4 holdout" note in `eval_split/holdout.txt`.
- The developer should not cite "holdout 0/4 → 0/4" as evidence of universality. It is consistent with universality AND consistent with the v90 patch having any effect ≤ 1 holdout-task in magnitude.

### F5 — Universality claim, three lenses  [Severity: medium]

- **Mechanism / substitute-name test:** PASS. The v90 B2 glob is filename-agnostic (`*.md` under task_dir + context + doc roots); the A2 rewrite removes the `"description"` filter that was a KDD-shaped heuristic. Both pre-existing audits (V87, PRE_V88) verified the substitute-name and inverse-domain criteria.
- **Cross-benchmark transfer:** WEAK. One DABstep task lifted by clear mechanism (F1). But the same B2 cache was present on all 10 DABstep tasks and only 1 lifted (49 was easy: a multiple-choice question whose options literally hint at the metric). 8/10 DABstep tasks failed on causes orthogonal to glossary hints (1273/1305 = numeric precision; 1464/1681/1753 = wrong row-set; 1871 = wrong column; 2697 = wrong aggregation level; 70 = wrong null-policy). **B2 helps when the question hinges on a documented definition; that condition was rare in this benchmark slice.**
- **Coverage:** Probably adequate for the `.md` doc convention. Misses: PDF docs, README inside subdirectories not in (task_dir, context, doc, context/doc), reST/asciidoc, JSON sidecar glossaries, table-only docs. None of those are present in the two benchmarks we have, so the gap is undetected, not absent.

### F6 — What the developer is overclaiming / could be fooling themselves about  [Severity: medium-high]

- **"DABstep +1 is the first empirical universal-LIFT signal"** — True in *direction*, weak in *magnitude*. F1 supports the causal claim (mechanism-driven), but a single task on N=10 cannot calibrate the rate at which B2 transfers. The honest framing: "first mechanism-confirmed lift outside KDD; magnitude unknown until N≥30 transfer benchmark."
- **"KDD v90 = -2 within noise"** — Correct, but the developer should add: *and the v88→v90 task diffs are not attributable to the v90 patch because B2 coverage on KDD did not change*. This matters because if a future v91 narrows DABstep coverage and KDD swings ±3 tasks, that swing is still pure noise — repeated runs are needed to separate signal.
- **"task_49 newly PASS (5 hints emitted)"** — Both true and causally connected (F1). Not overclaim. But "5 hints" alone is not the mechanism; the *content* of those hints (Fraud Ratio formula) is. If a future B2 change ever emits 5 hints with different content, the lift may not survive.
- **"Holdout clean: 0/4 → 0/4"** — Vacuous. See F4. Reporting it as evidence is misleading.
- **The Heavy-mode confound:** All four runs are with Heavy mode on. CLAUDE.md notes Heavy adds variance beyond the temp=0 baseline. The ±4-5 noise band may be optimistic for these runs; the true noise on Heavy-Heavy comparisons could be wider.

---

## What the developer is RIGHT about

- Mechanism design (substitute-name, inverse-domain, pre-existence) genuinely passes — confirmed by prior audits and by inspection of the 0285609 diff being filename-agnostic.
- v90 patch is "universal-SAFE": no KDD coverage was lost, only added. Score movement on KDD is unattributable to v90 and therefore not a regression of v90.
- B2 cache IS extracting and emitting glossary content (22 terms / 5 hints), and that content IS landing in the Planner prompt (verified in trace.json).
- Calling LIFT "marginal" rather than "confirmed" is the right epistemic temperature.

## What the developer is WRONG or OVERCLAIMING about

- Treating the holdout as evidence of non-overfit (it has no power on a 4-task all-zero baseline).
- Implicit linkage of KDD score movement (v88→v90 -2) to the v90 patch — by their own commit message, KDD coverage was unchanged, so KDD deltas are pure variance and should be reported as such.
- Calling DABstep +1 a "universal" signal without acknowledging that only 1/10 DABstep tasks was the *kind* of question B2 can help (definition-hinge); the other 9 failures are orthogonal to glossary hints, so the transfer rate is undefined.

## Recommended next steps (in priority order)

1. **Re-run KDD v90 with a different seed (or temp 0 baseline)** to separate variance from signal. If v90 lands in [33, 37] again, the v88→v90 -2 is confirmed noise. Budget: 1 eval run. This is the single highest-value next action.
2. **Expand the DABstep slice from 10 to ≥30 tasks** (use the full DABstep dev set if available). Without this, the B2-transfer rate cannot be estimated, and any future B2 tuning has no measurement substrate.
3. **Replace or augment the holdout.** A 4-task all-zero holdout has no statistical power. Either (a) substitute in tasks where baseline passes ≥1, or (b) accept that holdout is a binary regression detector only and stop reporting it as universality evidence.

## What additional N=? runs would reduce ambiguity to acceptable confidence

- **KDD variance characterisation:** 3 repeat runs of v90 at heavy mode → estimate per-task variance and a confidence interval on the underlying pass rate. Cost: ~3× current KDD budget.
- **DABstep transfer-rate estimate to ±5pp:** need ≥80 DABstep tasks scored under v90, ideally with a held-out doc-glossary subset to estimate per-question-type lift. Cost: 8× current DABstep budget.
- **Universality decision-grade:** add a third benchmark with documented-domain conventions distinct from both KDD (knowledge.md) and DABstep (manual.md) — e.g. a financial filings dataset, a clinical trial doc set. Without a third benchmark, "universal" remains a 2-point line that cannot distinguish "works on docs" from "works on these two doc styles".

---

**Bottom line for ship gate:** v90 is safe to ship as a hygiene improvement (universal-SAFE confirmed). The "universal-LIFT" claim should be downgraded in the changelog to "first mechanism-confirmed cross-benchmark lift, N=1 task, magnitude not yet measurable". Do not let task_49 anchor expectations for future doc-bearing benchmarks.
