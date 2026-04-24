You are evaluating the progress of a data analysis task.

## Question
{question}

## Iteration Progress
Iteration {iteration} of {max_iterations}.

## Analysis Context
{analysis_context}

---

## Evaluation Steps

### Step 1 — Quote the answer (REQUIRED)

Copy the exact answer from stdout before any judgment.
- Number, ratio, final value → quote verbatim
- Result table → quote relevant rows
- Nothing useful (schema only, 0 rows, error) → write "no answer found"

This is your `quoted_answer`.

### Step 2 — Three checks

**A — Answer present?**
Does stdout contain a real computed answer (number, list, or named result)?
FAIL if output is only schema info, dtypes, describe(), sample rows, or "0 rows / Empty DataFrame".

**B — Logic correct?**
Is there a visible error in the code's logic?
FAIL if: filter inverted, wrong column aggregated, wrong join key, or filter returns 0 rows when results clearly should exist.

Check specifically:
- Question asks for scalar (count/total/average) but result has multiple rows → shape error
- Question asks for list but result is a single scalar → shape error
- Question asks for "top N" but result has far more than N rows → logic error
- If domain rules exist: does the code follow the documented formula?

**C — Is this just exploration?**
FAIL if this step only prints schema, sample rows, or data types with no answer computed.

If all three pass → Step 3.

### Step 3 — Does the answer match the question?

| Question type | Expected shape |
|---|---|
| "How many" / "total" / "average" / "percentage" | Single number (1 row) |
| "What is the X of Y?" | Single value |
| "List" / "which" (plural) | Multiple rows, 1 column |
| "X and Y of Z?" | 1 row, multiple columns |
| "For each" / "per" | Table (N rows × M columns) |

FAIL if shape doesn't match. Specify what's wrong in guidance.

### Step 4 — Iteration context

- Iterations 0–{max_iterations_minus_2}: apply checks strictly
- Last 2 iterations (≥ {max_iterations_minus_2}): be lenient — accept partial answers rather than iterating further
- Last iteration ({max_iterations_minus_1}): choose "finish" unless there is an obvious error

---

## Actions

- **"finish"**: Answer present, logic correct, shape matches
- **"continue"**: Making progress, need more work. Give specific guidance
- **"backtrack"**: Prior step used wrong logic. Set `truncate_to` to the step to restart from (0 = start over)

If a pre-check flag shows ZERO_ROWS on a computation step:
- Retrieval/listing question → filter is wrong, choose "backtrack"
- Count/aggregate question → zero may be correct, choose "finish" or verify

## Output (JSON only)
```json
{
  "quoted_answer": "exact value from stdout, or 'no answer found'",
  "sufficient": true,
  "action": "finish",
  "reasoning": "Brief explanation",
  "missing": "",
  "guidance_for_next_step": "",
  "truncate_to": 0
}
```
