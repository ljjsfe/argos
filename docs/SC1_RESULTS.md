# SC1 Results — Phase 0 Diagnostic

**Generated**: 2026-05-18
**Gate**: Score ≥0.9 on ≥4/6 tasks

> Discipline: this file records **only** pass/fail/data-gap counts.
> Per-task analysis lives in `eval_split/skeletons/task_<id>_NOTES.md` and is
> NOT a design input for Phase 1B. Phase 1B may only reference the frozen
> taxonomy SHA `43ad212` per `eval_split/TAXONOMY_SNAPSHOT.md`.

## SC1-A — Opus + helpers

| task | difficulty | score | helper-status |
|---|---|---|---|
| task_86 | easy | 1.0 | sufficient |
| task_163 | medium | 1.0 | sufficient |
| task_180 | medium | 1.0 | sufficient |
| task_344 | hard | 0.0 | NA — data-completeness gap, not helper gap |
| task_352 | hard | 1.0 | sufficient |
| task_418 | extreme | 1.0 | sufficient |

**Score-≥0.9 pass count: 5/6 → Gate ≥4/6 PASS**.

## SC1-B — Opus, no helpers

| task | score | vs A |
|---|---|---|
| task_86 | 1.0 | = (Opus replaced `safe_read_json_df` with manual `{"records": [...]}` unwrap) |
| task_163 | 1.0 | = (same unwrap pattern) |
| task_180 | 1.0 | = |
| task_344 | 0.0 | = (still data gap) |
| task_352 | 1.0 | = |
| task_418 | 1.0 | = (A had no helpers anyway) |

**Score-≥0.9 pass count: 5/6 = SC1-A**.

**Interpretation: A − B = 0**. Helpers are **convenience**, not **binding**,
when Opus reasoning is unlimited. Without helpers, Opus must hand-write
the JSON `records` unwrap (one extra line) but reaches the same answer.

Decision-matrix row: `A ≥4/6 AND B ≈ A` → "helpers convenience not binding"
→ **skip P1B, at most do P1A (docstring overhaul)**.

NOTE: this is only an Opus-arm signal. Qwen arms (C1/C2) may show a
different picture — if Qwen can't reliably figure out the unwrap on its
own, helpers become binding *for Qwen* even though they aren't for Opus.
That is the next probe.

## SC1-C1 — Qwen + API skeleton

Runner: `scripts/run_sc1_qwen.py --arm C1`. Two-stage:
- run the skeleton's PROBE block, capture stdout (col names + 3-row head)
- include probe output in user prompt so Qwen sees the actual schema
- single LLM call; Qwen returns the full filled script

| task | score | pred shape vs gold |
|---|---|---|
| task_86 | 0.0 | duplicates rows; no dedup |
| task_163 | 0.0 | 2 rows by category, not 1 row for October Meeting |
| task_180 | 0.0 | 0 rows; filter wrong |
| task_344 | 0.0 | answer = 3 (same as A — data gap, NOT a Qwen failure) |
| task_352 | 0.0 | ratio = 0.0; narrative parse failed |
| task_418 | 0.0 | count = 0; narrative parse failed |

**Score-≥0.9 pass count: 0/6**.

Net Qwen-attributable failures: **5/6** (task_344 excluded — data gap).

**Interpretation: C1 << A**. Even with API skeleton (helper calls already in
place, probe output appended), Qwen fails to fill in correct expressions on
5 of 6 tasks. The gap is NOT discovery — helpers are *handed* to Qwen.

The gap is therefore in:
- expression-level implementation (dedup, filter, groupby choice)
- narrative-to-structured parsing (task_352, task_418)
- multi-step reasoning over 3 tables (task_86, task_163)

This points to either (a) decomposition gap or (b) implementation gap.
SC1-C2 (logic skeleton with decomposition outline pre-provided) is the
diagnostic that separates the two.

## SC1-C2 — Qwen + logic skeleton

Same runner; skeleton uses NO helper calls, contains numbered step-by-step
decomposition + TODO expression placeholders. Probe output appended to prompt.

| task | score | failure mode |
|---|---|---|
| task_86 | 0.0 | wrong semantic mapping: chose `drivers.number` (car number) over `standings.position` |
| task_163 | 1.0 | ✓ |
| task_180 | 0.0 | type mismatch: filter `Date == '201208'` (string) but column is int |
| task_344 | 0.0 | answer = 3 (data gap, same as A — NOT a Qwen failure) |
| task_352 | EXEC_FAIL | regex group index error (incomplete regex spec) |
| task_418 | 0.0 | regex too loose; count = 31 (way over) |

**Score-≥0.9 pass count: 1/6**.

Net Qwen-attributable: 1/5 PASS (task_344 excluded — data gap).

## Headline decision-matrix reading

| arm | pass | Qwen-attributable pass |
|---|---|---|
| A | 5/6 | (Opus) |
| B | 5/6 | (Opus) |
| C1 | 0/6 | 0/5 |
| C2 | 1/6 | 1/5 |

- **A ≥ 4/6** ✓ — helpers + Opus reasoning are sufficient
- **B ≈ A** — helpers convenience-only (for Opus)
- **C1 << A** — discovery is NOT the gap (helpers handed in, still fails)
- **C2 ≈ C1, both << A** — decomposition outline does NOT substantially close
  the gap. Even with full step-by-step plan, Qwen still fails 4/5 attributable

**Matrix row: `A ≥4/6 AND B ≈ A AND C1 << A AND C2 << A` → REASONING/IMPL gap.**

Per planned matrix, this routes to "P2A probe first, P1 low priority".

**BUT the C2 = 1/5 result complicates this.** Upstream Decomposer (P2A) is
*literally what the C2 skeleton simulates* — full decomposition outline + named
subgoals + TODO expressions. If P2A's mechanism is "pre-decompose then let Qwen
fill", we already have evidence Qwen only fills correctly 1/5 of the time even
with that help. P2A as planned will NOT close the gap.

The four observed Qwen failure shapes on C2:
1. **Semantic concept → column mapping** (task_86: "track number" → `position`)
2. **Type-aware filter writing** (task_180: int vs str literal)
3. **Regex completeness** (task_352, task_418: incomplete capture groups, loose patterns)
4. (No "decomposition planning" failure observed — that's the diagnostic point)

These are *expression-level correctness* issues, not decomposition gaps.

## Reproducibility

```bash
for t in 86 163 180 344 352 418; do
  python3 eval_split/skeletons/task_${t}_A.py
done
```

Predictions land in `eval_split/skeletons/_pred_task_<id>_A/prediction.csv`
(gitignored). Source reference code is checked in at
`eval_split/skeletons/task_<id>_A.py`.

## Decision protocol (see experiment.md Phase 0 plan)

Once SC1-A/B/C1/C2 all complete, apply the decision matrix in
`experiment.md` to pick Phase 1 path. **Do NOT design helpers from
SC1 observations.** Phase 1B helpers must derive only from
`docs/QWEN_CAPABILITY_PROFILE.md` at SHA `43ad212`.
