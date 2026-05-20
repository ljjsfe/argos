# Phase 0 SC1 — Findings & Path Forward

**Captured**: 2026-05-18
**Status**: diagnostic complete; no code change yet; awaiting direction decision

## 1. Raw data

### 1.1 Arm results

Setup: 6 train tasks (`eval_split/train_8.txt` — subset selected per `SKELETON_RUBRIC.md`).

| arm | what it measures | pass | Qwen-attributable |
|---|---|---|---|
| A — Opus + helpers | helper expressiveness ceiling | 5/6 | n/a |
| B — Opus no helpers | helper binding for Opus | 5/6 | n/a |
| C1 — Qwen + API skeleton (helpers wired, probe output appended) | discovery / invocation gap | 0/6 | **0/5** |
| C2 — Qwen + logic skeleton (decomposition wired, no helpers) | decomposition gap | 1/6 | **1/5** |
| C3 — Qwen + C2 skeleton + self-verify system prompt | prompt-level self-check sufficiency | 1/6 | **1/5** |

Note: task_344 is excluded from Qwen-attributable counts on every arm because
the gold (4) is unreachable from given data — A also fails it. Data-completeness
issue, not capability.

### 1.2 The 4 Qwen-attributable C2 failure shapes

| task | failure | universal pattern |
|---|---|---|
| task_86 | mapped "track number" → `drivers.number` (car number) instead of `standings.position` | semi-domain word → wrong column |
| task_180 | filter `Date == '201208'` (string literal) but column dtype is int → 0 rows | mixed-dtype filter literal |
| task_352 | regex constructed without expected capture group → IndexError at .group(1) | regex completeness |
| task_418 | regex too permissive → captured unrelated numbers; count = 31 | regex precision |

The 1 C2 pass (task_163) is the only one whose only difficulty is multi-table join — no semantic mapping, no type mismatch, no narrative parse.

### 1.3 Cross-check vs production v80

All 6 train tasks scored 0.0 in v80 baseline. So both single-shot SC1 (no
agent loop) and production multi-iter agent loop (with HarnessGate +
Judge + retry) fail on the same tasks.

## 2. Interpretation

### 2.1 What the matrix originally predicted

`A ≥4/6 AND B ≈ A AND C1 << A AND C2 << A` → "reasoning/impl gap, run P2A
probe first".

### 2.2 Why that prediction is invalid

P2A as planned = inject upstream Decomposer that pre-decomposes the task
into subgoals before Planner writes code. **The C2 skeleton is essentially
that output, hand-fed to Qwen.** C2 = 1/5 Qwen-attributable pass.

P2A therefore cannot deliver more than ~1/5 fix rate on the stable
failures. The plan's path to ship is empirically broken.

### 2.3 Why production agent loop doesn't recover

Production has multi-iter retry + HarnessGate + Judge. Yet all 6 fail in
v80. The likely reason: HarnessGate has no rule that fires on the 4
failure shapes above (semantic mapping, dtype filter, regex completeness,
regex precision). Judge accepts plausible-looking wrong answers because
the Qwen output looks structurally valid.

So **the gap is not in planning or discovery; it is in mid-execution
correctness signals — and the production loop has no signals to catch
the shapes we see**.

## 3. What is ruled OUT by SC1

| Phase 1/2 item | Ruled out by | Why |
|---|---|---|
| P1A docstring overhaul (helper usage examples) | B ≈ A | helpers are convenience, not binding |
| P1B new gap-fill helpers | C1 = 0/5 | Qwen can't use helpers handed in directly |
| P1C feature router | C1 = 0/5 | discovery isn't the bottleneck |
| P2A upstream Decomposer | C2 = 1/5 | C2 simulates exactly P2A's mechanism |

## 4. What is NOT ruled out (post-C3 update)

C3 ran on 2026-05-18 and produced **1/5 — same ceiling as C2**.

So three different prompt-level interventions (discovery / decomposition /
self-verify) all hit the same 1/5 ceiling. **Prompt-engineering is not
the lever**. Qwen partially applies self-check rules but applies them
inconsistently, and the fundamental semantic-mapping errors persist
because the self-check itself starts from the wrong column choice.

Remaining untested directions:

| direction | what it would test | next step |
|---|---|---|
| **HarnessGate expression-correctness rules** | does adding deterministic post-execution checks (filter-no-effect, dtype-mismatch, regex-low-match) catch the failure shapes in production loop? | check history first (#4 pending), then design |
| **Auto-injected probe wrapping** | infrastructure rewrites code so every filter/merge prints before/after row count; Qwen sees its own mistake mid-execution | larger lift |
| **Multi-iter feedback with specific diagnostics** | when HarnessGate fires "filter returned 0 rows", does Qwen recover in iter 2 if given specific dtype info? | needs orchestrator probe |
| **Acceptance**: these are Qwen-3B-active ceiling | declare 5/13 stable fails permanent | only after exhausting infrastructure options |

## 5. Statistical caveat

N = 6 is thin. The 4 failure shapes look universal but could be a sampling
artifact. To strengthen the claim, the cheapest test is:

- run A/B/C1/C2 on the remaining 2 train tasks (`task_169`, `task_173`)
- plus 5 random non-fail tasks from public/ for false-positive control

This is the "X" option from the discussion; deferred.

## 6. Decision rules to apply

Before committing to any new direction:

1. **Generality test**: any new helper / rule / infrastructure piece must
   pass the substitute test, inverse-domain test, AND pre-existence test
   (see `eval_split/SKELETON_RUBRIC.md` discipline).
2. **History check**: grep `git log --oneline -- <file>` for the same idea
   shape before re-implementing. Specifically known re-prone areas:
   - HarnessGate rule additions (multiple reverted)
   - Judge prompt augmentations (rubric v81/v82 reverted)
   - Self-check loops (CoVe family — Phase 2.4 was pending, never tried)
3. **Holdout discipline**: holdout.txt names are never read; any
   intervention must wait until P1V holdout validation before ship.
4. **Stop-loss**: total Phase 0+1 budget ≤ $30, ≤ 5 working days.

## 7. Open questions for next session

1. Does prompt-level self-verify (C3) recover anything? (answered by
   running Y next)
2. Is the 4-failure-shape pattern stable when SC1 is extended to N=11?
   (deferred to X)
3. Why does HarnessGate not fire on these failure shapes in production —
   missing rules, or shapes are genuinely hard to detect deterministically?
4. Is task_344 truly a data-completeness failure, or did the gold
   reference data we don't have? (orthogonal probe)

## 8. Reproducibility

All skeletons + Qwen outputs + predictions checked in:
- `eval_split/skeletons/task_{86,163,180,344,352,418}_{A,B,C1,C2}.py`
- `eval_split/skeletons/task_*_{C1,C2}_output.py`
- `_pred_*` dirs are gitignored (regenerable)

To re-run any arm:
```bash
# Opus arms — manual execution per file (A,B are reference code)
python3 eval_split/skeletons/task_86_A.py

# Qwen arms — set LLM env, run script
set -a && source .env && set +a
python3 scripts/run_sc1_qwen.py --arm C1 --tasks 86,163,180,344,352,418
python3 scripts/run_sc1_qwen.py --arm C2 --tasks 86,163,180,344,352,418
```
