"""SQLite database profiling with enriched column statistics."""

from __future__ import annotations

import logging
import os
import sqlite3

import pandas as pd

from ..core.types import ManifestEntry
from .column_stats import compute_column_stats, compressed_value_repr

logger = logging.getLogger(__name__)


def read_sqlite(file_path: str) -> ManifestEntry:
    """Profile a SQLite database into a ManifestEntry."""
    size = os.path.getsize(file_path)
    conn = sqlite3.connect(file_path)
    try:
        tables = _get_tables(conn)
        foreign_keys = _get_foreign_keys(conn, tables)

        table_summaries = []
        for table_name in tables:
            table_summaries.append(_profile_table(conn, table_name))

        return ManifestEntry(
            file_path=file_path,
            file_type="sqlite",
            size_bytes=size,
            summary={
                "tables": table_summaries,
                "foreign_keys": foreign_keys,
            },
        )
    finally:
        conn.close()


def _get_tables(conn: sqlite3.Connection) -> list[str]:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in cursor.fetchall()]


def _quote_identifier(name: str) -> str:
    """Safely quote a SQLite identifier to prevent SQL injection.

    Uses double-quote escaping: any embedded " becomes "".
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _profile_table(conn: sqlite3.Connection, table_name: str) -> dict:
    quoted_table = _quote_identifier(table_name)

    # Column info via PRAGMA (PRAGMA doesn't support parameterized queries,
    # but table_name comes from sqlite_master, not user input)
    cursor = conn.execute(f"PRAGMA table_info({quoted_table})")
    pragma_rows = cursor.fetchall()

    # Row count
    count_row = conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()
    row_count = count_row[0] if count_row else 0

    # Sample rows
    cursor = conn.execute(f"SELECT * FROM {quoted_table} LIMIT 3")
    sample_rows_raw = cursor.fetchall()
    col_names = [desc[0] for desc in cursor.description] if cursor.description else []

    columns = []
    for pragma_row in pragma_rows:
        col_name = pragma_row[1]
        col_type = pragma_row[2]
        not_null = bool(pragma_row[3])
        pk = bool(pragma_row[5])

        quoted_col = _quote_identifier(col_name)

        # Get sample values
        try:
            vals = conn.execute(
                f"SELECT {quoted_col} FROM {quoted_table} WHERE {quoted_col} IS NOT NULL LIMIT 100"
            ).fetchall()
            sample = [v[0] for v in vals[:3]]
            all_values = [v[0] for v in vals]
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("Failed to read column %s.%s: %s", table_name, col_name, exc)
            sample = []
            all_values = []

        col_info: dict = {
            "name": col_name,
            "dtype": col_type or "TEXT",
            "not_null": not_null,
            "primary_key": pk,
            "sample": sample,
        }

        # Enrich with column stats
        if all_values:
            series = pd.Series(all_values)
            col_info.update(compute_column_stats(series, col_name=col_name))
            col_info["value_repr"] = compressed_value_repr(series)

        columns.append(col_info)

    # Full-table DISTINCT value scan for low-cardinality text columns
    _scan_distinct_values(conn, quoted_table, row_count, columns)

    sample_rows = [dict(zip(col_names, row)) for row in sample_rows_raw]

    return {
        "name": table_name,
        "row_count": row_count,
        "columns": columns,
        "sample_rows": sample_rows,
    }


_DISTINCT_LIMIT = 30  # Max unique values to enumerate


def _scan_distinct_values(
    conn: sqlite3.Connection,
    quoted_table: str,
    row_count: int,
    columns: list[dict],
) -> None:
    """Compute exact cardinality and DISTINCT values for all columns.

    For text columns with ≤ _DISTINCT_LIMIT unique values, stores all distinct
    values. For all columns, stores exact cardinality and null percentage.
    Mutates column dicts in place.
    """
    if row_count == 0:
        return

    for col_info in columns:
        quoted_col = _quote_identifier(col_info["name"])
        try:
            # Exact cardinality
            result = conn.execute(
                f"SELECT COUNT(DISTINCT {quoted_col}) FROM {quoted_table}"
            ).fetchone()
            n_unique = result[0] if result else 0
            col_info["cardinality"] = n_unique

            # Null percentage from full table
            null_result = conn.execute(
                f"SELECT COUNT(*) FROM {quoted_table} WHERE {quoted_col} IS NULL"
            ).fetchone()
            null_count = null_result[0] if null_result else 0
            col_info["null_pct"] = round(null_count / max(row_count, 1), 3)

            # Enumerate distinct values for low-cardinality text columns
            dtype_lower = col_info.get("dtype", "").upper()
            is_text = dtype_lower in ("TEXT", "VARCHAR", "CHAR", "") or "TEXT" in dtype_lower
            if is_text and n_unique <= _DISTINCT_LIMIT:
                vals_raw = conn.execute(
                    f"SELECT DISTINCT {quoted_col} FROM {quoted_table} "
                    f"WHERE {quoted_col} IS NOT NULL ORDER BY {quoted_col}"
                ).fetchall()
                vals = [row[0] for row in vals_raw]
                # Skip if any value is very long (XML blobs, JSON strings)
                if all(len(str(v)) <= 100 for v in vals):
                    col_info["distinct_values"] = [str(v) for v in vals]
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("Failed to scan distinct values for %s: %s", col_info["name"], exc)
            continue


def _get_foreign_keys(conn: sqlite3.Connection, tables: list[str]) -> list[dict]:
    fks: list[dict] = []
    for table_name in tables:
        try:
            quoted_table = _quote_identifier(table_name)
            cursor = conn.execute(f"PRAGMA foreign_key_list({quoted_table})")
            for row in cursor.fetchall():
                fks.append({
                    "from_table": table_name,
                    "from_col": row[3],
                    "to_table": row[2],
                    "to_col": row[4],
                })
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.warning("Failed to read foreign keys for %s: %s", table_name, exc)
            continue
    return fks
