# Architecture Decision Records

This file tracks architecturally significant decisions for the dataline
data agent. Each ADR captures the context, the decision, and what would
trigger reconsideration.

---

## ADR-001 — Harness-owned verification (over agent self-attestation)

**Date**: 2026-05-03
**Status**: accepted
**Inspired by**: 陆徐洲, *Harness Engineering 实战*, Ch.7 (verification layer)

### Context

Two failed experiments showed that adding LLM-based verification *as the
agent* (Skeptic v53, prompt audit v60) regressed scores. The article's
empirical evidence: 60% of agent self-attested "task done" claims are
incorrect when no harness verification exists; harness-owned
verification (run pytest then feed failure back) lifts pass rate from
40% to 100% on Python bug fixing.

For data analysis we cannot run pytest, but the principle generalizes:
**verification responsibility belongs to the harness, not the agent**.

### Decision

Adopt three coordinated harness-side verification components, deployed
together (not piecemeal):

1. **C1 Multi-candidate Consensus** — execute all generated code
   candidates, compare answers, BLOCK on disagreement.
2. **C2 Plan Artifact (Mode A)** — for hard questions, write a
   persistent plan.md, agent reads/updates it across iterations.
3. **C3 Invariant Verifier** — deterministic post-Judge rules
   (percentage range, count integer, non-zero aggregate).

Each component shipped as its own commit + tag, but evaluated together
on KDD. Bisect via tag-revert if regression.

### Consequences

Positive
- Verification logic is auditable and deterministic where possible.
- Failure modes are testable (each component has unit tests).
- Bypasses the "agent verifies itself" failure mode that caused
  Skeptic and prompt-audit regressions.

Negative
- ~+30% sandbox time from running all candidates (vs first-success).
- ~+5-10% prompt tokens on hard questions (plan.md + finding fields).
- One additional LLM call per hard question (decomposer).

### Reconsideration triggers
- KDD eval shows Δ < +3 across two runs after all three components ship.
- Specific harness rule (e.g., I3) causes deterministic regression on
  a previously-passing task.
- Agent compliance with plan.md updates < 30% (i.e., agent ignores
  the artifact most of the time).

### Reference
`docs/HARNESS_VERIFICATION_DESIGN.md`

---

## ADR-002 — No reliance on benchmark difficulty labels

**Date**: 2026-05-03
**Status**: accepted

### Context

Initial design read `task.json["difficulty"]` (KDD) and
`dev_tasks.json[].level` (DABstep) to gate Plan Artifact triggering.
This couples a "general-purpose data agent" to benchmark metadata.

A real user does not annotate "this question is hard" before asking
it. Any deployment scenario lacking these labels would silently lose
the Plan Artifact mechanism. That is a leakage of benchmark structure
into the core architecture.

### Decision

Difficulty classification is computed **only from the question text**
via a regex/heuristic in `dataline/agents/question_analyzer.py`:

```python
def estimate_difficulty(question: str) -> str:
    """Returns 'easy' | 'medium' | 'hard'.  Universal — no benchmark
    label dependency."""
```

Triggers:
- Plan Artifact: difficulty == "hard"

Benchmark difficulty fields, where they exist, are used only by eval
diagnostics (per-difficulty score breakdown), never by the agent's
decision logic.

### Consequences

Positive
- Agent generalizes to any question source (real users, new
  benchmarks, custom datasets).
- Clean architectural boundary: benchmark metadata stays in eval, not
  in agent.

Negative
- Heuristic may misclassify (false positives waste a decomposer call;
  false negatives lose Plan Artifact benefit). Acceptable for a
  general agent; benchmark labels could only ever match their own
  taxonomy anyway.

### Reconsideration triggers
- Heuristic accuracy on labeled benchmarks falls below 75%
  (over-trigger or under-trigger).
- A specific real-world deployment provides reliable difficulty input
  via a well-defined channel (not a benchmark file).

---

## ADR-003 — Plan Artifact: Mode A (advisory) before Mode B (per-step loop)

**Date**: 2026-05-03
**Status**: accepted (Mode A); deferred (Mode B)

### Context

Two implementations of the Plan Artifact pattern were considered:

**Mode A** (advisory): Plan written to plan.md before main loop;
PlannerCoder reads it as context each iteration; updates it after each
successful save_result. Iter loop unchanged.

**Mode B** (strict): Each plan step runs its own complete iter loop
(Plan→Code→Sandbox→Harness→Judge per sub-question). Inter-step
results passed via REPL state.

### Decision

Implement **Mode A only** for now.

### Rationale

Empirical and practical:
- The article (P_doc 100%) describes an artifact-based plan, not
  per-step independent verification — Mode A matches.
- 3B-active model coherence over many sub-question iters is uncertain;
  Mode B doubles the surface area for the model to lose track.
- Mode A is fail-soft: if agent ignores plan, behavior degrades to
  baseline. Mode B failure mode (sub-question decomposition errors
  compound) is harder to recover from.
- Implementation cost: Mode A ~5h, Mode B ~3 days due to orchestrator
  loop restructuring.

### Consequences

Positive
- Lower risk, faster to ship.
- Plan artifact infrastructure shared with future Mode B.

Negative
- Mode A relies on agent compliance with the plan it sees; agents may
  ignore plan and just write what they want. Article suggests this
  works at P_doc 100%, but sample size small.

### Reconsideration triggers
- KDD/DABstep show Mode A gives Δ < +3 after two runs.
- Agent compliance with plan.md (measured via plan-step alignment in
  trace) < 50%.
- Stronger underlying model (e.g., qwen-vl-plus, GPT-class) becomes
  available — Mode B's higher coherence demand becomes feasible.

### Migration path

The PlanArtifact data type and plan.md file format are designed to be
reused by Mode B. Adding Mode B = adding an alternative orchestrator
loop that consumes plan_artifact.steps as iteration boundaries; no
data type changes.

---

## ADR-004 — Difficulty classification: LLM judgment over regex heuristic

**Date**: 2026-05-03
**Status**: accepted
**Supersedes**: regex-based `estimate_difficulty` proposed in early
HARNESS_VERIFICATION_DESIGN drafts.

### Context

Plan Artifact triggering needs a binary "is this hard?" decision per
task. Two implementations considered:

- **Regex heuristic**: counts nested clauses, modal verbs, length.
  Zero LLM cost, deterministic, ~70-75% accuracy.
- **LLM judgment**: one small LLM call seeing question + manifest
  summary. ~$0.005/task, ~80-85% accuracy, sees semantic + schema
  signals regex cannot.

The LLM approach can detect cases regex misses, e.g., a short
question like "list ID, sex, disease for severe thrombosis patients"
which is structurally simple but semantically requires a cross-source
join over Examination + Patient — the LLM sees the manifest, regex
does not.

### Decision

Implement **LLM-based difficulty classification** as a single small
LLM call per task. Discipline:
- `temperature=0` for stability.
- Output strictly typed: "easy" | "medium" | "hard". Any other
  response fails to "medium" (does not trigger Plan Artifact).
- Prompt includes 2-3 concrete examples to anchor classification
  (avoid bias drift).
- Result cached on disk by question hash so re-running the same task
  doesn't pay the cost twice.
- Fail-soft on any error → "medium" → Plan Artifact does not
  trigger → behavior degrades to baseline.

### Consequences

Positive
- ~+10pp accuracy over regex (5-7 tasks correctly routed on KDD).
- Sees manifest, picks up cross-source/multi-table complexity that
  short questions hide.
- Generalizes to any input (no benchmark dependency).
- Cost negligible: $0.10-0.25 per 50-task eval batch.

Negative
- ~+1 LLM call per task (caching mitigates re-runs).
- LLM judgment can drift; temp=0 + strict schema mitigates.
- Adds a path that is testable but not deterministic; unit tests
  must mock the LLM call.

### Reconsideration triggers
- Cached LLM judgments diverge between runs (means temp=0 isn't
  deterministic enough on the eval endpoint).
- Cost per eval becomes meaningful (>$1) — switch to regex first +
  LLM only on borderline.
- A clean regex heuristic emerges that matches LLM accuracy.

### Implementation note

The classifier returns 3 levels for future flexibility (e.g., medium
might trigger a lighter "compound plan" later). Currently only "hard"
triggers full Plan Artifact.
