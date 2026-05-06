# Harness Verification Design — v63-v65

**Date**: 2026-05-03 (planning)
**Status**: REVIEW — implementation pending sign-off
**Baseline**: stable-v62-vision-integrated (KDD 33/50, DABstep 1/10)

---

## 1. Goal & Non-Goals

### Goal
Shift answer verification responsibility **from agent self-attestation to
harness ownership**, in three coordinated layers (pre-action, during,
post-action). Inspired by 陆徐洲's *Harness Engineering 实战*: agents
self-report "task done" 60% incorrectly when there is no harness
verification; the lift comes from making harness do the verifying.

### Non-Goals
- Improve KDD score by prompt tuning (proven futile via v60 audit).
- Add new agent roles or new LLM calls in the main loop (we add at
  most one decompose call, gated on complexity).
- Implement full Plan-Mode like Claude Code's permission gating (the
  article shows mechanical permission gating *hurts* — P3=14%).

---

## 2. Background — what we already learned

### 2.1 Skeptic failure (v53)
Adding LLM verification AFTER Judge said finish caused -4 task
regression. Same failure pattern as the article's D3 (LoopGuard with
prescriptive guidance): directive guidance pollutes exploration
space.

### 2.2 Prompt audit failure (v60)
Bulk-editing active prompts (sharpening / removing redundancy) caused
-3 regression with two deterministic regressions. Verbose redundancy
in prompts is load-bearing for 3B-active models.

### 2.3 What's confirmed working
- Determinism > LLM (HarnessGate static rules).
- Multi-candidate code generation (we have it but throw away
  consensus signal).
- Vision integration (lazy, fail-soft).
- Carry-forward fix (v55).

### 2.4 What's missing (the 4 defenses, mapped to our agent)

| Defense | Article | Our agent | Status |
|---|---|---|---|
| Plan as artifact | P_doc 100% | PlannerCoder writes ephemeral JSON plan | ❌ no persistent artifact |
| Neutral LoopGuard | LoopGuard | block_rule_counts escalation | ⚠️ partial; judge_guidance can be directive |
| Post-execution verify | O3 (run pytest) | HarnessGate static rules + LLM Judge | ⚠️ no independent re-execution |
| Independent ground truth | run_pytest | Judge reads stdout (agent's framing) | ❌ missing |

This document addresses defenses 1, 3, and 4 via three components.

---

## 3. Components

### 3.1 Component C1 — Multi-candidate Consensus (Defense 4: independent verify)

**Idea**: We currently generate 2-3 code candidates and stop at the first
rc=0. We can — at near-zero cost — execute ALL candidates, compare
their answers, and use disagreement as a signal of genuine ambiguity.

#### Design

```
Old loop:
  for c in candidates:
    r = execute(c)
    if r.rc == 0: break
  return r

New loop:
  clean_results = []
  for c in candidates:
    r = execute(c)
    if r.rc == 0 and r.structured_json:
      clean_results.append((c, r))
    else:
      log_failure(c, r)

  if not clean_results:
    fall through to debugger path  # unchanged
  elif len(clean_results) == 1:
    use it  # no consensus signal — like before
  else:
    if all_answers_agree(clean_results):
      pick first; mark consensus="agree"
    else:
      mark consensus="disagree"
      inject HarnessGate flag "candidate_disagreement"
      orchestrator forces continue with disagreement summary
```

#### `all_answers_agree` definition

Compare normalized `structured_json` across candidates:
1. Parse `answer` dict from each candidate's JSON.
2. Normalize numeric values (round to 4 decimals).
3. Sort lists in each value (set-equal comparison).
4. Compare keys + values.
5. Allow tolerance for floats: `abs(a-b) / max(abs(a), abs(b), 1) < 0.01`.

Two candidates agree iff their normalized answer dicts are equal.

#### Disagreement → HarnessGate behavior

```
flag = HarnessFlag(
    rule="candidate_disagreement",
    severity="block",   # blocks Judge; forces retry
    message=f"Candidates produced different answers:\n"
            f"  Candidate 0 ({lang}): {ans_0_summary}\n"
            f"  Candidate 1 ({lang}): {ans_1_summary}\n"
            f"There is genuine ambiguity in the question or data. "
            f"Re-examine whether the question's filters/aggregations "
            f"are unambiguous."
)
```

#### State changes

- `AnalysisState` adds `last_consensus: str = ""` (`"agree"` | `"disagree"` | `""`)
- Trace adds per-iter `consensus_signal` field.

#### Cost impact
- Existing: ~1.0× sandbox time per iter (first success wins, others skipped)
- New: ~1.7× sandbox time per iter (all candidates executed)
- KDD avg iters per task ~1.5 → marginal cost ~+50% sandbox time, but
  sandbox is fast (most candidates <2s), so wall-clock impact small.

#### Failure modes
- Candidates always agree on wrong answer → consensus is FALSE positive.
  Mitigation: HarnessGate other rules (stdout_leak etc.) still catch
  obvious wrongs.
- Tie-possible questions: candidates may legitimately produce different
  row orderings → false disagreement. Mitigation: sort lists in
  comparison.
- Numeric precision differences: fixed by tolerance.

#### KDD tasks expected to benefit
- task_86 (Alex Yoong, "extra rows"): if candidate variants disagree
  on filter, signal exposes it.
- task_89 (2008 Chinese GP): if candidates pick different "rank 2"
  interpretations, exposes it.
- Maybe 1-3 tasks total.

---

### 3.2 Component C2 — Plan Artifact (Defense 1: pre-action plan as file)

**Idea**: For complex multi-step questions, decompose once into an
ordered list of sub-steps written to `plan.md` in TEMP_DIR. Plan is
persistent across iterations. Each iteration: PlannerCoder reads
plan.md, executes one or more steps, MUST update plan.md with
finding before save_result.

This is the article's P_doc design: the plan is a file, not a
prompt-time string.

#### Difficulty gating (LLM-based, see ADR-004)

Only `hard` questions trigger Plan Artifact. Easy/medium skip it
(preserves baseline on ~80% of tasks).

```python
def estimate_difficulty(question: str, manifest_summary: str, llm) -> str:
    """One small LLM call. Returns 'easy' | 'medium' | 'hard'.
    Cached on disk by question hash. Fail-soft to 'medium'."""

PROMPT = """Classify difficulty for this data analysis question.

Question: {question}
Available data: {manifest_short}

Levels:
- easy: single filter or single aggregation, one source.
- medium: 2-3 filters/joins, single source or simple cross-source.
- hard: nested conditions, multi-step reasoning, modal/hypothetical
        phrasing, or 3+ tables to join.

Examples:
  "How many patients with diagnosis X?" → easy
  "Average X grouped by Y for last 6 months?" → medium
  "For X transactions, what would be the average fee that scheme Y
   would charge for value Z?" → hard

Answer with one word: easy | medium | hard"""
```

Discipline:
- temperature=0
- strict output schema; non-matching → "medium" (no plan)
- on any LLM error → "medium" (fail-soft)
- result cached by sha1(question) in `cache/difficulty.json`

KDD distribution (estimated): ~30 easy, ~12 medium, ~8 hard.

#### plan.md format

```markdown
# Plan: <task_id>

## Question
<verbatim question>

## Output Shape
- columns: <expected_columns from QuestionSpec or LLM inference>
- row_count: <single | one_or_more | multiple>

## Steps
### S1: <description>
- inputs: <table.col list>
- expected_output: <var name + type>
- status: pending
- finding: ""

### S2: ...
- depends_on: S1
- ...

## Verified Findings
(filled progressively as steps complete)
```

#### Plan generation (1 LLM call before main loop)

```python
# dataline/agents/plan_artifact.py

@dataclass(frozen=True)
class PlanStep:
    id: str                            # "S1", "S2"
    description: str
    inputs: tuple[str, ...]            # ['payments.csv:value', 'fees.json:fee_value']
    expected_output: str               # "scalar: avg_fee_eur"
    depends_on: tuple[str, ...] = ()
    status: str = "pending"            # pending | running | done | failed
    finding: str = ""

@dataclass(frozen=True)
class PlanArtifact:
    question: str
    output_columns: tuple[str, ...]
    output_row_count: str
    steps: tuple[PlanStep, ...]

def build(question, manifest_summary, domain_rules, llm) -> PlanArtifact:
    """One LLM call. Decomposer prompt produces structured plan."""

def render_md(artifact: PlanArtifact) -> str:
    """Serialize for plan.md."""

def parse_planner_update(response: str) -> tuple[str, str]:
    """Extract (step_id, finding) from PlannerCoder JSON output."""

def update_step(artifact, step_id, status, finding) -> PlanArtifact:
    """Return new artifact with one step updated."""
```

#### PlannerCoder prompt addition

When `plan_path.exists()`:

```markdown
## Active Plan (read-only — must reference and update)
{plan_md_content}

In your output JSON, include:
- "step_id": which plan step you are completing this iteration (e.g., "S2").
- "finding": one-line verified result (number, table shape, key facts).

After your code runs, the orchestrator updates plan.md to record your finding.
The plan is persistent across iterations — DO NOT redo completed steps.
Reference completed step variables/findings rather than re-querying.
```

#### Orchestrator integration

```python
# After QuestionSpec / TaskRouter / Playbook:
complexity = estimate_complexity(question)
if complexity == "complex":
    artifact = plan_artifact.build(question, manifest_summary, domain_rules, traced_llm)
    plan_path = Path(sandbox.temp_dir) / "plan.md"
    plan_path.write_text(plan_artifact.render_md(artifact))
    state = state.with_plan_artifact(artifact)
    _log(trace, "plan", f"Decomposed into {len(artifact.steps)} steps")

# In iter loop, before PlannerCoder:
if state.plan_artifact:
    plan_md = plan_path.read_text()
    # Inject as Section in PlannerCoder context, priority 95

# After PlannerCoder + Sandbox:
if state.plan_artifact and pc_output.step_id and pc_output.finding:
    new_artifact = plan_artifact.update_step(
        state.plan_artifact,
        pc_output.step_id,
        status="done",
        finding=pc_output.finding,
    )
    plan_path.write_text(plan_artifact.render_md(new_artifact))
    state = state.with_plan_artifact(new_artifact)
```

#### State changes

```python
# AnalysisState:
plan_artifact: PlanArtifact | None = None
```

#### PlannerCoder output schema change

Existing JSON envelope:
```json
{"plan": "...", "language": "sql"}
```

New (only when plan_artifact is active):
```json
{"plan": "...", "language": "sql",
 "step_id": "S2",
 "finding": "Q2 result: avg_fee=0.12 EUR"}
```

Backward compat: `step_id` and `finding` are optional. If missing,
orchestrator skips the plan update.

#### Failure modes
- Decomposer (build) returns empty/invalid plan → fall back to no
  artifact (state.plan_artifact stays None, behavior unchanged).
- PlannerCoder ignores step_id/finding → orchestrator just doesn't
  update plan.md; behavior degrades to non-artifact path.
- Plan steps wildly wrong → agent ignores plan and writes its own
  query (advisory framing).

All three failure modes are fail-soft: KDD baseline behavior
preserved.

#### KDD tasks expected to benefit (shape failures)
- task_163: plan declares output_columns=['type','total'] → forces 2 cols
- task_173: plan declares output_columns=['Country'] → no date confusion
- task_257: plan declares output_columns=['ViewCount','user'] → 2 cols
- task_379: plan declares output_columns=['element'] → no count column

Direct hits: 4 tasks. Plus 1-2 task_11-style where decomposer says
"S0: verify join" — if model follows plan, catches issue at iter 0.

---

### 3.3 Component C3 — Invariant Verifier (Defense 3: post-execution check)

**Idea**: After Judge says finish, run deterministic invariant checks
on the final answer. ONLY high-confidence rules to avoid false BLOCKs.
This is NOT a Skeptic-style LLM second opinion.

#### Rule set (3 high-confidence rules)

##### Rule I1: Percentage range

```python
def _check_pct_range(answer: dict, question: str) -> list[HarnessFlag]:
    """Question contains percentage/percent → answer values must be in [0, 100]."""
    if not _question_asks_percentage(question):
        return []
    flags = []
    for col, vals in answer.items():
        if not isinstance(vals, list):
            continue
        for v in vals:
            if isinstance(v, (int, float)) and not (0 <= v <= 100.0001):
                flags.append(HarnessFlag(
                    rule="invariant_percentage_range",
                    severity="block",
                    message=f"Column '{col}' value {v} is outside [0, 100] "
                            f"for a percentage question.",
                ))
                return flags  # one flag is enough
    return flags

def _question_asks_percentage(q: str) -> bool:
    ql = q.lower()
    return any(t in ql for t in (
        "percentage", "percent", "what %", "% of", "fraction of",
    ))
```

##### Rule I2: Count must be non-negative integer

```python
def _check_count_int(answer: dict, qa: QuestionSpec) -> list[HarnessFlag]:
    """Count questions: answer must be non-negative integer."""
    if qa.computation_type != "count":
        return []
    flags = []
    for col, vals in answer.items():
        if not isinstance(vals, list):
            continue
        for v in vals:
            if not isinstance(v, (int, float)):
                continue
            if v < 0 or (isinstance(v, float) and v != int(v)):
                flags.append(HarnessFlag(
                    rule="invariant_count_int",
                    severity="block",
                    message=f"Column '{col}' value {v} is not a "
                            f"non-negative integer for a count question.",
                ))
                return flags
    return flags
```

##### Rule I3: Aggregate of non-trivial set must be non-zero

```python
def _check_nonzero_aggregate(answer: dict, question: str, qa: QuestionSpec) -> list[HarnessFlag]:
    """Aggregate question whose phrasing implies a positive expected value
    must not return all-zero. Conservative: only fires on specific phrasings."""
    if qa.computation_type not in ("aggregate", "ratio"):
        return []
    if not _expects_positive(question):
        return []
    flags = []
    for col, vals in answer.items():
        if not isinstance(vals, list) or not vals:
            continue
        if all(_is_zero(v) for v in vals):
            flags.append(HarnessFlag(
                rule="invariant_nonzero_aggregate",
                severity="block",
                message=f"Column '{col}' is all-zero for an aggregate "
                        f"question expecting a positive value. Likely a "
                        f"filter dropped everything or the aggregation "
                        f"is wrong.",
            ))
            return flags
    return flags

def _expects_positive(q: str) -> bool:
    """Conservative: only fire when phrasing makes 0 implausible."""
    ql = q.lower()
    # 'how much / how many ... that match X' could legitimately be 0,
    # so we exclude it. We fire only on explicit "average / total /
    # sum / what is the X" phrasings.
    if any(t in ql for t in ("how many", "how much")):
        return False
    return any(t in ql for t in (
        "average", "what is the total", "total cost", "sum of",
        "what is the percentage",  # 0% is suspicious on a populated set
    ))

def _is_zero(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (int, float)):
        return abs(v) < 1e-9
    return False
```

#### Integration point

`HarnessGate.check()` already runs after Sandbox and before Judge.
Append the three rules to the existing rule list. They produce
BLOCKs that prevent Judge from accepting and force a retry.

```python
# In harness_gate.py check()
flags.extend(_check_pct_range(answer, question))
flags.extend(_check_count_int(answer, spec))
flags.extend(_check_nonzero_aggregate(answer, question, spec))
```

#### KDD tasks expected to benefit
- task_396 (pct=0 on a populated superhero set): I3 + (maybe I1 if
  question says percentage) → BLOCK, force retry.
- task_418 (answer=0 for an aggregate-shaped question): I3 if
  question matches `_expects_positive`.
- task_352 (ratio=0): I3 if question says "what is the ratio".
- task_169 (avg=82M): does NOT trigger any rule (no magnitude
  invariant). Acknowledged miss.

Direct hits: 2-3 tasks. Reduces if some don't match `_expects_positive`.

#### Risk: false BLOCKs

The rules are conservative on purpose. Genuine 0-answer cases:
- "How many patients have X?" → can be 0; excluded by `_expects_positive`.
- "What is the total cost of X (which doesn't exist)?" → 0; flagged
  by I3 incorrectly, but data-genuinely-NA cases are handled by the
  Finalizer's "Not Applicable" rule.

If a task gets a false BLOCK, max_iterations are wasted; failure
mode is "retry until budget exhausted, fall back to last clean
result". Net effect: extra cost, possible -1 task.

---

## 4. Combined Effect on KDD

| Task | Failure | C1 | C2 | C3 | Net |
|---|---|---|---|---|---|
| task_11 | 0% overlap | weak | partial | — | weak |
| task_163 | shape | — | ✅ | — | ✅ |
| task_169 | magnitude | — | — | — | ❌ |
| task_173 | shape | — | ✅ | — | ✅ |
| task_180 | filter | — | weak | — | weak |
| task_196 | factor 2x | weak | — | — | weak |
| task_200 | interpretation | — | — | — | ❌ |
| task_257 | shape | — | ✅ | — | ✅ |
| task_344 | range | — | — | — | ❌ |
| task_352 | code error | — | — | ✅ | ✅ |
| task_379 | shape | — | ✅ | — | ✅ |
| task_396 | pct=0 | — | — | ✅ | ✅ |
| task_415 | (?) | — | — | — | ❌ |
| task_418 | =0 | — | — | ✅ | ✅ |
| task_86 | extra rows | weak | — | — | weak |
| task_89 | wrong driver | — | — | — | ❌ |

**Direct hits**: 7 tasks (163, 173, 257, 352, 379, 396, 418)
**Weak signals**: 4 tasks (11, 180, 196, 86)
**Misses**: 6 tasks (169, 200, 344, 415, 89, others)

**Optimistic**: +6 tasks → 36-37/50
**Realistic**: +3-4 tasks → 33-34/50
**Pessimistic with false BLOCKs**: +1 net → 31-32/50

Decision threshold: Δ ≥ +5 = success; Δ < +3 = run second confirm
before declaring failure.

## 5. DABstep Impact Estimation

| Task | Failure | C1 | C2 | C3 | Net |
|---|---|---|---|---|---|
| 1273 (avg fee) | hard multi-hop | — | ✅ (decomposes) | — | ✅ |
| 1305 (off 2x) | calc | weak | weak | — | weak |
| 1464/1681/1753 | row IDs as answer | — | ✅ (plan: scalar output) | — | ✅ |
| 1871 (off 100x) | magnitude | — | — | — | ❌ |
| 2697 (wrong field) | extraction | — | weak | — | weak |
| 49 (multi-choice) | wrong choice | — | — | — | ❌ |
| 5 (PASS already) | — | — | — | — | — |
| 70 (NA judgment) | NA | — | — | — | ❌ |

**Realistic DABstep**: 1/10 → 4-5/10. Most leverage from C2 Plan
Artifact on the row-ID-as-answer cases.

## 6. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| C1: candidates always agree on wrong answer | High | Low | other harness rules catch obvious wrongs |
| C1: tie-possible questions trigger false disagree | Medium | Medium | sort lists in comparison |
| C1: cost overhead from running all candidates | Certain | Low (~+30% sandbox) | accept |
| C2: decomposer produces nonsense plan | Medium | Medium | fail-soft: if parse fails, skip artifact |
| C2: agent ignores plan and writes its own query | High | Low | advisory framing, no enforcement |
| C2: complexity heuristic mislabels tasks | Medium | Low | conservative threshold |
| C2: plan.md token bloat across iters | Low | Medium | cap finding length, no full step bodies |
| C3: false BLOCK on legitimately 0 answer | Medium | Medium | conservative `_expects_positive` |
| C3: rules overlap with existing HarnessGate | Low | Low | review existing rules first |
| Combined: token cost +20-30% | Certain | Medium | budget OK for KDD eval |
| Combined: 3 changes confound debugging | Medium | High | separate commits + tags for bisect |

## 7. Testing Strategy

### Per-component unit tests
- C1: 6 tests (single candidate, all agree, all disagree, numeric
  tolerance, list ordering, empty results).
- C2: 10 tests (build/render/parse/update + complexity heuristic +
  edge cases).
- C3: 8 tests (each rule pos + neg + edge case).

### Integration smoke
- task_22 (KDD simple, complexity=simple): no plan artifact, no
  consensus regression. Token within ±5% of v62b.
- task_11 (KDD complex): plan artifact triggers, S0 verify join.
- task_169 (KDD complex calc): not directly fixed, but should run
  through new path without crashing.
- DABstep task_1273 (complex multi-hop): plan artifact decomposes.

### Eval gate
KDD 50-task batch. Compare to v62b (33/50, σ=1.78).
- Δ ≥ +5: ✅ success, retain
- +3 ≤ Δ < +5: ⚠️ borderline, run second confirm eval
- 0 ≤ Δ < +3: ⚠️ within noise, but check if specific failure modes
  fixed (e.g., task_163/173/257/379 should now pass)
- Δ < 0: ❌ regression, bisect via tag revert

## 8. Implementation Order

### Day A
- **A.1** (~30 min): Tag stable-v62-pre-harness. Write
  `docs/DECISIONS.md` ADR for harness verification approach.
- **A.2** (~3h): C1 Multi-candidate consensus.
  - orchestrator candidate loop change.
  - `consensus_check` helper.
  - HarnessFlag rule "candidate_disagreement".
  - 6 unit tests.
  - Smoke task_22.
  - Tag stable-v63-multi-candidate.
- **A.3** (~5h): C2 Plan Artifact.
  - `dataline/agents/plan_artifact.py` (build, render_md,
    update_step, parse).
  - `dataline/prompts/plan_artifact.md` (decomposer prompt).
  - `estimate_complexity` in question_analyzer.
  - State field, orchestrator integration.
  - PlannerCoder prompt section + output schema.
  - 10 unit tests.
  - Smoke task_22 (no plan), task_11 (plan triggers).
  - Tag stable-v64-plan-artifact.

### Day B
- **B.1** (~2h): C3 Invariant Verifier.
  - 3 rules in harness_gate.py.
  - 8 unit tests.
  - Smoke task_22 (no flags), task_396 (I3 fires).
  - Tag stable-v65-invariant-verifier.
- **B.2** (~30 min): KDD eval (50 tasks, ~25 min wall clock).
- **B.3** (~30 min): Score + per-task analysis.
- **B.4** (~30 min): DABstep eval (10 tasks, optional).
- **B.5**: Decision per gate criteria.

## 9. Acceptance Criteria

**To declare the harness verification redesign successful**:
1. KDD score Δ ≥ +5 vs v62b (33/50), confirmed across 2 runs.
2. Specific shape-failure tasks (163, 173, 257, 379) pass at least
   once each.
3. No new deterministic regression (task that passed in 3+ prior
   runs now fails in both v65 runs).
4. Token cost ≤ +30% vs v62b.
5. All unit tests pass.

If any criterion fails: bisect by reverting one tag, re-eval, repeat
until isolating the regression cause.

## 10. Open Questions for Review

1. **Complexity heuristic threshold**: the proposed regex catches
   nested clauses + multi-step modal verbs. Should we be more
   aggressive (catch compound too) or more conservative (only
   complex)? Trade-off: more triggering = more decomposer cost,
   broader coverage.

2. **Plan Artifact mode**: the design above is "advisory injection"
   (mode A). Should we also implement mode B (each step runs its
   own iter loop)? Mode B is heavier but more rigorous.

3. **Multi-candidate disagreement**: should we BLOCK and force retry,
   or WARN and let Judge decide? BLOCK is the article's prescription;
   WARN is safer.

4. **Invariant rule I3**: my `_expects_positive` is conservative.
   Should we extend to include "average X" / "what is the X"? More
   coverage but more false-BLOCK risk.

5. **Decomposer prompt**: should we use a structured JSON output
   (steps as list of dicts) or markdown? JSON is parseable but more
   rigid; markdown is more natural for LLM but harder to update
   programmatically.

6. **plan.md token budget**: how much room in the prompt should the
   plan section take? The schema can grow with multi-hop tasks.
   Cap at 1500 chars after rendering?

---

## Approval

To approve this design, confirm:
- [ ] Goals and non-goals are correct.
- [ ] Component designs match intent.
- [ ] Risk mitigations are sufficient.
- [ ] Acceptance criteria are appropriate.
- [ ] Open questions resolved.

After approval, implementation proceeds per Section 8.
