"""Parquet file profiling via pyarrow."""

from __future__ import annotations

import logging
import os

from ..core.types import ManifestEntry
from .column_stats import compute_column_stats, compressed_value_repr

logger = logging.getLogger(__name__)

_DISTINCT_LIMIT = 30  # Max unique values to enumerate


def read_parquet(file_path: str) -> ManifestEntry:
    """Profile a Parquet file into a ManifestEntry."""
    size = os.path.getsize(file_path)

    try:
        import pyarrow.parquet as pq
        import pandas as pd

        pf = pq.ParquetFile(file_path)
        schema = pf.schema_arrow
        row_count = pf.metadata.num_rows

        columns = []
        for i in range(len(schema)):
            columns.append({
                "name": schema.field(i).name,
                "dtype": str(schema.field(i).type),
            })

        # Read full file for sample rows, column stats, and DISTINCT scanning
        df = pd.read_parquet(file_path)
        sample_rows = df.head(3).to_dict(orient="records")

        # Enrich with column stats (consistent with CSV/JSON/SQLite/Excel readers)
        for col_info in columns:
            col_name = col_info["name"]
            if col_name not in df.columns:
                continue
            series = df[col_name].dropna()
            if len(series) > 0:
                col_info.update(compute_column_stats(series, col_name=col_name))
                col_info["value_repr"] = compressed_value_repr(series)
                col_info["sample"] = series.head(3).tolist()

        # DISTINCT value scan
        _scan_distinct_values(df, columns)

        return ManifestEntry(
            file_path=file_path, file_type="parquet", size_bytes=size,
            summary={
                "columns": columns,
                "row_count": row_count,
                "sample_rows": sample_rows,
            },
        )
    except Exception as e:
        return ManifestEntry(
            file_path=file_path, file_type="parquet", size_bytes=size,
            summary={"error": str(e)},
        )


def _scan_distinct_values(df: "pd.DataFrame", columns: list[dict]) -> None:
    """Compute exact cardinality and DISTINCT values from full DataFrame.

    For text columns with ≤ _DISTINCT_LIMIT unique values, stores all distinct
    values. Mutates column dicts in place.
    """
    import pandas as pd

    col_lookup = {c["name"]: c for c in columns}
    for col_name in df.columns:
        if col_name not in col_lookup:
            continue
        col_info = col_lookup[col_name]
        n_unique = int(df[col_name].nunique())
        col_info["cardinality"] = n_unique
        col_info["null_pct"] = round(float(df[col_name].isna().mean()), 3)

        # Enumerate distinct values for low-cardinality text/object columns
        if str(df[col_name].dtype) == "object" and n_unique <= _DISTINCT_LIMIT:
            vals = df[col_name].dropna().unique().tolist()
            if all(len(str(v)) <= 100 for v in vals):
                col_info["distinct_values"] = sorted(str(v) for v in vals)
