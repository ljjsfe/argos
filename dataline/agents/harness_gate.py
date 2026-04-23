"""HarnessGate: deterministic verification layer between Sandbox and Judge.

Zero LLM cost. 13 rules derived from v4 eval failure-mode analysis.

Severity levels:
- "block": skip Judge, use message as guidance for next iteration
- "warn":  pass to Judge as reference information

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
# Rule 2: Scalar question producing too many rows
# ---------------------------------------------------------------------------

_SCALAR_PATTERNS = [
    r"how many\b", r"how much\b", r"what percentage\b", r"what is the (total|average|sum|count)",
    r"what is the (ratio|rate|proportion)", r"what fraction\b",
]


def _count_data_lines(stdout: str) -> int:
    """Count non-header, non-empty lines in stdout."""
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    # Skip common header patterns
    if lines and re.match(r"^\s*[\w_]+\s{2,}", lines[0]):
        lines = lines[1:]  # skip DataFrame header
    return len(lines)


def _count_answer_rows(structured_json: str, stdout: str) -> int:
    """Count answer rows preferring structured_json over stdout line counting.

    stdout line counting is unreliable — agents print many diagnostic lines.
    structured_json reflects the actual answer shape.
    """
    if structured_json:
        try:
            data = json.loads(structured_json)
            answer = data.get("answer", {})
            if isinstance(answer, dict) and answer:
                lengths = [len(v) for v in answer.values() if isinstance(v, list)]
                if lengths:
                    return lengths[0]
        except (json.JSONDecodeError, ValueError):
            pass
    return _count_data_lines(stdout)


def _check_output_shape(question: str, stdout: str, structured_json: str) -> list[HarnessFlag]:
    q_lower = question.lower()
    if not any(re.search(p, q_lower) for p in _SCALAR_PATTERNS):
        return []
    data_rows = _count_answer_rows(structured_json, stdout)
    if data_rows > 5:
        return [HarnessFlag(
            rule="output_shape",
            severity="block",
            message=(
                f"Scalar question but output has {data_rows} answer rows. "
                f"Expected a single aggregated value. Add aggregation "
                f"(COUNT/SUM/AVG/MIN/MAX) to reduce to one row."
            ),
        )]
    return []


# ---------------------------------------------------------------------------
# Rule 3: Phantom filter conditions
# ---------------------------------------------------------------------------

_JSON_STRUCTURAL_WORDS = frozenset({
    "records", "data", "columns", "index", "values", "orient",
    "split", "table", "true", "false", "null", "none",
})

_DATE_PATTERN = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}")


def _check_phantom_filter(question: str, code: str) -> list[HarnessFlag]:
    # Extract string literals from WHERE clauses
    where_matches = re.findall(
        r"WHERE\s+.*?'([^']+)'",
        code,
        re.IGNORECASE | re.DOTALL,
    )
    if not where_matches:
        return []

    q_lower = question.lower()
    flags: list[HarnessFlag] = []
    for literal in where_matches:
        lit_lower = literal.lower().strip()
        if lit_lower in _JSON_STRUCTURAL_WORDS:
            continue
        if _DATE_PATTERN.match(literal):
            continue
        if lit_lower not in q_lower:
            flags.append(HarnessFlag(
                rule="phantom_filter",
                severity="warn",
                message=(
                    f"WHERE clause uses string '{literal}' which does not "
                    f"appear in the question. This may be a hallucinated "
                    f"filter value (or it may come from domain rules)."
                ),
            ))
    return flags


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
    question: str,
    structured_json: str,
) -> list[HarnessFlag]:
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

    # (a) Debug columns
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

    # (b) Scalar question with multiple columns
    q_lower = question.lower()
    if any(re.search(p, q_lower) for p in _SCALAR_PATTERNS):
        if len(answer) > 1:
            flags.append(HarnessFlag(
                rule="extra_columns",
                severity="block",
                message=(
                    f"Scalar question but answer has {len(answer)} columns "
                    f"({list(answer.keys())}). Expected 1 column. "
                    f"Remove extra columns to avoid score penalty."
                ),
            ))

    return flags


# ---------------------------------------------------------------------------
# Rule 8: Placeholder / empty answer
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERNS = [
    r"^not applicable$", r"^n/?a$", r"^none$", r"^null$",
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
    """Rule 10: column count vs QA expectation."""
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
        return [HarnessFlag(
            rule="qa_column_count",
            severity="warn",
            message=(
                f"Expected {expected} columns but answer has {actual}. "
                f"Extra columns ({list(answer.keys())}) may reduce score."
            ),
        )]
    return []


def _check_qa_row_count(
    spec: QuestionSpec,
    stdout: str,
    structured_json: str,
) -> list[HarnessFlag]:
    """Rule 11: row count vs QA expectation."""
    if spec.expected_row_count == "unknown":
        return []
    data_rows = _count_answer_rows(structured_json, stdout)
    if spec.expected_row_count == "single" and data_rows > 5:
        return [HarnessFlag(
            rule="qa_row_count",
            severity="block",
            message=(
                f"Expected single-row answer but output has {data_rows} rows. "
                f"Add aggregation to reduce to one result."
            ),
        )]
    if spec.expected_row_count == "multiple" and data_rows == 1:
        return [HarnessFlag(
            rule="qa_row_count",
            severity="warn",
            message=(
                "Expected multiple rows but output has only 1 row. "
                "Verify the query isn't over-aggregating."
            ),
        )]
    return []


def _check_qa_answer_type(
    spec: QuestionSpec,
    structured_json: str,
) -> list[HarnessFlag]:
    """Rule 12: shape vs QA answer_type."""
    if spec.answer_type != "scalar" or not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if not isinstance(answer, dict):
            return []
    except (json.JSONDecodeError, ValueError):
        return []

    num_cols = len(answer)
    max_rows = max((len(v) for v in answer.values() if isinstance(v, list)), default=0)

    if num_cols > 1 or max_rows > 1:
        return [HarnessFlag(
            rule="qa_answer_type",
            severity="block",
            message=(
                f"Expected scalar answer but got {num_cols} columns, "
                f"{max_rows} rows. Reduce to a single aggregated value."
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

    # Rules 1-9: always run
    flags.extend(_check_agg_type(question, code))
    flags.extend(_check_output_shape(question, stdout, structured_json))
    flags.extend(_check_phantom_filter(question, code))
    flags.extend(_check_join_cardinality(stdout))
    flags.extend(_check_empty_output(question, stdout))
    flags.extend(_check_row_count_bound(stdout, data_profile))
    flags.extend(_check_column_merge(code, structured_json))
    flags.extend(_check_extra_columns(question, structured_json))
    flags.extend(_check_empty_answer(question, structured_json))
    flags.extend(_check_value_embellishment(structured_json))

    # Rules 10-13: QA rules (only if spec has non-unknown values)
    flags.extend(_check_qa_column_count(spec, structured_json))
    flags.extend(_check_qa_row_count(spec, stdout, structured_json))
    flags.extend(_check_qa_answer_type(spec, structured_json))
    flags.extend(_check_scalar_range(spec, question, structured_json, data_profile))

    return flags
