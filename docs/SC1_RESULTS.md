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

_Pending_

## SC1-C2 — Qwen + logic skeleton

_Pending_

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
