Format the final answer from the completed analysis.

## Question
{question}

## Analysis results (all steps and their outputs)
{steps_summary}

## Rules
1. Extract the answer from the step results above.
2. Format as a JSON object representing a table: {"columns": {"col_name": [values]}}
3. Column names should match what the question asks for. Use the exact wording from the question when possible.
4. Include ALL requested data. If the question asks for ID, name, and value — include all three columns.
5. Strip formatting characters ($, %, ',', unit suffixes) and extra whitespace from values, unless the question explicitly asks for formatted values. The scorer normalizes numeric values to 2 decimal places automatically — copy numbers verbatim from the step output, do not pre-round.
6. If the question asks for a single value, still format as: {"columns": {"answer": [value]}}
7. Use the most recent step that called `save_result()` with non-empty answer as the primary data source. Earlier steps may have been exploratory.
8. If the question asks multiple sub-questions (e.g., "What is X and Y?", "Find A, B, and C"), your output MUST have a separate column or value for EACH sub-question. Never merge multiple answers into a single column.
9. All lists in the output MUST have the same length. A table with mismatched column lengths is invalid.
10. "Not Applicable" rules — distinguish two cases:
    - **Legitimate NA**: You successfully queried the data and confirmed the data genuinely does not contain the information needed (e.g., column exists but all values are null, the entity asked about does not appear in the dataset). "Not Applicable" is the correct answer.
    - **Code failure NA**: The code raised an error, timed out, returned 0 rows due to a wrong filter, or you are unsure. You MUST NOT output "Not Applicable" — instead output your best partial answer from earlier successful steps. Never disguise a code failure as "Not Applicable".
11. Column granularity — CRITICAL: match the question's requested granularity exactly.
    - If the question asks for individual entity values (e.g., "list the patient IDs"), output one row per entity — do NOT aggregate or deduplicate unless the question asks for distinct counts.
    - If the question asks for a summary (e.g., "total count", "average fee"), output a single scalar — do NOT output the underlying records.
    - Never conflate record-level output with aggregate output.
12. Do NOT output a raw DataFrame or Python repr as the answer. The answer must contain the actual computed values — numbers, strings, or identifiers — not a formatted table printout with alignment spaces and index columns.
13. OUTPUT TYPE GUIDANCE: if the Question Analysis above specifies `answer_type` (scalar / list / table), use it to verify your structure. A scalar answer must produce a single value column. A list answer must produce one row per entity. A table answer must preserve all requested dimensions as separate columns.
14. COLUMN SCOPE — CRITICAL: if "Required Column Structure" is listed above, output ONLY those columns. Do NOT add extra explanation columns, intermediate computation columns, metadata columns, or index columns. Every extra column that has no matching gold column reduces your score.

## Output (JSON only, no other text)
{"columns": {"column_name_1": [val1, val2, ...], "column_name_2": [val1, val2, ...]}}
