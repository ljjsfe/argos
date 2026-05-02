# Prompt Audit — v60

**Date**: 2026-05-02
**Scope**: All 8 prompt files in `dataline/prompts/`
**Method**: Each prompt evaluated against 7 gates:
1. Necessity (delete and behavior changes?)
2. Specificity (concrete action, not abstract advice?)
3. Uniqueness (not duplicated in another prompt?)
4. Correctness (matches current architecture?)
5. Schema-agnostic (no benchmark-specific names?)
6. Actionable framing (do/don't, not "consider"?)
7. Right ownership (rule belongs to this agent's scope?)

**Output**: per-prompt verdict with KEEP / EDIT / DELETE / MOVE annotations,
plus cross-prompt findings and canon placement.

---

## Headline findings (read this first)

### 🔴 Two prompts are entirely dead

| Prompt | Lines | Status | Reason |
|---|---|---|---|
| **analyzer.md** | 41 | **DEAD** | `analyzer.analyze()` defined but NEVER called from `orchestrator.py`. Only `_extract_domain_rules` (deterministic) and `compile_domain_rules` (uses `domain_extractor.md`) are wired in. `_attempt_llm_profile` and `_attempt_simple_profile` are reachable only from `analyze()`. |
| **question_analyzer.md** | 96 | **DEAD** | `analyze_llm()` exists in code but no caller. Orchestrator at line 193 calls `analyze_deterministic(question)` exclusively. The LLM-based version is explicitly labeled "Legacy — prefer analyze_deterministic" in the docstring. |

**Action**: DELETE both prompts. Optionally also delete the corresponding
unused `analyze()` and `analyze_llm()` functions in the agents (separate
cleanup commit, conservative scope this round).

That's **137 lines of dead prompt** and likely 100+ lines of dead Python.

### 🟡 Six prompts are alive but uneven

| Prompt | Lines | Verdict |
|---|---|---|
| debugger.md | 50 | KEEP, light edit |
| domain_extractor.md | 31 | KEEP, light edit |
| finalizer.md | 34 | EDIT (rule numbering broken — jumps from 9 to 11; consolidate) |
| finalizer_dabstep.md | 24 | KEEP, light edit |
| judge.md | 111 | EDIT (Step 2D + Step 4 leniency need tightening) |
| planner_coder.md | 90 | KEEP, with surgical canon additions |

### 🟢 Net change estimate

```
DELETE: 41 + 96  = -137  lines  (analyzer.md, question_analyzer.md)
EDIT:    -25 ~ -40 lines (consolidations, vague advice removed)
ADD:     +20 ~ +30 lines (canon rules where they actually fit)
─────────────────────────
NET: roughly -130 to -150 lines, prompts go 477 → 330 ish
```

**Quality > volume.** This audit subtracts more than it adds.

---

## Per-prompt audit

### 1. analyzer.md (41 lines) — **DELETE ENTIRE FILE**

Not loaded in current pipeline. The `analyzer.analyze()` function that
would consume this prompt is unreachable from `orchestrator.py`.

The "deep profiling" function it describes is replaced by:
- `manifest.scan()` — deterministic file scanning
- Profiler reader modules — deterministic per-format profiling
- `analyzer._extract_domain_rules()` — deterministic doc extraction

**Recommendation**: DELETE `dataline/prompts/analyzer.md`. Optionally
also remove `analyzer.analyze()`, `_attempt_llm_profile()`,
`_attempt_simple_profile()` from `dataline/agents/analyzer.py` in a
follow-up commit (more conservative — this audit only touches prompts).

---

### 2. question_analyzer.md (96 lines) — **DELETE ENTIRE FILE**

Same situation: `analyze_llm()` function exists but no caller.
Orchestrator uses `analyze_deterministic()` (regex-based, zero LLM).

**Recommendation**: DELETE `dataline/prompts/question_analyzer.md`.
Optionally remove `analyze_llm()` function from `question_analyzer.py`.

---

### 3. planner_coder.md (90 lines) — **KEEP, surgical edits**

This is the workhorse prompt. Most content is well-targeted.

#### KEEP as-is
- Decision Sequence Step 1 (answer shape)
- Decision Sequence Step 3 (SQL-first, LIMIT 1 ties, percentages)
- Execution Environment section (working dir, env vars, libraries)
- DuckDB SQL section
- Output Format section

#### EDIT
- **Step 1 shape patterns**: the `"X and Y of Z" → 1 row, multiple columns` line is correct but understated. Add explicit rule about multi-noun questions producing multi-column output.
- **Step 2 schema reminders**: the `link_to_X` rule is generic enough but not all benchmarks use that prefix. Reword to "FK column names ending in `_id` or starting with `link_to_` indicate joins to a primary table".
- **Helpers list (lines 44-55)**: hand-maintained list. v57 added helpers that are NOT here — `parse_jsonish_value`, `parse_jsonish_column`, `explode_jsonish_column`, `coerce_numeric_id_columns`, `join_with_type_coercion`, `safe_extract_tables`. **Add them to the I/O / probe sections.**

#### MOVE FROM (no rules currently belong elsewhere — clean ownership)

#### Canon additions (only after this audit lands)
The 5 surgical canon additions to ADD here (after audit):
1. Average per time unit → `SUM/COUNT(DISTINCT)`, not `AVG`
2. NULL semantics in aggregates
3. `value_overlap` before joining same-named ID across heterogeneous sources
4. After-filter row count print as verification habit
5. Whitespace trim + `clean_numeric` for dirty string data

(Deferred — see "Canon placement" section at the end.)

---

### 4. judge.md (111 lines) — **EDIT, tighten leniency**

#### KEEP
- Step 1 quote-the-answer requirement (reduces hallucination)
- Step 2.A "answer present?" check
- Step 2.B logic correctness check
- Step 2.C exploration-only check
- Step 3 expected-shape table
- Multi-row answer caveat (don't force LIMIT 1 — important)

#### EDIT
- **Step 2.D Domain formula compliance** is currently 5 lines of nested
  conditional logic. A small model often skips it. **Rewrite as a
  single-question check**: "Does the question reference a metric whose
  formula is given in Domain Rules? If yes, do the code's columns
  match the formula? Flag if mismatch."
- **Step 4 "Iteration context" leniency**:
  ```
  current: "Last 2 iterations (≥ {max_iterations_minus_2}): be lenient
            — accept partial answers rather than iterating further"
  ```
  Empirically (task_11 trace), this caused premature acceptance of
  bad answers. **Tighten to**: "Last 2 iterations: still apply Step 2
  red-flag checks; only accept if no red flags fire. The leniency is
  for shape mismatches with `tie_possible=true`, NOT for logic errors."
- **Step 3 multi-row table** (lines 66-77) repeats the tie-possible
  guidance from Step 2. Consolidate into one paragraph.

#### DELETE
- The `quoted_answer` field in output JSON (line 104) is collected but
  not used downstream by orchestrator. Either wire it in or remove the
  output requirement (saves ~3 lines and one mental task for the
  model). Recommendation: REMOVE from output (add back when actually
  consumed).

---

### 5. finalizer.md (34 lines) — **EDIT, fix numbering and consolidate**

#### Bugs to fix
- Rule numbering jumps from 9 to 11 (rule 10 is missing). Renumber.
- Rule 5 ("clean numbers: no $/%") and Rule 9 ("strip formatting
  characters") say the same thing twice. Merge.

#### KEEP
- Rules 11, 14, 17, 18 — these are precisely the canon shape contracts
  we'd add, already here.
- Rule 13 NA distinguishing — important behavior.

#### EDIT
- Rule 8 ("Use the LAST successful step's output") is mostly correct
  but the WARNING about 0 rows / empty results — orchestrator now has
  carry-forward logic that handles this. **Soften** to "use the most
  recent step that called `save_result()` with non-empty answer".
- Rule 16 (NAME COLUMNS) is benchmark-specific (KDD scorer behavior).
  Move to a `## Scorer notes (KDD)` block at the end OR remove if
  scorer is now strict.

#### Net
~5 lines removed (deduplication), 0 added.

---

### 6. finalizer_dabstep.md (24 lines) — **KEEP, light edit**

Distinct purpose from `finalizer.md` (scalar output for DABstep). Concise.

#### EDIT
- Rule 6 NA distinguishing duplicates `finalizer.md` Rule 13 verbatim.
  Consider extracting to a shared `## NA handling` snippet that both
  prompts include via prefix or just keep duplicated (it's important
  enough). For now: **KEEP both copies**, accept duplication as cost
  of separation.

#### KEEP all rules — this prompt is tight already.

---

### 7. debugger.md (50 lines) — **KEEP, minor edit**

#### KEEP
- All 6 rules (1-6) are concrete and actionable.
- "Common fixes" list (lines 30-37) is excellent — concrete error → fix.
- Retry strategy section (lines 40-46) is correct.

#### EDIT
- Line 34: `JSONDecodeError → use describe_data()`. `describe_data` is
  for objects, not raw JSON files. The actual helper for nested JSON
  inspection should be referenced. **Fix to**: "JSONDecodeError →
  inspect raw text first; check if records are nested under a key
  like `data.records`; use `safe_read_json_df()` which auto-unwraps".

#### Add (canon)
- One line under "Common fixes": "Wrong row count after merge →
  type mismatch on join keys; use `coerce_numeric_id_columns()` or
  `join_with_type_coercion()`."
  (This is the type-coercion canon rule, placed where debugger
  consumes it.)

---

### 8. domain_extractor.md (31 lines) — **KEEP, no changes needed**

This prompt is tight. Extracts business rules from documentation.
No action.

---

## Cross-prompt findings

### Duplications (same rule said in two places)

| Rule | Locations | Action |
|---|---|---|
| NA handling (Legitimate vs Code-failure) | finalizer.md rule 13 + finalizer_dabstep.md rule 6 | KEEP both (separated for clarity) |
| Multi-noun output → multi-column | planner_coder.md Step 1 (implicit) + finalizer.md Rule 11 (explicit) | KEEP both: Planner writes the right code, Finalizer enforces the right output |
| Tie-possible (no LIMIT 1) | planner_coder.md + judge.md Step 3 | KEEP — both need it |
| Counts as integers | planner_coder.md Step 3 + (implicit elsewhere) | OK |

No real duplication problems — the apparent overlaps are by design (same rule from different roles).

### Contradictions

None found.

### Out-of-place rules

- `finalizer.md` Rule 16 (KDD scorer name-column behavior) is leaking
  scorer logic into the finalizer prompt. Move to a clearly tagged
  `## Scorer notes (KDD)` section or remove.

### Stale references

- planner_coder.md helper list missing 6 v57+ additions (already
  flagged above).

---

## Canon placement decisions

After audit, here are the 5 canon rules that genuinely add value
(reduced from my original 13 — others are already covered by existing
prompts as noted in the per-prompt section):

| # | Canon rule | Where | Section |
|---|---|---|---|
| C1 | Average per time unit → `SUM/COUNT(DISTINCT)` not `AVG` | planner_coder.md | Step 3 (Write the query) |
| C2 | NULL in aggregates: `COUNT()` skips NULL, `SUM(NULL)=NULL` | planner_coder.md | Step 3 |
| C3 | `value_overlap` before joining same-named ID across heterogeneous sources | planner_coder.md | Step 2 (Map question to source columns) |
| C4 | Print row count after every filter (verification habit) | planner_coder.md | Step 3, end of section |
| C5 | Wrong row count after merge → join-key type mismatch | debugger.md | "Common fixes" list |

**Rules from my original 13 that the audit found are already covered**:
- "Multi-noun → multi-column": finalizer.md Rule 11 already enforces
- "Single-noun → 1 column": finalizer.md Rule 18 enforces
- "Granularity match": finalizer.md Rule 14
- "How many → 1 row 1 column": planner_coder.md Step 1
- "Trim whitespace": debugger.md handles encoding fixes; planner_coder
  prompt could use a brief mention but `clean_numeric` already in
  helpers list
- "Clean numeric strings": already mentioned via `clean_numeric` helper
- "Many-to-many join inflation": partial coverage in judge.md Step 3
  ("if the raw output already has 2-5 plausible rows...") — could
  strengthen but low priority
- "describe_df / count_distinct": already in planner_coder helpers

**Net**: Originally proposed 13 canon additions → only 5 truly new
after audit. The other 8 are already in the prompts.

---

## Recommended commit sequence

```
Phase 2.2a: prompt audit cleanup (this audit's deletions/edits ONLY)
  - DELETE  analyzer.md, question_analyzer.md
  - EDIT    finalizer.md     (renumber, dedupe rules 5/9, soften rule 8)
  - EDIT    judge.md         (tighten Step 2.D, Step 4 leniency, drop quoted_answer)
  - EDIT    planner_coder.md (refresh helpers list, reword link_to_X, multi-noun emphasis)
  - EDIT    debugger.md      (fix JSONDecodeError reference)
  Net: ~-150 lines, ~+10 lines
  Tag: stable-v60-prompt-audit
  Eval: A/B (audit vs pre-audit) on KDD

Phase 2.2b: canon additions (only if 2.2a doesn't regress)
  - ADD     planner_coder.md C1, C2, C3, C4 (4 lines each-ish)
  - ADD     debugger.md C5 (1 line)
  Net: ~+20 lines
  Tag: stable-v61-canon
  Eval: A/B (canon vs no canon)
```

---

## Decision gates

### After Phase 2.2a (audit only, no canon)

| Δ KDD score | Action |
|---|---|
| ≥ 0 (within ±2 noise) | ✅ audit succeeded — proceed to canon |
| -1 ~ -2 | ⚠️ recheck 2-3 specific tasks; might be noise; if no obvious cause, proceed |
| ≤ -3 | ❌ revert specific edit; investigate which removal/change broke it |

### After Phase 2.2b (canon added)

| Δ KDD score (vs 2.2a) | Action |
|---|---|
| ≥ +1 | ✅ canon helps, retain |
| -1 ~ +1 | neutral — keep canon, value is generality not score |
| ≤ -2 | identify which canon line caused it, remove, retry |

---

## Open questions for review

1. **Should the dead Python (analyze, analyze_llm) be removed in this commit or separately?** Recommendation: separately, more conservative.
2. **Rule 16 in finalizer.md (KDD name columns)**: move to scorer-notes section, or delete entirely (since it's actively benchmark-specific)?
3. **judge.md `quoted_answer` field**: drop now or wire it to something useful?

Awaiting your verdict per item before I produce the staged diff.
