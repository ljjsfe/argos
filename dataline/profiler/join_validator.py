"""Value-based join key validation.

Goes beyond column name overlap: checks if shared column names
have actual value overlap between two data sources.
Uses sample values already in manifest — no file re-read.

Also provides cross-name FK discovery: detect foreign-key relationships
between differently-named columns via value overlap (e.g. sets.themeId → themes.id).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.types import ManifestEntry


@dataclass(frozen=True)
class JoinHint:
    """Validated join key between two sources."""
    column_name: str
    source_a: str
    source_b: str
    value_overlap_pct: float  # 0.0 to 1.0
    confidence: float         # combined confidence score
    cardinality: str = ""     # "1:1" | "1:N" | "N:1" | "N:N" | "" (unknown)


@dataclass(frozen=True)
class ForeignKeyHint:
    """Discovered FK relationship between differently-named columns."""
    left_table: str
    left_column: str
    right_table: str
    right_column: str
    overlap: float       # 0.0 to 1.0
    confidence: float
    left_unique: int
    right_unique: int
    relationship: str    # "one_to_one" | "many_to_one" | "many_to_many"


def _is_sequential_integers(values_a: list, values_b: list) -> bool:
    """Return True if both value lists look like row-counter IDs (1, 2, 3, …).

    Criteria: all values are integers AND max_value ≤ 1.5 × count, indicating
    a dense integer sequence rather than a semantic identifier space.
    """
    def _dense_int_sequence(vals: list) -> bool:
        try:
            ints = [int(v) for v in vals if v is not None]
            if len(ints) < 2:
                return False
            return min(ints) >= 0 and max(ints) <= len(ints) * 1.5
        except (ValueError, TypeError):
            return False

    return _dense_int_sequence(values_a) and _dense_int_sequence(values_b)


def validate_join_keys(
    entry_a: ManifestEntry,
    entry_b: ManifestEntry,
    shared_cols: set[str],
) -> list[JoinHint]:
    """For each shared column, check actual value overlap percentage.

    Uses sample values from manifest summaries (no file I/O).
    """
    hints: list[JoinHint] = []

    for col_name in sorted(shared_cols):
        values_a = _extract_values_for_column(entry_a, col_name)
        values_b = _extract_values_for_column(entry_b, col_name)

        if not values_a or not values_b:
            # No sample values to compare — fall back to name-only hint
            hints.append(JoinHint(
                column_name=col_name,
                source_a=entry_a.file_path,
                source_b=entry_b.file_path,
                value_overlap_pct=0.0,
                confidence=0.3,  # low confidence: name match only
            ))
            continue

        # Skip generic row-counter columns (id, index, row_id) whose values
        # are sequential integers in both tables — these produce 100% overlap
        # but carry no semantic FK meaning (both just number their rows 1,2,3…).
        if col_name.lower() in ("id", "index", "row_id", "rowid") and _is_sequential_integers(values_a, values_b):
            continue

        set_a = {str(v).lower().strip() for v in values_a}
        set_b = {str(v).lower().strip() for v in values_b}
        overlap = set_a & set_b

        smaller = min(len(set_a), len(set_b))
        overlap_pct = len(overlap) / smaller if smaller > 0 else 0.0

        # Confidence: name match (0.3) + value overlap bonus (up to 0.7)
        confidence = 0.3 + 0.7 * overlap_pct

        hints.append(JoinHint(
            column_name=col_name,
            source_a=entry_a.file_path,
            source_b=entry_b.file_path,
            value_overlap_pct=round(overlap_pct, 3),
            confidence=round(min(confidence, 1.0), 2),
            cardinality=_infer_cardinality(entry_a, entry_b, col_name),
        ))

    return hints


def _infer_cardinality(entry_a: ManifestEntry, entry_b: ManifestEntry, col_name: str) -> str:
    """Infer join cardinality from per-side uniqueness_ratio.

    Returns one of '1:1', '1:N', 'N:1', 'N:N', or '' when uniqueness is unknown.
    Generic deterministic check — uses full-data uniqueness already computed
    by column_stats. UNIQUE_THRESHOLD=0.95 to tolerate near-PKs with rare dupes.
    """
    UNIQUE_THRESHOLD = 0.95

    def _uniq_ratio(entry: ManifestEntry) -> float | None:
        col_lower = col_name.lower()
        for col in entry.summary.get("columns", []):
            if col.get("name", "").lower() == col_lower:
                return col.get("uniqueness_ratio")
        for table in entry.summary.get("tables", []):
            for col in table.get("columns", []):
                if col.get("name", "").lower() == col_lower:
                    return col.get("uniqueness_ratio")
        return None

    a_ratio, b_ratio = _uniq_ratio(entry_a), _uniq_ratio(entry_b)
    if a_ratio is None or b_ratio is None:
        return ""
    a_unique = a_ratio >= UNIQUE_THRESHOLD
    b_unique = b_ratio >= UNIQUE_THRESHOLD
    if a_unique and b_unique:
        return "1:1"
    if a_unique and not b_unique:
        return "1:N"
    if not a_unique and b_unique:
        return "N:1"
    return "N:N"


def _extract_values_for_column(entry: ManifestEntry, col_name: str) -> list:
    """Extract sample values for a specific column from manifest summary."""
    s = entry.summary
    col_lower = col_name.lower()

    # Flat columns (CSV, JSON, Parquet)
    for col in s.get("columns", []):
        if col.get("name", "").lower() == col_lower:
            return col.get("sample", []) + _extract_top_values(col)

    # SQLite tables
    for table in s.get("tables", []):
        for col in table.get("columns", []):
            if col.get("name", "").lower() == col_lower:
                return col.get("sample", []) + _extract_top_values(col)

    # Excel sheets
    for sheet in s.get("sheets", []):
        for col in sheet.get("columns", []):
            if col.get("name", "").lower() == col_lower:
                return col.get("sample", []) + _extract_top_values(col)

    return []


def _extract_top_values(col_info: dict) -> list:
    """Extract values from top_values if enriched stats are present."""
    top = col_info.get("top_values", [])
    return [entry["value"] for entry in top if "value" in entry]


# ---------------------------------------------------------------------------
# Cross-name FK discovery
# ---------------------------------------------------------------------------

# Patterns for columns that might be foreign keys pointing to another table.
# Split into two patterns: case-insensitive for snake_case/bare names,
# case-SENSITIVE for camelCase (setCode, languageId) — combining them with (?i)
# would make the camelCase pattern match all-lowercase too.
_FK_LIKE_CI_RE = re.compile(
    r"(?i)(?:_id$|_key$|_code$|^id$|^key$|^code$|_ref$|_link$|_fk$)"
)
_FK_LIKE_CAMEL_RE = re.compile(
    r"[a-z](?:Id|Code|Key|Ref)$"  # case-sensitive: lowercase letter + TitleCase suffix
)


def _is_fk_like(col_name: str) -> bool:
    return bool(_FK_LIKE_CI_RE.search(col_name) or _FK_LIKE_CAMEL_RE.search(col_name))

# Patterns for columns that might be primary keys (targets of FK references)
_PK_LIKE_RE = re.compile(
    r"(?i)(?:^id$|^key$|^code$|_id$|_key$|_code$)",
)


def _get_all_columns(entry: ManifestEntry) -> list[tuple[str, str, dict]]:
    """Extract all (table_name, column_name, column_info) from entry.

    For flat files (CSV/JSON/Parquet): table_name is the filename stem.
    For SQLite: table_name is the actual table name.
    """
    s = entry.summary
    results: list[tuple[str, str, dict]] = []

    # Derive table name from file path
    import os
    default_table = os.path.splitext(os.path.basename(entry.file_path))[0]

    # Flat columns (CSV, JSON, Parquet)
    if "columns" in s:
        for col in s["columns"]:
            name = col.get("name", "")
            if name:
                results.append((default_table, name, col))

    # SQLite tables
    for table in s.get("tables", []):
        table_name = table.get("name", default_table)
        for col in table.get("columns", []):
            name = col.get("name", "")
            if name:
                results.append((table_name, name, col))

    # Excel sheets
    for sheet in s.get("sheets", []):
        sheet_name = sheet.get("name", default_table)
        for col in sheet.get("columns", []):
            name = col.get("name", "")
            if name:
                results.append((sheet_name, name, col))

    return results


def discover_cross_name_fks(
    entries: list[ManifestEntry],
) -> list[ForeignKeyHint]:
    """Discover FK relationships between differently-named columns.

    Strategy: for each pair of structured sources, compare ID-like columns
    in table A against PK-like columns in table B using value overlap.
    Only report pairs with overlap > 0.5 to avoid noise.

    Zero LLM cost — purely deterministic using sample values in manifest.
    """
    structured = [
        e for e in entries
        if e.file_type in ("csv", "json", "sqlite", "excel", "parquet")
    ]

    # Collect all columns with their values per source
    source_columns: list[tuple[ManifestEntry, str, str, list]] = []
    # (entry, table_name, col_name, values)
    for entry in structured:
        for table_name, col_name, col_info in _get_all_columns(entry):
            values = col_info.get("sample", []) + _extract_top_values(col_info)
            if values:
                source_columns.append((entry, table_name, col_name, values))

    # Find FK candidates: column in source A whose values overlap with
    # a PK-like column in source B (different source)
    hints: list[ForeignKeyHint] = []
    seen: set[tuple[str, str, str, str]] = set()  # avoid duplicates

    for i, (entry_a, table_a, col_a, vals_a) in enumerate(source_columns):
        if not _is_fk_like(col_a):
            continue
        set_a = {str(v).lower().strip() for v in vals_a if v is not None}
        if len(set_a) < 2:  # need at least 2 distinct values to compare
            continue

        for entry_b, table_b, col_b, vals_b in source_columns:
            # Skip same source
            if entry_a.file_path == entry_b.file_path and table_a == table_b:
                continue
            # Skip if same column name (handled by existing same-name logic)
            if col_a.lower() == col_b.lower():
                continue
            if not _PK_LIKE_RE.search(col_b):
                continue

            # Dedup: only keep one direction
            pair_key = tuple(sorted([
                (table_a, col_a), (table_b, col_b),
            ]))
            if pair_key in seen:
                continue

            set_b = {str(v).lower().strip() for v in vals_b if v is not None}
            if len(set_b) < 2:
                continue

            overlap = set_a & set_b
            smaller = min(len(set_a), len(set_b))
            overlap_pct = len(overlap) / smaller if smaller > 0 else 0.0

            if overlap_pct < 0.5:
                continue

            seen.add(pair_key)

            # Determine relationship type
            a_unique_ratio = len(set_a) / max(len(vals_a), 1)
            b_unique_ratio = len(set_b) / max(len(vals_b), 1)
            if a_unique_ratio > 0.9 and b_unique_ratio > 0.9:
                relationship = "one_to_one"
            elif b_unique_ratio > 0.9:
                relationship = "many_to_one"
            else:
                relationship = "many_to_many"

            confidence = 0.4 + 0.6 * overlap_pct  # base 0.4 for cross-name

            hints.append(ForeignKeyHint(
                left_table=table_a,
                left_column=col_a,
                right_table=table_b,
                right_column=col_b,
                overlap=round(overlap_pct, 3),
                confidence=round(min(confidence, 1.0), 2),
                left_unique=len(set_a),
                right_unique=len(set_b),
                relationship=relationship,
            ))

    return hints
