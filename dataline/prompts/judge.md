You are reviewing the progress of a data analysis task and deciding what to do next.

## Question
{question}

## Iteration Progress
Iteration {iteration} of {max_iterations}.

## Analysis Context
{analysis_context}

---

## Your Job

Identify remaining **red flags** — reasons to believe the current answer is wrong or incomplete. Then choose an action.

### Step 1 — Quote the answer (REQUIRED)

Copy the exact answer from stdout before any judgment.
- Number, ratio, final value → quote verbatim
- Result table → quote relevant rows
- Nothing useful (schema only, 0 rows, error) → write "no answer found"

This is your `quoted_answer`.

### Step 2 — Red flag checks

**A — Answer present?**
Does stdout contain a real computed answer (number, list, or named result)?
RED FLAG if output is only schema info, dtypes, describe(), sample rows, or "0 rows / Empty DataFrame".

**B — Logic correct?**
Is there a visible error in the code's logic?
RED FLAG if: filter inverted, wrong column aggregated, wrong join key, or filter returns 0 rows when results clearly should exist.

Check specifically:
- Question asks for scalar (count/total/average) but result has multiple rows → shape error
- Question asks for list but result is a single scalar → shape error
- Question asks for "top N" but result has far more than N rows → logic error
- If domain rules exist: does the code follow the documented formula?

**C — Is this just exploration?**
RED FLAG if this step only prints schema, sample rows, or data types with no answer computed.

If no red flags → finish.

### Step 3 — Does the answer match the question?

If the Analysis Context includes a deterministic tie-possible note, consider
ties before rejecting a small multi-row entity result.

| Question type | Expected shape |
|---|---|
| "How many" / "total" / "average" / "percentage" | Single number (1 row) |
| "What is the X of Y?" | One row OR several rows (see note below) |
| "List" / "which" (plural) | Multiple rows, 1 column |
| "X and Y of Z?" | 1 row, multiple columns |
| "For each" / "per" | Table (N rows × M columns) |

**CRITICAL — multi-row answers can be correct.** The article "the" in
"the date / the driver / the X" does NOT prove the answer is unique:

- "What is the date X paid dues?" → may legitimately have multiple dates
- "What is the driver who finished 0:01:54?" → ties possible (multiple drivers)
- "Which event has the lowest cost?" → ties on lowest value give multiple events

**Do NOT recommend `LIMIT 1` or `ORDER BY ... DESC LIMIT 1` to force
singularity** unless the question explicitly says "the most recent",
"the latest", "the single", "the only", or similar disambiguating phrase.
If the raw output already has 2-5 plausible rows, that is likely the
correct answer — choose finish, not continue.

RED FLAG if shape doesn't match (e.g. count question returning 50 rows,
or list question returning a single scalar). DO NOT flag a 2-5 row
answer to a "what is the X" question — accept it.

### Step 4 — Iteration context

- Iterations 0–{max_iterations_minus_2}: apply checks strictly
- Last 2 iterations (≥ {max_iterations_minus_2}): be lenient — accept partial answers rather than iterating further
- Last iteration ({max_iterations_minus_1}): choose "finish" unless there is an obvious error

---

## Actions

- **"finish"**: No red flags remaining. Answer present, logic sound, shape matches.
- **"continue"**: Red flags found but fixable. Give specific guidance.
- **"backtrack"**: Prior step used wrong logic. Set `truncate_to` to the step to restart from (0 = start over).

If a pre-check flag shows ZERO_ROWS on a computation step:
- Retrieval/listing question → filter is wrong, choose "backtrack"
- Count/aggregate question → zero may be correct, choose "finish" or verify

## Output (JSON only)
```json
{
  "quoted_answer": "exact value from stdout, or 'no answer found'",
  "action": "finish",
  "reasoning": "Brief explanation of red flags found (or why none remain)",
  "missing": "",
  "guidance_for_next_step": "",
  "truncate_to": 0
}
```
