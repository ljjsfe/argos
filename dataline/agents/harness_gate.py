"""HarnessGate: deterministic verification layer between Sandbox and Judge.

Zero LLM cost. Rules derived from eval failure-mode analysis.

Severity levels:
- "block": skip Judge, use message as guidance for next iteration
- "warn":  pass to Judge as reference information

Block fatigue (in orchestrator): same rule blocking ≥3 consecutive times
→ downgraded to warn so Judge can accept partial results.

If no flags → normal Judge flow.
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


def _check_agg_type(question: str, code: str) -> list[HarnessFlag]:
    q_lower = question.lower()
    flags: list[HarnessFlag] = []
    for keywords, expected, forbidden in _AGG_RULES:
        if not any(kw in q_lower for kw in keywords):
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
    cols = _count_answer_cols(structured_json)

    # --- Row checks ---
    if rows is not None:
        # Scalar question with too many rows
        if is_scalar_q and rows > 5:
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="block",
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
                severity="block",
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
        # Scalar answer type with multiple rows (non-tie)
        elif (spec.answer_type == "scalar" and rows > 1
              and not spec.tie_possible and not is_scalar_q):
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="block",
                message=(
                    f"Expected scalar answer but got {rows} rows. "
                    f"Reduce to a single aggregated value."
                ),
            ))

    # --- Column checks ---
    if cols is not None:
        # Scalar question with multiple columns
        if is_scalar_q and cols > 1:
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="block",
                message=(
                    f"Scalar question but answer has {cols} columns. "
                    f"Expected 1 column. Remove extra columns."
                ),
            ))
        elif spec.answer_type == "scalar" and cols > 1 and not is_scalar_q:
            flags.append(HarnessFlag(
                rule="output_shape",
                severity="block",
                message=(
                    f"Expected scalar answer but got {cols} columns. "
                    f"Reduce to 1 column."
                ),
            ))

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
            severity="block",
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
# Rule 7: Column merge detection
# ---------------------------------------------------------------------------

_COMPOSITE_COLUMNS: dict[str, list[str]] = {
    "full_name": ["first_name", "last_name"],
    "fullname": ["firstname", "lastname"],
    "name": ["first_name", "last_name"],
    "full_address": ["street", "city", "state", "zip"],
    "address": ["street", "city"],
}


def _check_column_merge(code: str, structured_json: str) -> list[HarnessFlag]:
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    answer_keys_lower = {k.lower() for k in answer}
    code_lower = code.lower()
    flags: list[HarnessFlag] = []

    for composite, parts in _COMPOSITE_COLUMNS.items():
        if composite in answer_keys_lower:
            if all(part in code_lower for part in parts):
                flags.append(HarnessFlag(
                    rule="column_merge",
                    severity="block",
                    message=(
                        f"Answer has merged column '{composite}' but code "
                        f"references {parts}. Scorer matches columns "
                        f"independently — keep them as separate columns."
                    ),
                ))
    return flags


# ---------------------------------------------------------------------------
# Rule 7b: Extra columns detection
# ---------------------------------------------------------------------------

_DEBUG_COLUMN_PATTERNS = [
    r"^unnamed", r"^index$", r"^row_count$", r"^row_num",
    r"^level_\d+$", r"^__", r"^count$",
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
                severity="block",
                message=(
                    f"Answer contains debug/index column '{col_name}'. "
                    f"Remove it — extra columns reduce score "
                    f"(Score = Recall − λ×ExtraCols/PredCols)."
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
        if not isinstance(vals, list):
            continue
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


def _check_nan_values(structured_json: str) -> list[HarnessFlag]:
    """Rule 8c: block answers that contain NaN/null values.

    A NaN in a structured answer almost always means the computation failed
    silently (e.g. missing join key, wrong column name, division by zero).
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

    nan_cols: list[str] = []
    for col, vals in answer.items():
        if not isinstance(vals, list):
            vals = [vals]
        for v in vals:
            if str(v).strip().lower() in _NAN_STRINGS:
                nan_cols.append(col)
                break

    if not nan_cols:
        return []

    return [HarnessFlag(
        rule="nan_answer",
        severity="block",
        message=(
            f"Answer contains NaN/null values in column(s): {nan_cols}. "
            f"This usually means a join returned no matches, a column name "
            f"was wrong, or a computation produced NaN. Check your data "
            f"loading and computation logic."
        ),
    )]


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
                severity="warn",
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
            severity="block",
            message=(
                f"Expected {expected} columns but answer has {actual}. "
                f"Missing columns will reduce recall score."
            ),
        )]
    if actual > expected:
        severity = "block" if actual >= expected * 3 else "warn"
        return [HarnessFlag(
            rule="qa_column_count",
            severity=severity,
            message=(
                f"Expected {expected} columns but answer has {actual}. "
                f"Extra columns ({list(answer.keys())}) reduce score "
                f"(Score = Recall − λ×ExtraCols/PredCols). "
                f"Return ONLY the columns the question asks for."
            ),
        )]
    return []


    # Rules 11 and 12 (qa_row_count, qa_answer_type) merged into _check_shape()


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

    # Extract the numeric value
    value = None
    for vals in answer.values():
        if isinstance(vals, list) and vals:
            try:
                value = float(vals[0])
            except (ValueError, TypeError):
                pass
        break

    if value is None:
        return []

    q_lower = question.lower()
    flags: list[HarnessFlag] = []

    # Percentage check
    if any(w in q_lower for w in ["percentage", "percent", "%"]):
        if value < 0 or value > 100:
            flags.append(HarnessFlag(
                rule="scalar_range",
                severity="block",
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
                severity="block",
                message=f"Count question but value is negative ({value}).",
            ))
        # Check against source table size
        source_rows = re.findall(r"(\d+)\s*rows?", data_profile, re.IGNORECASE)
        if source_rows:
            max_source = max(int(n) for n in source_rows)
            if max_source > 0 and value > max_source * 2:
                flags.append(HarnessFlag(
                    rule="scalar_range",
                    severity="block",
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
    flags.extend(_check_column_merge(code, structured_json))
    flags.extend(_check_extra_columns(structured_json))
    flags.extend(_check_empty_answer(question, structured_json))
    flags.extend(_check_dict_string_answer(structured_json))
    flags.extend(_check_nan_values(structured_json))
    flags.extend(_check_value_embellishment(structured_json))

    # QA-spec checks
    flags.extend(_check_qa_column_count(spec, structured_json))
    flags.extend(_check_scalar_range(spec, question, structured_json, data_profile))

    # Tie-possible check
    flags.extend(_check_tie_possible(spec, question, code, structured_json))

    return flags
