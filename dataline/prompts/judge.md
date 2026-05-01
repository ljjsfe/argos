You are reviewing a data analysis task and deciding next action.

## Question
{question}

## Iteration Progress
Iteration {iteration} of {max_iterations}.

## Analysis Context
{analysis_context}

---

## Decide

**Step 1 — Quote the answer.** Copy exact value/rows from stdout into `quoted_answer`. Empty / schema-only / errors → "no answer found".

**Step 2 — Red flag checks** (any flag → not finish):
- **Answer absent**: stdout shows only schema/sample/`Empty DataFrame`.
- **Wrong shape**: scalar question → multi-row result; list question → single scalar; "top N" → far more than N.
- **Logic error**: filter inverted, wrong join key, wrong aggregation column, 0 rows when results should exist.
- **Domain formula mismatch** (only if `Domain Rules` section present): if the question's metric is defined there, the code must use the same columns/aggregation. Semantic equivalents (`SUM(CASE WHEN…)` vs `COUNT(…) FILTER`) are fine — flag only genuine divergence.
- **Exploration only**: step prints schema/dtypes with no computed answer.

**Step 3 — Tie-aware shape check.**
| Question type | Expected shape |
|---|---|
| "How many" / "total" / "average" / "percentage" | 1 row, 1 col |
| "List" / "which" (plural) | many rows, 1 col |
| "X and Y of Z" | 1 row, multi col |
| "For each" / "per" | grouped table |

Multi-row answers can be correct — articles like "the date / the driver / the X" do NOT prove uniqueness; ties are common.
Do NOT push for `LIMIT 1` unless the question says "the latest", "the most recent", "the single", "the only".
2-5 plausible rows for "what is the X" → accept.

**Step 4 — Iteration leniency.**
- Iter 0 ~ {max_iterations_minus_2}: strict.
- Last 2 iter (≥{max_iterations_minus_2}): accept partial answers rather than retrying.
- Last iter ({max_iterations_minus_1}): finish unless obvious error.

---

## Actions
- **finish**: no red flags.
- **continue**: red flags fixable in next iter; give specific guidance.
- **backtrack**: prior step used wrong logic; set `truncate_to` (0 = restart).

ZERO_ROWS pre-check on a compute step:
- Retrieval question → filter wrong, backtrack.
- Count/aggregate question → zero may be correct, finish or verify.

## Output (JSON only)
```json
{
  "quoted_answer": "exact value from stdout, or 'no answer found'",
  "action": "finish",
  "reasoning": "Brief red flags found (or why none remain)",
  "missing": "",
  "guidance_for_next_step": "",
  "truncate_to": 0
}
```
