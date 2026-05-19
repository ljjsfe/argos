# Leak → Honest Info Substitution Map

**Captured**: 2026-05-19 (during G1 full eval)
**Purpose**: Catalog what _kinds of information_ leaked files were accidentally
providing to the agent, so future iterations can deliver the same info via
honest, deterministic mechanisms (no leaks).

This is the design substrate for H1 (question→manifest hint) and successor
work after S1 ships.

## Info type 1 — Filename semantics (entity hint)

Examples (deleted by L4):

| filename | implicit hint |
|---|---|
| `severe_thrombosis_patients.csv` | filter `Thrombosis = severe` |
| `severe_thrombosis_patients_fixed.csv` | same as above, revised |
| `countries_june_2013.json` | output schema: `Country` list for June 2013 |
| `creatinine_age_analysis_result.json` | filter dimensions: creatinine + age |

**Why it worked**: small models map question concepts to filename tokens.
"severe thrombosis" question + "severe_thrombosis_*" file → trivially identify
target.

**Honest substitute** = H1 question-entity → manifest-column matching:
- Extract entities from question (regex: quoted, capitalised phrases, domain terms)
- Match against manifest columns + DISTINCT values
- Emit `FOCUS_HINTS` section to PlannerCoder

H1 prototype confirmed: task_352 "Yearly Kickoff" + "October Meeting" → exact
match against `event.csv::event_name`. Universal mechanism, 0 LLM cost.

## Info type 2 — Pre-computed column schema

Examples:

| file | implicit schema hint |
|---|---|
| any `output/result.json` with `{"answer": [...]}` shape | tells agent the expected output format |
| `creatinine_age_analysis_result.json` (had keys: `count`, `under_70_count`, etc.) | gold output is a count |

**Honest substitute** = stronger `QuestionSpec` propagation:
- QuestionSpec already infers `answer_type`, `expected_row_count`, etc.
- HarnessGate already has shape checks
- But: explicit "Required Answer Shape" section in PlannerCoder context could
  be more prominent (already exists in code per `planner_coder.md` line 8;
  could be moved/expanded)

## Info type 3 — Domain-term → column binding

Examples (these were NOT in leaks; the gap is in current manifest):

| question term | bound column | source |
|---|---|---|
| "white blood cells" | `Laboratory.WBC` | knowledge.md domain rules |
| "creatinine" | `Laboratory.CRE` | knowledge.md |
| "fibrinogen" | `Laboratory.FG` | knowledge.md |
| "track number" | `driverStandings.position` | NOT in any current file (the v80 stable-fail task_86 issue) |

**Honest substitute**:
- DomainRules agent already extracts knowledge.md content
- Gap: no explicit `<term> → <column>` table is built
- Direction: parse knowledge.md for `**Term**: <column-mentioning text>` patterns,
  emit `DOMAIN_TERM_BINDINGS` section

## Info type 4 — Question shape → output column schema

Examples:

| question shape | implied output |
|---|---|
| "Identify the type X and their total value" | 2-column: type, sum |
| "How many times..." | 1-row scalar (ratio or count) |
| "List the names of X" | 1-column list |
| "Give their consumption status" | 1-column (Consumption) |

**Honest substitute**:
- QuestionAnalyzer's `expected_row_count` + `answer_type` already partially
  cover this
- Gap: doesn't infer the COLUMN NAMES expected in output
- Direction: extend QuestionSpec with `output_column_names_guess: list[str]`,
  derived from noun phrases in the question (e.g., "type of expenses and
  their total value" → `["type", "total_value"]`)

## Info type 5 — Pre-existing failed attempts (debug history)

Some leaked `result.json` files had `{"answer": 0, "error": "..."}` or empty
results from prior failed runs. These actually **hurt** the agent — it would
trust the wrong cached answer.

**Honest substitute**: NONE NEEDED. Failed prior attempts shouldn't influence
the agent's reasoning at all. L1+L2+L3 fully address this.

## Priority for post-S1 work

Ordered by impact estimate × universality:

1. **H1 question-entity → manifest match** (Info type 1) — strongest signal,
   replaces the filename-hint mechanism. H1 prototype demonstrated viability;
   needs: JSON records live-load, knowledge.md term extraction, number-noise
   filter, then production wiring.

2. **DOMAIN_TERM_BINDINGS section** (Info type 3) — extract knowledge.md
   `**Term**: ...` patterns and bind to mentioned columns. Universal across
   any benchmark that ships domain documentation. Solves task_86 ("track
   number" → position) family.

3. **Output schema guess** (Info type 4) — extend QuestionSpec to also emit
   inferred output column names. Helps Finalizer/HarnessGate column-count rule.

(2 and 3 are smaller; (1) is the main lever.)

## Open questions

- Is task_11's filename hint ("severe_thrombosis_patients") actually
  Information the agent needs to solve the task? Or just luck masking a
  genuine reasoning gap?
  - Test: Opus with clean input wrote correct query without the hint, so
    the info IS reasonably derivable from data + knowledge.md. The hint
    was a shortcut, not essential. Real fix: ensure DomainRules surfaces
    "Thrombosis=2 means severe" → trivially derive filter.

- Should H1 work be done before another full eval, or as a separate phase?
  Answer: separate phase. H1 is a substantive new feature, not a hygiene fix.
