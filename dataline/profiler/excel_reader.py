"""Excel file profiling with enriched column statistics."""

from __future__ import annotations

import logging
import os

import pandas as pd

from ..core.types import ManifestEntry
from .column_stats import compute_column_stats, compressed_value_repr, safe_scalar

logger = logging.getLogger(__name__)

_DISTINCT_LIMIT = 30  # Max unique values to enumerate


def _scan_distinct_values(df: pd.DataFrame, columns: list[dict]) -> None:
    """Compute exact cardinality and DISTINCT values from full DataFrame.

    For text columns with ≤ _DISTINCT_LIMIT unique values, stores all distinct
    values. Mutates column dicts in place.
    """
    col_lookup = {c["name"]: c for c in columns}
    for col_name in df.columns:
        str_name = str(col_name)
        if str_name not in col_lookup:
            continue
        col_info = col_lookup[str_name]
        n_unique = int(df[col_name].nunique())
        col_info["cardinality"] = n_unique
        col_info["null_pct"] = round(float(df[col_name].isna().mean()), 3)

        # Enumerate distinct values for low-cardinality text columns
        if str(df[col_name].dtype) == "object" and n_unique <= _DISTINCT_LIMIT:
            vals = df[col_name].dropna().unique().tolist()
            if all(len(str(v)) <= 100 for v in vals):
                col_info["distinct_values"] = sorted(str(v) for v in vals)


def read_excel(file_path: str) -> ManifestEntry:
    """Profile an Excel file into a ManifestEntry."""
    size = os.path.getsize(file_path)

    try:
        xls = pd.ExcelFile(file_path)
        sheets = []
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet_name, nrows=100)
            columns = []
            for col in df.columns:
                col_info: dict = {
                    "name": str(col),
                    "dtype": str(df[col].dtype),
                    "null_pct": round(float(df[col].isna().mean()), 3),
                    "sample": [safe_scalar(v) for v in df[col].dropna().head(3).tolist()],
                }
                # Enriched stats
                col_info.update(compute_column_stats(df[col], col_name=str(col)))
                col_info["value_repr"] = compressed_value_repr(df[col])
                columns.append(col_info)

            # Get row count and DISTINCT values from full sheet
            try:
                full_df = pd.read_excel(xls, sheet_name=sheet_name, header=0)
                sheet_row_count = len(full_df)
                _scan_distinct_values(full_df, columns)
            except Exception:
                sheet_row_count = len(df)

            sheets.append({
                "name": sheet_name,
                "row_count": sheet_row_count,
                "columns": columns,
                "sample_rows": df.head(3).to_dict(orient="records"),
            })

        return ManifestEntry(
            file_path=file_path, file_type="excel", size_bytes=size,
            summary={"sheets": sheets},
        )
    except Exception as e:
        return ManifestEntry(
            file_path=file_path, file_type="excel", size_bytes=size,
            summary={"error": str(e)},
        )


