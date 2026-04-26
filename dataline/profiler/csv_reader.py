"""CSV file profiling with enriched column statistics."""

from __future__ import annotations

import csv as _csv
import logging
import os

import pandas as pd

from ..core.types import ManifestEntry
from .column_stats import compute_column_stats, compressed_value_repr, detect_anomalies, safe_scalar

logger = logging.getLogger(__name__)


def read_csv(file_path: str) -> ManifestEntry:
    """Profile a CSV file into a ManifestEntry with enriched stats."""
    size = os.path.getsize(file_path)
    try:
        df = pd.read_csv(file_path, nrows=100)
    except Exception:
        df = pd.read_csv(file_path, nrows=100, encoding="latin-1")

    columns = []
    anomalies: list[str] = []
    for col in df.columns:
        col_info: dict = {
            "name": str(col),
            "dtype": str(df[col].dtype),
            "null_pct": round(float(df[col].isna().mean()), 3),
        }
        if pd.api.types.is_numeric_dtype(df[col]):
            col_info["min"] = safe_scalar(df[col].min())
            col_info["max"] = safe_scalar(df[col].max())

        # Enriched stats (deterministic)
        col_info.update(compute_column_stats(df[col], col_name=str(col)))
        col_info["value_repr"] = compressed_value_repr(df[col])

        # Keep raw sample for backward compat
        col_info["sample"] = [safe_scalar(v) for v in df[col].dropna().head(3).tolist()]

        columns.append(col_info)
        anomalies.extend(detect_anomalies(df[col], str(col)))

    # Get full row count using csv.reader (handles multiline quoted fields correctly)
    try:
        with open(file_path, newline="", encoding="utf-8") as f:
            row_count = max(sum(1 for _ in _csv.reader(f)) - 1, 0)
    except UnicodeDecodeError:
        try:
            with open(file_path, newline="", encoding="latin-1") as f:
                row_count = max(sum(1 for _ in _csv.reader(f)) - 1, 0)
        except Exception:
            row_count = len(df)
    except Exception:
        row_count = len(df)

    sample_rows = df.head(3).to_dict(orient="records")

    # Full-file DISTINCT value scan for low-cardinality text columns
    _scan_distinct_values(file_path, columns, size)

    summary: dict = {
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample_rows,
    }
    if anomalies:
        summary["anomalies"] = anomalies

    return ManifestEntry(
        file_path=file_path,
        file_type="csv",
        size_bytes=size,
        summary=summary,
    )


_DISTINCT_LIMIT = 30  # Max unique values to enumerate
_LARGE_FILE_THRESHOLD = 100_000_000  # 100MB — read only text cols above this


def _scan_distinct_values(
    file_path: str, columns: list[dict], file_size: int,
) -> None:
    """Scan full CSV file to compute exact cardinality and DISTINCT values.

    For text columns with ≤ _DISTINCT_LIMIT unique values, stores all distinct
    values. For all columns, stores exact cardinality and null percentage.

    For files > 100MB, only reads text columns (usecols) to limit memory.
    Mutates column dicts in place.
    """
    text_cols = [c["name"] for c in columns if c["dtype"] == "object"]

    # For large files, only read text columns to save memory.
    # If no text columns exist in a large file, skip entirely — numeric stats
    # are already computed from the 100-row sample.
    if file_size >= _LARGE_FILE_THRESHOLD and not text_cols:
        return
    use_cols = text_cols if (file_size >= _LARGE_FILE_THRESHOLD) else None

    try:
        for enc in ("utf-8", "latin-1"):
            try:
                df_full = pd.read_csv(file_path, encoding=enc, usecols=use_cols)
                break
            except UnicodeDecodeError:
                continue
        else:
            return
    except Exception:
        return

    col_lookup = {c["name"]: c for c in columns}
    for col_name in df_full.columns:
        if col_name not in col_lookup:
            continue
        col_info = col_lookup[col_name]
        n_unique = int(df_full[col_name].nunique())
        col_info["cardinality"] = n_unique
        col_info["null_pct"] = round(float(df_full[col_name].isna().mean()), 3)

        # Enumerate distinct values for low-cardinality text columns
        if col_name in text_cols and n_unique <= _DISTINCT_LIMIT:
            vals = df_full[col_name].dropna().unique().tolist()
            # Skip if any value is very long (XML blobs, JSON strings)
            if all(len(str(v)) <= 100 for v in vals):
                col_info["distinct_values"] = sorted(str(v) for v in vals)


