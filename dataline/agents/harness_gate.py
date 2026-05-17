"""HarnessGate: deterministic verification layer (zero LLM cost).

Three-tier exit mechanism (precision-first):
- "block": answer is definitely wrong → retry with feedback
- "warn":  answer may be wrong → soft-retry once (iteration 0 only)
- no flags: accept result

BLOCK rules (100% precision — zero false-positive tolerance):
- nan_answer: NaN/null values in answer
- empty_output: empty DataFrame / 0 rows (existence questions excluded)
- empty_answer: placeholder values like N/A, null (retrieval questions excluded)
- dict_string_answer: stringified dict instead of scalar value
- value_embellishment: formatting artifacts ($, %, units) that break scorer
- error_string_answer: error messages passed as answer values
- excuse_answer: "data unavailable/not found" instead of actual computation

WARN rules: agg_type, output_shape, join_cardinality, row_count_bound,
extra_columns, qa_column_count, scalar_range, tie_possible.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.types import HarnessFlag, QuestionSpec


# ---------------------------------------------------------------------------
# Rule 1: Aggregation function verification
# ---------------------------------------------------------------------------

_AGG_RULES: list[tuple[list[str], str, list[str]]] = [
    (
        ["lowest", "minimum", "smallest", "least", "min "],
        "MIN/min/nsmallest/idxmin",
        ["SUM(", "sum(", "AVG(", "avg(", ".mean(", ".sum("],
    ),
    (
        ["highest", "maximum", "largest", "greatest", "most", "max "],
        "MAX/max/nlargest/idxmax",
        ["SUM(", "sum(", "AVG(", "avg(", ".mean(", ".sum("],
    ),
    (
        ["average", "avg", "mean"],
        "AVG/mean",
        ["SUM(", "sum(", ".sum("],
    ),
    (
        ["total", "sum of"],
        "SUM/sum",
        ["AVG(", "avg(", ".mean("],
    ),
]


_SUM_AS_AVG_PATTERNS = [
    # SUM(x) / 12  or  SUM(x)/12.0  → annual-to-monthly normalization
    re.compile(r"SUM\s*\([^)]*\)\s*/\s*\d+(?:\.\d+)?", re.IGNORECASE),
    # SUM(x) / COUNT(...) → standard mean decomposition
    re.compile(r"SUM\s*\([^)]*\)\s*/\s*COUNT\s*\(", re.IGNORECASE),
    # .sum() / N or .sum() / len(...) in pandas
    re.compile(r"\.sum\s*\(\s*\)\s*/\s*(?:\d+|len\s*\(|count)", re.IGNORECASE),
]


def _is_sum_as_average(code: str) -> bool:
    """Detect SUM(...)/N or SUM(...)/COUNT(...) — a legitimate decomposed average.

    "average monthly" / "average per customer" semantics often require
    SUM-divided-by-period rather than row-wise AVG. Skip the agg_type rule
    when this pattern is present.
    """
    return any(p.search(code) for p in _SUM_AS_AVG_PATTERNS)


def _check_agg_type(question: str, code: str) -> list[HarnessFlag]:
    q_lower = question.lower()
    flags: list[HarnessFlag] = []
    for keywords, expected, forbidden in _AGG_RULES:
        if not any(kw in q_lower for kw in keywords):
            continue
        # Skip the rule when the code uses SUM/N or SUM/COUNT — that is a
        # valid decomposed-average and was the dominant false-positive on
        # "average monthly / per X" questions in v21 (e.g. task_169).
        is_avg_rule = any(kw in keywords for kw in ("average", "avg", "mean"))
        if is_avg_rule and _is_sum_as_average(code):
            continue
        for fb in forbidden:
            if fb in code:
                flags.append(HarnessFlag(
                    rule="agg_type",
                    severity="warn",
                    message=(
                        f"Aggregation mismatch: question asks for "
                        f"'{next(kw for kw in keywords if kw in q_lower).strip()}' "
                        f"(expected {expected}) but code uses '{fb.strip('(')}'. "
                        f"Use the correct aggregation function."
                    ),
                ))
                break  # one flag per rule match
    return flags


# ---------------------------------------------------------------------------
# Rule 2: Shape verification (unified)
#
# Merges former rules 2, 7b-scalar, 11, 12 into one coherent check.
# Avoids duplicate/conflicting flags for the same shape issue.
# ---------------------------------------------------------------------------

_SCALAR_PATTERNS = [
    r"how many\b", r"how much\b", r"what percentage\b", r"what is the (total|average|sum|count)",
    r"what is the (ratio|rate|proportion)", r"what fraction\b",
]


def _count_answer_rows(structured_json: str) -> int | None:
    """Count answer rows from structured_json only.

    Returns None if structured_json is absent or unparseable — callers must
    handle None by skipping the rule rather than falling back to stdout.
    """
    if not structured_json:
        return None
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if isinstance(answer, dict) and answer:
            lengths = [len(v) for v in answer.values() if isinstance(v, list)]
            if lengths:
                return lengths[0]
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _count_answer_cols(structured_json: str) -> int | None:
    """Count answer columns from structured_json."""
    if not structured_json:
        return None
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if isinstance(answer, dict):
            return len(answer)
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _check_shape(
    question: str,
    structured_json: str,
    spec: "QuestionSpec",
) -> list[HarnessFlag]:
    """Unified shape check: rows and columns vs question type + QA spec.

    Produces at most one BLOCK per dimension (rows, columns) to avoid
    redundant flags from the old separate rules.
    """
    flags: list[HarnessFlag] = []
    q_lower = question.lower()
    is_scalar_q = any(re.search(p, q_lower) for p in _SCALAR_PATTERNS)

    rows = _count_answer_rows(structured_json)

    # --- Row checks ---
    if rows is not None:
        # Scalar question with too many rows
        if is_scalar_q and rows > 5:
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="warn",
                message=(
                    f"Scalar question but answer has {rows} rows. "
                    f"Expected a single aggregated value. Add aggregation "
                    f"(COUNT/SUM/AVG/MIN/MAX) to reduce to one row."
                ),
            ))
        # QA spec: single expected but got many
        elif (spec.expected_row_count == "single" and rows > 5
              and not is_scalar_q):
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="warn",
                message=(
                    f"Expected single-row answer but answer has {rows} rows. "
                    f"Add aggregation to reduce to one result."
                ),
            ))
        # QA spec: multiple expected but got 1
        elif spec.expected_row_count == "multiple" and rows == 1:
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="warn",
                message=(
                    "Expected multiple rows but answer has only 1 row. "
                    "Verify the query isn't over-aggregating."
                ),
            ))
        # Scalar answer type with many rows (non-tie) — threshold matches
        # other scalar checks (>5) to avoid flagging legitimate 2-3 row results
        elif (spec.answer_type == "scalar" and rows > 5
              and not spec.tie_possible and not is_scalar_q):
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="warn",
                message=(
                    f"Expected scalar answer but got {rows} rows. "
                    f"Verify whether multiple results are valid (ties/multiple matches) "
                    f"before aggregating."
                ),
            ))

    # Column count checks handled by _check_qa_column_count() which uses
    # the more precise expected_column_count from QuestionAnalyzer.

    return flags


# ---------------------------------------------------------------------------
# Rule 3: (removed — phantom_filter had high false-positive rate due to
# domain rules introducing filter values not present in the question)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 4: JOIN row explosion
# ---------------------------------------------------------------------------

def _check_join_cardinality(stdout: str) -> list[HarnessFlag]:
    # Look for patterns like "After join: 50000 rows" vs "Loaded: 500 rows"
    loaded = re.findall(r"(?:Loaded|rows_loaded)[:\s]+(\d+)", stdout, re.IGNORECASE)
    joined = re.findall(r"(?:After join|post.?join|joined)[:\s]+(\d+)\s*rows?", stdout, re.IGNORECASE)
    if not loaded or not joined:
        return []
    max_loaded = max(int(n) for n in loaded)
    max_joined = max(int(n) for n in joined)
    if max_loaded > 0 and max_joined > max_loaded * 10:
        return [HarnessFlag(
            rule="join_cardinality",
            severity="warn",
            message=(
                f"JOIN row explosion: loaded {max_loaded} rows but "
                f"post-JOIN has {max_joined} rows ({max_joined // max(max_loaded, 1)}x). "
                f"Check JOIN keys for type mismatch or missing ON condition."
            ),
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 5: Empty output detection
# ---------------------------------------------------------------------------

_EMPTY_MARKERS = [
    "empty dataframe", "0 rows", "no data", "no results",
    "no matches found", "no records", "0 records",
]

_EXISTENCE_PATTERNS = [
    r"\bis there\b", r"\bare there\b", r"\bdoes .+ exist\b",
    r"\bhow many\b", r"\bcount\b",
]


def _check_empty_output(question: str, stdout: str) -> list[HarnessFlag]:
    stdout_lower = stdout.lower()
    if not any(marker in stdout_lower for marker in _EMPTY_MARKERS):
        return []
    # Don't flag existence/count questions where 0 is a valid answer
    q_lower = question.lower()
    if any(re.search(p, q_lower) for p in _EXISTENCE_PATTERNS):
        return []
    return [HarnessFlag(
        rule="empty_output",
        severity="block",
        message=(
            "Output appears empty (0 rows / empty DataFrame). "
            "Check filter conditions, column names, and data types. "
            "Print available values for the filtered column to diagnose."
        ),
    )]


# ---------------------------------------------------------------------------
# Rule 6: Row count upper bound
# ---------------------------------------------------------------------------

def _check_row_count_bound(
    stdout: str,
    data_profile: str,
) -> list[HarnessFlag]:
    # Extract source table max rows from data_profile
    source_rows = re.findall(r"(\d+)\s*rows?", data_profile, re.IGNORECASE)
    if not source_rows:
        return []
    max_source = max(int(n) for n in source_rows)
    if max_source == 0:
        return []

    # Extract output rows
    output_rows = re.findall(r"Result rows?:\s*(\d+)", stdout, re.IGNORECASE)
    if not output_rows:
        output_rows = re.findall(r"(\d+)\s*rows?\s*[×x×]\s*\d+\s*col", stdout, re.IGNORECASE)
    if not output_rows:
        return []

    max_output = max(int(n) for n in output_rows)
    if max_output > max_source * 2:
        return [HarnessFlag(
            rule="row_count_bound",
            severity="warn",
            message=(
                f"Output has {max_output} rows but largest source table "
                f"has {max_source} rows. Result exceeds 2x source — "
                f"possible cartesian product or duplicate rows."
            ),
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 7: (removed — official rules accept both split and merged name columns)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rule 7b: Extra columns detection
# ---------------------------------------------------------------------------

_DEBUG_COLUMN_PATTERNS = [
    r"^unnamed", r"^index$", r"^row_count$", r"^row_num",
    r"^level_\d+$", r"^__",
]


def _check_extra_columns(
    structured_json: str,
) -> list[HarnessFlag]:
    """Rule 7b: detect debug/index columns in answer.

    Scalar-with-multi-column check moved to unified _check_shape().
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    flags: list[HarnessFlag] = []
    for col_name in answer:
        col_lower = col_name.lower().strip()
        if any(re.match(p, col_lower) for p in _DEBUG_COLUMN_PATTERNS):
            flags.append(HarnessFlag(
                rule="extra_columns",
                severity="warn",
                message=(
                    f"Answer contains debug/index column '{col_name}'. "
                    f"Remove it — return only the columns the question "
                    f"requests; extras dilute the answer."
                ),
            ))
    return flags


# ---------------------------------------------------------------------------
# Rule 8: Placeholder / empty answer
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERNS = [
    r"^not applicable$", r"^n/?a$", r"^none$", r"^null$", r"^nan$",
    r"^\[\s*\]$", r"^\{\s*\}$", r"^$",
]

_RETRIEVAL_PATTERNS = [
    r"\bwhat is the\b", r"\bfind the\b", r"\bget the\b",
    r"\bretrieve\b", r"\blook up\b",
]


def _check_empty_answer(
    question: str,
    structured_json: str,
) -> list[HarnessFlag]:
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
    except (json.JSONDecodeError, ValueError):
        return []

    # Flatten answer values to a single string for checking
    answer_str = ""
    if isinstance(answer, dict):
        all_vals = []
        for vals in answer.values():
            if isinstance(vals, list):
                all_vals.extend(str(v) for v in vals)
            else:
                all_vals.append(str(vals))
        answer_str = " ".join(all_vals).strip()
    elif isinstance(answer, str):
        answer_str = answer.strip()

    if not answer_str:
        return []

    # Check for placeholder values
    for val_str in (answer_str,):
        if any(re.match(p, val_str.lower().strip()) for p in _PLACEHOLDER_PATTERNS):
            # For retrieval questions, N/A might be a legit answer
            q_lower = question.lower()
            if any(re.search(p, q_lower) for p in _RETRIEVAL_PATTERNS):
                return []
            return [HarnessFlag(
                rule="empty_answer",
                severity="block",
                message=(
                    f"Answer is a placeholder value ('{val_str[:50]}'). "
                    f"The computation likely failed or returned no result. "
                    f"Re-examine the query logic."
                ),
            )]

    return []


# ---------------------------------------------------------------------------
# Rule 8b: Dict-string answer detection
# ---------------------------------------------------------------------------

_DICT_STRING_RE = re.compile(r"^\{.*\}$", re.DOTALL)


_STDOUT_LEAK_MARKERS = (
    "[step_result] saved",
    "Result shape:",
    "=== ",  # session-banner-style debug headers
    "DataFrame:\n",
    "shape: (",
)


def _check_stdout_leak(structured_json: str) -> list[HarnessFlag]:
    """Reject answer values that are clearly stdout text, not real values.

    Failure mode: agent calls ``save_result(answer={"col": [stdout_string]})``
    where the string contains print artifacts ('[step_result] saved',
    '=== Verification ===', shape banners, etc.). Scorer compares this
    multi-line junk to a clean gold value and gives 0.

    Detects when ANY value in the answer dict is a string that contains
    a newline AND one of the known stdout-formatting markers.
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    for col, vals in answer.items():
        if not isinstance(vals, list):
            continue
        for v in vals:
            if not isinstance(v, str):
                continue
            if "\n" not in v:
                continue
            if any(m in v for m in _STDOUT_LEAK_MARKERS):
                return [HarnessFlag(
                    rule="stdout_leak",
                    severity="block",
                    message=(
                        f"Column '{col}' value contains captured stdout text "
                        f"(matched marker in: {v[:80]!r}). "
                        f"save_result() was called with print output as the "
                        f"answer. Compute the actual scalar/list and pass "
                        f"it directly, not the formatted log."
                    ),
                )]
    return []


def _check_dict_string_answer(structured_json: str) -> list[HarnessFlag]:
    """Rule 8b: detect save_result() called with a dict-as-string value.

    Pattern: agent computed a dict of intermediate values and called
    save_result(answer={"col": str(some_dict)}).  The scorer receives a
    string like "{'pct': 31.2, 'n': 750}" instead of the scalar 31.2.

    Detection: any answer value is a string that matches '{...}'.
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    for col, vals in answer.items():
        # Case A: value is a dict object directly — agent passed a dict instead of a list
        if isinstance(vals, dict):
            return [HarnessFlag(
                rule="dict_string_answer",
                severity="block",
                message=(
                    f"Column '{col}' value is a Python dict: {str(vals)[:80]}. "
                    f"save_result() must receive a list of scalar values, not a dict. "
                    f"Use: save_result(answer={{'col1': [val1], 'col2': [val2]}}) "
                    f"where each key maps to a list of row values."
                ),
            )]
        if not isinstance(vals, list):
            continue
        # Case B: value is a list but contains stringified dicts
        for v in vals:
            if isinstance(v, str) and _DICT_STRING_RE.match(v.strip()):
                return [HarnessFlag(
                    rule="dict_string_answer",
                    severity="block",
                    message=(
                        f"Column '{col}' value looks like a Python dict string: "
                        f"'{v[:80]}'. "
                        f"save_result() was called with str(dict) instead of the "
                        f"actual scalar value. Extract the correct scalar and pass "
                        f"it directly: save_result(answer={{'{col}': [value]}})."
                    ),
                )]
    return []


# ---------------------------------------------------------------------------
# Rule 8c: NaN / null value detection
# ---------------------------------------------------------------------------

_NAN_STRINGS = {"nan", "none", "null", "nat", "<na>", ""}

# Columns whose name matches these patterns are "key-like" — NaN in them
# almost certainly means a failed join/lookup, not legitimate missing data.
_KEY_COLUMN_RE = re.compile(
    r"(?i)(?:^id$|_id$|^key$|_key$|^code$|_code$|^name$|_name$|^title$|_title$"
    r"|^index$|^rowid$|^identifier$)",
)


def _col_has_nan(vals: list) -> tuple[bool, int, int]:
    """Check if a column's values contain NaN. Returns (has_nan, nan_count, total)."""
    total = len(vals)
    nan_count = sum(1 for v in vals if str(v).strip().lower() in _NAN_STRINGS)
    return nan_count > 0, nan_count, total


def _check_nan_values(structured_json: str) -> list[HarnessFlag]:
    """Rule 8c: NaN/null detection with per-column severity.

    Severity logic (precision-first):
    - All columns all-null → BLOCK (computation completely failed)
    - Key-like column has any null → BLOCK (join/lookup failed)
    - Non-key column with partial nulls → WARN (may be legitimate missing data)

    This avoids false-positive BLOCKs on list queries where gold answers
    legitimately contain NULL in non-key columns (e.g. task_199).
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict) or not answer:
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    # Classify columns
    key_nan_cols: list[str] = []
    nonkey_nan_cols: list[str] = []
    all_cols_all_null = True  # assume true, falsify below

    for col, vals in answer.items():
        if not isinstance(vals, list):
            vals = [vals]
        has_nan, nan_count, total = _col_has_nan(vals)

        if not has_nan or total == 0:
            all_cols_all_null = False
            continue

        # Column has at least one NaN
        col_all_null = (nan_count == total)

        if not col_all_null:
            all_cols_all_null = False

        is_key = bool(_KEY_COLUMN_RE.search(col))

        if is_key:
            key_nan_cols.append(col)
        elif col_all_null:
            nonkey_nan_cols.append(col)
        else:
            nonkey_nan_cols.append(col)

    if not key_nan_cols and not nonkey_nan_cols:
        return []

    flags: list[HarnessFlag] = []

    # Case 1: all answer columns entirely null → BLOCK
    if all_cols_all_null:
        all_null_cols = key_nan_cols + nonkey_nan_cols
        return [HarnessFlag(
            rule="nan_answer",
            severity="block",
            message=(
                f"All answer values are NaN/null (columns: {all_null_cols}). "
                f"The computation completely failed — check join keys, "
                f"column names, and data loading logic."
            ),
        )]

    # Case 2: key-like column has NaN → BLOCK
    if key_nan_cols:
        flags.append(HarnessFlag(
            rule="nan_answer",
            severity="block",
            message=(
                f"Key column(s) contain NaN/null: {key_nan_cols}. "
                f"This usually means a join returned no matches or a "
                f"column name was wrong. Check join keys and column names."
            ),
        ))

    # Case 3: non-key column partial null → WARN (may be legitimate)
    if nonkey_nan_cols and not key_nan_cols:
        flags.append(HarnessFlag(
            rule="nan_answer",
            severity="warn",
            message=(
                f"Non-key column(s) contain some NaN/null values: "
                f"{nonkey_nan_cols}. This may be legitimate missing data "
                f"or a computation issue. Verify the source data."
            ),
        ))

    return flags


# ---------------------------------------------------------------------------
# Rule 9: Value embellishment (formatting artifacts)
# ---------------------------------------------------------------------------

_EMBELLISHMENT_PATTERNS = [
    (r"\$\d", "dollar sign ($)"),
    (r"\d%", "percent sign (%)"),
    (r"\d\s*(days?|months?|years?|hours?|minutes?|seconds?)\b", "unit suffix"),
    (r"\b(approximately|about|around|roughly|~)\s*\d", "approximation prefix"),
]


def _check_value_embellishment(structured_json: str) -> list[HarnessFlag]:
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
    except (json.JSONDecodeError, ValueError):
        return []

    # Flatten to string values
    str_values: list[str] = []
    if isinstance(answer, dict):
        for vals in answer.values():
            if isinstance(vals, list):
                str_values.extend(str(v) for v in vals)
            else:
                str_values.append(str(vals))

    combined = " ".join(str_values)
    flags: list[HarnessFlag] = []
    for pattern, desc in _EMBELLISHMENT_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            flags.append(HarnessFlag(
                rule="value_embellishment",
                severity="block",
                message=(
                    f"Answer values contain {desc}. "
                    f"Scorer does exact string matching — "
                    f"formatting characters will cause match failure. "
                    f"Output raw numeric values without units or symbols."
                ),
            ))
            break  # one warning is enough
    return flags


# ---------------------------------------------------------------------------
# Rule 9b: Error string in answer
# ---------------------------------------------------------------------------

_ERROR_PREFIXES = [
    "error during", "error:", "exception:", "could not",
    "failed to", "no code generated", "unable to",
    "traceback (most recent call last)",
    "file not found", "filenotfounderror",
]


def _check_error_string_answer(structured_json: str) -> list[HarnessFlag]:
    """Rule 9b: detect error messages passed as answer values.

    Pattern: code caught an exception and called save_result(answer={"col": [error_msg]})
    instead of propagating the error. The scorer receives an error string instead of data.

    Safeguards: only matches values starting with known error prefixes AND
    longer than 10 characters to avoid false positives on short legitimate values.
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    for col, vals in answer.items():
        if not isinstance(vals, list):
            continue
        for v in vals:
            if not isinstance(v, str) or len(v) < 10:
                continue
            v_lower = v.strip().lower()
            for prefix in _ERROR_PREFIXES:
                if v_lower.startswith(prefix):
                    return [HarnessFlag(
                        rule="error_string_answer",
                        severity="block",
                        message=(
                            f"Answer contains error message in column '{col}': "
                            f"'{v[:80]}'. "
                            f"The code caught an error and passed it as the answer "
                            f"instead of fixing the underlying issue. "
                            f"Fix the data loading or computation error."
                        ),
                    )]
    return []


# ---------------------------------------------------------------------------
# Rule 9c: Excuse answer detection
# ---------------------------------------------------------------------------

_EXCUSE_PATTERNS = [
    r"(?:data|budget|file|information|table|column)\s+(?:not\s+found|unavailable|missing)",
    r"(?:cannot|can't|could not|couldn't|unable to)\s+(?:complete|find|determine|locate|access|calculate|compute)",
    r"no\s+(?:data|results?|records?|matching\s+\w+)\s+(?:found|available|exist)",
    r"(?:insufficient|inadequate)\s+(?:data|information)",
    r"data\s+(?:is\s+)?not\s+available",
]


def _check_excuse_answer(structured_json: str) -> list[HarnessFlag]:
    """Rule 9c: detect 'excuse answers' where the code gave up.

    Pattern: code couldn't find data and returned an explanation string
    like "Budget data unavailable" instead of computing an actual answer.

    Safeguards: only matches string values >= 10 chars with known excuse patterns.
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    for col, vals in answer.items():
        if not isinstance(vals, list):
            continue
        for v in vals:
            if not isinstance(v, str) or len(v) < 10:
                continue
            v_lower = v.strip().lower()
            for pattern in _EXCUSE_PATTERNS:
                if re.search(pattern, v_lower):
                    return [HarnessFlag(
                        rule="excuse_answer",
                        severity="block",
                        message=(
                            f"Answer contains an excuse instead of data in column "
                            f"'{col}': '{v[:80]}'. The code gave up instead of "
                            f"finding the data. Check file paths, table names, "
                            f"and column names in the data schema."
                        ),
                    )]
    return []


# ---------------------------------------------------------------------------
# Rules 10-13: QuestionSpec-based (QA rules)
# ---------------------------------------------------------------------------

def _check_qa_column_count(
    spec: QuestionSpec,
    structured_json: str,
) -> list[HarnessFlag]:
    """Rule 10: column count vs QA expectation.

    Missing columns → always BLOCK (reduces recall).
    Extra columns → BLOCK when ratio ≥ 3x (clearly wrong), else WARN.
    """
    if spec.expected_column_count <= 0 or not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    actual = len(answer)
    expected = spec.expected_column_count
    if actual < expected:
        return [HarnessFlag(
            rule="qa_column_count",
            severity="warn",
            message=(
                f"Expected {expected} columns but answer has {actual}. "
                f"Missing columns will reduce recall score."
            ),
        )]
    if actual > expected:
        # BLOCK when answer has 2x or more extra columns — clearly wrong.
        # WARN for mild violations (actual == expected + 1) to allow escalation.
        severity = "block" if actual >= expected * 2 else "warn"
        return [HarnessFlag(
            rule="qa_column_count",
            severity=severity,
            message=(
                f"Expected {expected} columns but answer has {actual}. "
                f"Extra columns ({list(answer.keys())}) — return ONLY "
                f"the columns the question asks for."
            ),
        )]
    return []


def _check_scalar_range(
    spec: QuestionSpec,
    question: str,
    structured_json: str,
    data_profile: str,
) -> list[HarnessFlag]:
    """Rule 13: scalar value range checks."""
    if spec.answer_type != "scalar" or spec.value_style != "numeric":
        return []
    if not structured_json:
        return []

    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    # Extract the numeric value (try each column until we find a number)
    value = None
    for vals in answer.values():
        if isinstance(vals, list) and vals:
            try:
                value = float(vals[0])
                break
            except (ValueError, TypeError):
                pass

    if value is None:
        return []

    q_lower = question.lower()
    flags: list[HarnessFlag] = []

    # Percentage check
    if any(w in q_lower for w in ["percentage", "percent", "%"]):
        if value < 0 or value > 100:
            flags.append(HarnessFlag(
                rule="scalar_range",
                severity="warn",
                message=(
                    f"Percentage question but value is {value} "
                    f"(expected 0-100). Check calculation."
                ),
            ))

    # Ratio check
    if any(w in q_lower for w in ["ratio", "proportion"]):
        if value < 0:
            flags.append(HarnessFlag(
                rule="scalar_range",
                severity="warn",
                message=f"Ratio question but value is negative ({value}).",
            ))

    # Count check
    if any(w in q_lower for w in ["how many", "count", "number of"]):
        if value < 0:
            flags.append(HarnessFlag(
                rule="scalar_range",
                severity="warn",
                message=f"Count question but value is negative ({value}).",
            ))
        # Check against source table size
        source_rows = re.findall(r"(\d+)\s*rows?", data_profile, re.IGNORECASE)
        if source_rows:
            max_source = max(int(n) for n in source_rows)
            if max_source > 0 and value > max_source * 2:
                flags.append(HarnessFlag(
                    rule="scalar_range",
                    severity="warn",
                    message=(
                        f"Count is {value} but largest source table has "
                        f"{max_source} rows. Count exceeds 2x source size."
                    ),
                ))

    return flags


# ---------------------------------------------------------------------------
# Rule 14: Tie-possible check (min/max questions with single-row result)
# ---------------------------------------------------------------------------

_TIE_QUESTION_PATTERNS = [
    r"\blowest\b", r"\bhighest\b", r"\bminimum\b", r"\bmaximum\b",
    r"\bmost\b", r"\bleast\b", r"\bfewest\b", r"\blargest\b", r"\bsmallest\b",
    r"\bmin\b", r"\bmax\b",
]

_LIMIT1_PATTERNS = re.compile(
    r"LIMIT\s+1\b|\.head\(1\)|\.iloc\[0\]|idxmin\(\)|idxmax\(\)|\.nsmallest\(1\)|\.nlargest\(1\)",
    re.IGNORECASE,
)


def _check_tie_possible(
    spec: QuestionSpec,
    question: str,
    code: str,
    structured_json: str,
) -> list[HarnessFlag]:
    """Rule 14: warn when a tie-possible question returns exactly 1 row.

    Fires when:
    - QuestionSpec marks tie_possible=True (from QuestionAnalyzer), OR
    - Question text contains min/max/lowest/highest keywords
    AND answer has exactly 1 row.

    Guides PlannerCoder to use RANK() or WHERE col = (SELECT MIN/MAX ...)
    instead of LIMIT 1 so tied values are not silently dropped.
    """
    tie_flagged = spec.tie_possible
    if not tie_flagged:
        q_lower = question.lower()
        tie_flagged = any(re.search(p, q_lower) for p in _TIE_QUESTION_PATTERNS)

    if not tie_flagged:
        return []

    data_rows = _count_answer_rows(structured_json)
    if data_rows is None or data_rows != 1:
        return []

    # Only warn when code used LIMIT 1 or similar — avoids false positives
    # when the data genuinely has a single extreme value
    if not _LIMIT1_PATTERNS.search(code):
        return []

    return [HarnessFlag(
        rule="tie_possible",
        severity="warn",
        message=(
            "Min/max question returned 1 row via LIMIT 1 / head(1) — ties may exist. "
            "Replace LIMIT 1 with WHERE col = (SELECT MIN/MAX(col) FROM ...) "
            "or use RANK() OVER (...) to capture all tied values."
        ),
    )]


# ---------------------------------------------------------------------------
# Rule 15: SQL Static Verifier (sqlglot AST analysis)
# ---------------------------------------------------------------------------

def _check_sql_static(
    code: str,
    data_profile: str,
    spec: QuestionSpec,
) -> list[HarnessFlag]:
    """Rule 15: SQL AST analysis for join keys, filter values, column count.

    Uses sqlglot to parse SQL and check:
    - JOIN keys against known columns/relations in data_profile
    - WHERE literal values against low-cardinality DISTINCT values
    - SELECT column count vs QuestionSpec expected

    Fail-open: parse failure → skip. Python code → skip.
    All checks produce WARN only (never BLOCK).
    """
    # Only analyze raw SQL or SQL embedded in duckdb.execute()
    sql = _extract_sql_from_code(code)
    if not sql:
        return []

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return []  # sqlglot not available → skip

    try:
        parsed = sqlglot.parse(sql, read="duckdb")
        if not parsed:
            return []
    except Exception:
        return []  # parse failure → fail-open

    flags: list[HarnessFlag] = []
    stmt = parsed[0]  # analyze first statement

    # --- Check SELECT column count vs QuestionSpec ---
    if spec.expected_column_count > 0:
        try:
            select_cols = [
                col for col in stmt.find_all(exp.Column)
                if _is_select_column(col, stmt)
            ]
            # Count star selects
            star_count = len(list(stmt.find_all(exp.Star)))
            if not star_count and select_cols:
                # Deduplicate by alias/name
                unique_cols = set()
                for col in select_cols:
                    unique_cols.add(str(col))
                actual = len(unique_cols)
                if actual > spec.expected_column_count * 2:
                    flags.append(HarnessFlag(
                        rule="sql_column_count",
                        severity="warn",
                        message=(
                            f"SQL SELECT has ~{actual} columns but question "
                            f"expects ~{spec.expected_column_count}. "
                            f"Remove extra columns to avoid score penalty."
                        ),
                    ))
        except Exception:
            pass  # fail-open

    # --- Check WHERE literals against DISTINCT values ---
    try:
        _check_where_literals(stmt, data_profile, flags)
    except Exception:
        pass  # fail-open

    return flags


def _extract_sql_from_code(code: str) -> str:
    """Extract SQL from raw SQL or Python-embedded duckdb.execute().

    Returns empty string if no SQL found.
    """
    stripped = code.strip()
    first_word = stripped.split()[0].upper() if stripped.split() else ""

    # Raw SQL
    sql_starters = ("SELECT", "WITH", "CREATE", "ATTACH", "PRAGMA")
    has_python = any(kw in code for kw in ("import ", "def ", "print(", "from ", "class "))
    if first_word in sql_starters and not has_python:
        return stripped

    # Embedded SQL in Python
    patterns = [
        r'\.(?:execute|sql)\s*\(\s*"""(.*?)"""',
        r"\.(?:execute|sql)\s*\(\s*'''(.*?)'''",
        r'\.(?:execute|sql)\s*\(\s*"((?:[^"\\]|\\.)*)"',
        r"\.(?:execute|sql)\s*\(\s*'((?:[^'\\]|\\.)*)'",
    ]
    for pattern in patterns:
        match = re.search(pattern, code, re.DOTALL)
        if match:
            sql = match.group(1).strip()
            if sql and len(sql) > 20:
                return sql

    return ""


def _is_select_column(col, stmt) -> bool:
    """Check if a Column expression is in the SELECT clause (not WHERE/JOIN)."""
    try:
        from sqlglot import exp
        parent = col.parent
        # Walk up to find if we're in a Select expression
        while parent is not None:
            if isinstance(parent, exp.Select):
                return True
            if isinstance(parent, (exp.Where, exp.Join, exp.On, exp.Group, exp.Order)):
                return False
            parent = parent.parent
    except Exception:
        pass
    return False


def _check_where_literals(stmt, data_profile: str, flags: list[HarnessFlag]) -> None:
    """Check WHERE clause literal values against known DISTINCT values.

    Only checks low-cardinality fields (DISTINCT values listed in profile).
    Fail-open on any ambiguity.
    """
    from sqlglot import exp

    # Parse DISTINCT values from data_profile
    # Format in profile: "col_name (dtype, N distinct): val1, val2, val3"
    distinct_map: dict[str, set[str]] = {}
    for line in data_profile.split("\n"):
        match = re.match(r"\s*(\w+)\s+\([^)]*\d+\s*distinct\):\s*(.+)", line, re.IGNORECASE)
        if match:
            col_name = match.group(1).lower()
            values = {v.strip().lower().strip("'\"") for v in match.group(2).split(",")}
            if len(values) <= 50:  # only low-cardinality
                distinct_map[col_name] = values

    if not distinct_map:
        return

    # Find EQ conditions in WHERE
    for eq in stmt.find_all(exp.EQ):
        try:
            left = eq.left
            right = eq.right
            col_name = None
            literal_val = None

            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                col_name = left.name.lower()
                literal_val = right.this.lower()
            elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
                col_name = right.name.lower()
                literal_val = left.this.lower()

            if col_name and literal_val and col_name in distinct_map:
                known_values = distinct_map[col_name]
                if literal_val not in known_values:
                    flags.append(HarnessFlag(
                        rule="sql_where_value",
                        severity="warn",
                        message=(
                            f"WHERE {col_name} = '{literal_val}' but this value "
                            f"is not in the known DISTINCT values for {col_name}. "
                            f"Known values: {sorted(list(known_values))[:10]}. "
                            f"Check spelling and case sensitivity."
                        ),
                    ))
        except Exception:
            continue  # fail-open per condition


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check(
    question: str,
    code: str,
    stdout: str,
    return_code: int,
    data_profile: str,
    question_spec: QuestionSpec | None,
    structured_json: str,
) -> list[HarnessFlag]:
    """Run all deterministic rules. Returns list of HarnessFlags.

    Called between Sandbox and Judge in the main loop.
    Empty list → no issues, proceed to Judge normally.
    """
    # Skip if code execution failed — debugger handles that
    if return_code != 0:
        return []

    spec = question_spec or QuestionSpec()
    flags: list[HarnessFlag] = []

    # Core checks
    flags.extend(_check_agg_type(question, code))
    flags.extend(_check_shape(question, structured_json, spec))
    flags.extend(_check_join_cardinality(stdout))
    flags.extend(_check_empty_output(question, stdout))
    flags.extend(_check_row_count_bound(stdout, data_profile))
    # Rule 7 (column_merge) removed — official rules accept both name formats
    flags.extend(_check_extra_columns(structured_json))
    flags.extend(_check_empty_answer(question, structured_json))
    flags.extend(_check_dict_string_answer(structured_json))
    flags.extend(_check_stdout_leak(structured_json))
    flags.extend(_check_nan_values(structured_json))
    flags.extend(_check_value_embellishment(structured_json))
    flags.extend(_check_error_string_answer(structured_json))
    flags.extend(_check_excuse_answer(structured_json))

    # QA-spec checks
    flags.extend(_check_qa_column_count(spec, structured_json))
    flags.extend(_check_scalar_range(spec, question, structured_json, data_profile))

    # Tie-possible check
    flags.extend(_check_tie_possible(spec, question, code, structured_json))

    # SQL static analysis (fail-open)
    flags.extend(_check_sql_static(code, data_profile, spec))

    return flags
