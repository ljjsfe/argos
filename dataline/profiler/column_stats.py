"""Deterministic per-column statistical enrichment.

Zero LLM cost. All functions operate on pandas Series or raw Python values.
Used by csv_reader, json_reader, sqlite_reader, excel_reader to enrich
column metadata beyond basic dtype/sample.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# --- Date-like string detection ---

_DATE_PATTERNS = (
    re.compile(r"^\d{4}-\d{2}-\d{2}"),           # 2024-01-15...
    re.compile(r"^\d{2}/\d{2}/\d{4}"),            # 01/15/2024
    re.compile(r"^\d{2}-\d{2}-\d{4}"),            # 15-01-2024
    re.compile(r"^\d{4}/\d{2}/\d{2}"),            # 2024/01/15
)

_MIN_DATE_CHECK = 5  # need at least 5 non-null values to flag date-like


def compute_column_stats(series: pd.Series, col_name: str = "") -> dict[str, Any]:
    """Compute deterministic enrichment stats for a single column.

    Returns a dict to merge into the existing column info dict.
    """
    total = len(series)
    non_null = series.dropna()
    n = len(non_null)

    stats: dict[str, Any] = {
        "completeness": round(n / max(total, 1), 3),
        "uniqueness_ratio": round(non_null.nunique() / max(n, 1), 3) if n > 0 else 0.0,
    }

    # Flags
    flags: list[str] = []

    if n == 0:
        stats["flags"] = flags
        return stats

    # Constant column detection
    if non_null.nunique() == 1:
        flags.append("constant")

    # Numeric stats
    if pd.api.types.is_numeric_dtype(series):
        stats.update(_numeric_stats(non_null))
    else:
        # Categorical stats (only when cardinality is manageable)
        nunique = non_null.nunique()
        if nunique <= 50:
            stats["top_values"] = _top_values(non_null, top_n=5)

        # Date-like detection for string columns
        if _is_date_like(non_null):
            flags.append("date_like")

        # Mixed-type detection
        if _is_mixed_type(non_null):
            flags.append("mixed_type")

        # JSON-ish string detection (dict/list embedded as strings)
        if _is_dict_like_string(non_null):
            flags.append("dict_like_string")

    # ID candidate detection: high uniqueness + not constant
    if stats["uniqueness_ratio"] > 0.9 and n > 5 and "constant" not in flags:
        flags.append("id_candidate")

    # PK/FK inference based on column name pattern + uniqueness
    key_type = infer_key_type(col_name, stats["uniqueness_ratio"], n)
    if key_type:
        flags.append(f"candidate_{key_type}")

    stats["flags"] = flags
    return stats


def compressed_value_repr(series: pd.Series) -> dict[str, Any]:
    """Produce a compressed value representation, more info-dense than raw samples.

    Returns: {value_type, cardinality, range_or_top, sample}
    """
    non_null = series.dropna()
    n = len(non_null)

    if n == 0:
        return {"value_type": "empty", "cardinality": 0}

    nunique = non_null.nunique()
    result: dict[str, Any] = {"cardinality": int(nunique)}

    if pd.api.types.is_bool_dtype(series):
        result["value_type"] = "boolean"
        vc = non_null.value_counts()
        result["distribution"] = {str(k): int(v) for k, v in vc.items()}
    elif pd.api.types.is_numeric_dtype(series):
        result["value_type"] = "numeric"
        result["range"] = [safe_scalar(non_null.min()), safe_scalar(non_null.max())]
        result["sample"] = [safe_scalar(v) for v in non_null.head(2).tolist()]
    else:
        result["value_type"] = "string"
        if nunique <= 10:
            result["all_values"] = sorted(non_null.unique().tolist())[:10]
        else:
            result["sample"] = [str(v) for v in non_null.head(3).tolist()]

    return result


def detect_anomalies(series: pd.Series, col_name: str) -> list[str]:
    """Return list of anomaly flags for a column. Deterministic."""
    anomalies: list[str] = []
    non_null = series.dropna()

    if len(non_null) == 0:
        return anomalies

    if _is_mixed_type(non_null):
        anomalies.append(f"{col_name}: mixed types detected")

    if not pd.api.types.is_numeric_dtype(series) and _is_date_like(non_null):
        anomalies.append(f"{col_name}: date-like strings (consider parsing)")

    # Numeric-looking strings
    if series.dtype == object:
        numeric_count = sum(1 for v in non_null.head(20) if _looks_numeric(str(v)))
        if numeric_count > 10:
            anomalies.append(f"{col_name}: numeric values stored as strings")

    return anomalies


# --- Private helpers ---


def _numeric_stats(non_null: pd.Series) -> dict[str, Any]:
    """Compute numeric summary stats. Handles inf/nan gracefully."""
    result: dict[str, Any] = {}
    try:
        desc = non_null.describe()
        result["mean"] = _safe_float(desc.get("mean"))
        result["std"] = _safe_float(desc.get("std"))
        result["q25"] = _safe_float(desc.get("25%"))
        result["median"] = _safe_float(desc.get("50%"))
        result["q75"] = _safe_float(desc.get("75%"))
    except (TypeError, ValueError) as exc:
        logger.warning("numeric_stats failed: %s", exc)
    return result


def _top_values(series: pd.Series, top_n: int = 5) -> list[dict[str, Any]]:
    """Return top-N value counts with percentages."""
    total = len(series)
    vc = series.value_counts().head(top_n)
    return [
        {"value": _safe_scalar(val), "pct": round(count / total, 3)}
        for val, count in vc.items()
    ]


def _is_date_like(series: pd.Series) -> bool:
    """Check if string series looks like dates."""
    if series.dtype != object:
        return False
    sample = series.dropna().head(20)
    if len(sample) < _MIN_DATE_CHECK:
        return False
    matches = sum(
        1 for v in sample
        if isinstance(v, str) and any(p.match(v) for p in _DATE_PATTERNS)
    )
    return matches / len(sample) > 0.5


def _is_dict_like_string(series: pd.Series) -> bool:
    """Check if a string column holds stringified dict/list values.

    Heuristic: ≥60% of sampled non-null values start with '{' or '['
    AND end with the matching close. Caught early so the agent can use
    parse_jsonish_column / explode_jsonish_column instead of treating
    the whole cell as an opaque string.
    """
    if series.dtype != object:
        return False
    sample = series.dropna().head(20)
    if len(sample) < 5:
        return False
    hits = 0
    for v in sample:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            hits += 1
    return hits / len(sample) >= 0.6


def _is_mixed_type(series: pd.Series) -> bool:
    """Check if series has genuinely mixed Python types."""
    if series.dtype != object:
        return False
    sample = series.dropna().head(20)
    types = {type(v).__name__ for v in sample}
    # object columns that are all strings are not mixed
    return len(types) > 1 and types != {"str"}


def _looks_numeric(v: str) -> bool:
    """Check if a string looks like a number (with currency/percentage markers)."""
    cleaned = v.strip().lstrip("$€£¥").rstrip("%").replace(",", "")
    try:
        float(cleaned)
        return True
    except (ValueError, TypeError):
        return False


def safe_scalar(v: object) -> object:
    """Convert numpy scalars to Python native types."""
    if hasattr(v, "item"):
        return v.item()
    return v


# Keep private alias for internal use
_safe_scalar = safe_scalar


# --- Key inference ---

_KEY_PATTERNS = re.compile(
    r"(?:^id$|_id$|_key$|_code$|^pk$|^fk_|^key$|^uuid$|^guid$)",
    re.IGNORECASE,
)


def infer_key_type(col_name: str, uniqueness_ratio: float, n: int) -> str | None:
    """Infer if column is a candidate primary/foreign key.

    Returns "primary_key", "foreign_key", or None.

    Heuristics:
    - PK: uniqueness >= 0.99 AND name matches key pattern AND n > 5
    - FK: uniqueness < 0.99 AND uniqueness > 0.01 AND name matches key pattern
    """
    if not col_name or n <= 5:
        return None

    if not _KEY_PATTERNS.search(col_name):
        return None

    if uniqueness_ratio >= 0.99:
        return "primary_key"
    if uniqueness_ratio > 0.01:
        return "foreign_key"
    return None


def _safe_float(v: object) -> float | None:
    """Safely convert to float, returning None for inf/nan."""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except (ValueError, TypeError):
        return None
