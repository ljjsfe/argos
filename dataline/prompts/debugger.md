Fix the following Python code that failed during data analysis execution.

## Goal
{question_context}

## Code that failed
```python
{failed_code}
```

## Error (full traceback)
```
{full_traceback}
```

## Error Summary
{error_type}: {error_message}

## Retry context
{retry_context}

## Data context (column names, types, sample values, value distributions)
{data_context}

## Rules
1. Fix the SPECIFIC error shown in the traceback.
2. Use the data context above to fix column name mismatches, type errors, encoding issues. Check exact column names and values.
3. If this is a retry, try a FUNDAMENTALLY DIFFERENT approach — do not just tweak the same failing logic.
4. The fixed code must still answer the original question (see Goal above).
5. Common fixes:
   - KeyError/column not found → check exact column names in data context; try case-insensitive matching
   - TypeError → check column dtypes and data structure (list vs dict vs DataFrame)
   - FileNotFoundError → use `safe_read_csv()` or `safe_read_json()` which auto-resolve paths
   - sqlite3 error → check table/column names via `PRAGMA table_info()`; use quotes around table/column names with spaces
   - "unhashable type: list" → don't use list columns as dict keys; iterate instead
   - JSONDecodeError → inspect the raw text first; if records are nested under a key like `data.records`, use `safe_read_json_df()` which auto-unwraps `{"records": [...]}` shapes
   - UnicodeDecodeError → use encoding="latin-1" or errors="replace"
6. Return the COMPLETE fixed code (all imports, all logic).

## Retry strategy (if this is attempt 2+)
- If the SAME error recurs: the approach is wrong. Try a completely different method.
  - If pandas failed on a CSV, try reading it with different parameters (delimiter, header).
  - If sqlite3 query failed, try loading the table into pandas first.
  - If JSON parsing failed, try reading as text and parsing manually.
- If a DIFFERENT error occurs: fix this new error while keeping the previous fix.
- NEVER repeat code from previous attempts that produced the same error.

## Output
Return the fixed Python code wrapped in ```python ... ```.
Add a brief comment at the top explaining what you changed.
