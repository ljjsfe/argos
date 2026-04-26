"""Auto-discover entity relations across data sources.

Enhanced with value-based join validation (not just column name overlap).
"""

from __future__ import annotations

from ..core.types import CrossSourceRelation, ManifestEntry
from .join_validator import validate_join_keys


def discover_relations(entries: list[ManifestEntry]) -> list[CrossSourceRelation]:
    """Find column name overlaps and value overlaps across structured sources."""
    structured = [e for e in entries if e.file_type in ("csv", "json", "sqlite", "excel", "parquet")]
    text_entries = [e for e in entries if e.file_type in ("markdown", "pdf", "docx")]

    relations: list[CrossSourceRelation] = []

    # 1. Column name overlap + value-based validation
    for i, a in enumerate(structured):
        cols_a = _get_column_names(a)
        if not cols_a:
            continue
        for b in structured[i + 1:]:
            cols_b = _get_column_names(b)
            if not cols_b:
                continue
            overlap = cols_a & cols_b
            if overlap:
                # Validate with actual value overlap
                hints = validate_join_keys(a, b, overlap)
                for hint in hints:
                    relations.append(CrossSourceRelation(
                        source_a=a.file_path,
                        source_b=b.file_path,
                        relation=(
                            f"Shared column '{hint.column_name}'"
                            f" (value overlap: {hint.value_overlap_pct:.0%})"
                        ),
                        confidence=hint.confidence,
                    ))

    # 1b. Type compatibility warnings for shared columns
    for i, a in enumerate(structured):
        cols_a = _get_column_types(a)
        for b in structured[i + 1:]:
            cols_b = _get_column_types(b)
            shared = set(cols_a.keys()) & set(cols_b.keys())
            for col in shared:
                warning = _check_type_compatibility(cols_a[col], cols_b[col], col)
                if warning:
                    relations.append(CrossSourceRelation(
                        source_a=a.file_path,
                        source_b=b.file_path,
                        relation=warning,
                        confidence=0.5,
                    ))

    # 1c. Temporal alignment across sources
    relations.extend(_check_temporal_alignment(structured))

    # 2. Structured source values mentioned in text documents
    for struct_entry in structured:
        sample_values = _get_sample_values(struct_entry)
        if not sample_values:
            continue
        for text_entry in text_entries:
            text_content = _read_text_content(text_entry)
            if not text_content:
                continue
            matches = [v for v in sample_values if str(v) in text_content]
            if matches:
                relations.append(CrossSourceRelation(
                    source_a=struct_entry.file_path,
                    source_b=text_entry.file_path,
                    relation=f"Document mentions values from structured data: {matches[:5]}",
                    confidence=round(min(len(matches) / max(len(sample_values), 1), 1.0), 2),
                ))

    return relations


def _read_text_content(entry: ManifestEntry) -> str:
    """Read text content from a document entry.

    Markdown: reads file directly from disk (no text_preview in summary).
    PDF/DOCX: uses text_preview from summary (extracted text from their readers).
    Returns empty string if content is unavailable.
    """
    if entry.file_type == "markdown":
        try:
            with open(entry.file_path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return ""

    return entry.summary.get("text_preview", "")


def _get_column_names(entry: ManifestEntry) -> set[str]:
    """Extract column names from a structured entry."""
    names: set[str] = set()
    s = entry.summary

    if "columns" in s:
        for col in s["columns"]:
            name = col.get("name", "")
            if name:
                names.add(name.lower())

    if "tables" in s:
        for table in s["tables"]:
            for col in table.get("columns", []):
                name = col.get("name", "")
                if name:
                    names.add(name.lower())

    if "sheets" in s:
        for sheet in s["sheets"]:
            for col in sheet.get("columns", []):
                name = col.get("name", "")
                if name:
                    names.add(name.lower())

    return names


def _get_sample_values(entry: ManifestEntry) -> list[str]:
    """Extract sample string values from a structured entry."""
    values: list[str] = []
    s = entry.summary

    if "columns" in s:
        for col in s["columns"]:
            for v in col.get("sample", []):
                if isinstance(v, str) and len(v) > 2:
                    values.append(v)

    if "tables" in s:
        for table in s["tables"]:
            for col in table.get("columns", []):
                for v in col.get("sample", []):
                    if isinstance(v, str) and len(v) > 2:
                        values.append(v)

    if "sheets" in s:
        for sheet in s["sheets"]:
            for col in sheet.get("columns", []):
                for v in col.get("sample", []):
                    if isinstance(v, str) and len(v) > 2:
                        values.append(v)

    return values[:50]


def _get_column_types(entry: ManifestEntry) -> dict[str, str]:
    """Extract {column_name_lower: dtype} from an entry."""
    types: dict[str, str] = {}
    s = entry.summary

    def _add_cols(columns: list) -> None:
        for col in columns:
            name = col.get("name", "")
            dtype = col.get("dtype", "")
            if name:
                types[name.lower()] = dtype.lower()

    if "columns" in s:
        _add_cols(s["columns"])
    for table in s.get("tables", []):
        _add_cols(table.get("columns", []))
    for sheet in s.get("sheets", []):
        _add_cols(sheet.get("columns", []))

    return types


def _check_type_compatibility(dtype_a: str, dtype_b: str, col_name: str) -> str | None:
    """Return a warning string if two dtypes for the same column are incompatible."""
    # Normalize common types
    numeric_types = {"int64", "float64", "int32", "float32", "integer", "real", "numeric", "int", "float"}
    string_types = {"object", "str", "string", "text", "varchar"}

    a_is_numeric = dtype_a in numeric_types
    b_is_numeric = dtype_b in numeric_types
    a_is_string = dtype_a in string_types
    b_is_string = dtype_b in string_types

    if (a_is_numeric and b_is_string) or (a_is_string and b_is_numeric):
        return (
            f"Type mismatch on shared column '{col_name}': "
            f"{dtype_a} vs {dtype_b} — may need casting before join"
        )
    return None


def _check_temporal_alignment(entries: list[ManifestEntry]) -> list[CrossSourceRelation]:
    """Check if date columns across sources have overlapping ranges."""
    # Collect entries that have date-like columns
    date_sources: list[tuple[str, str, list]] = []  # (file_path, col_name, sample_values)

    for entry in entries:
        s = entry.summary
        all_columns = list(s.get("columns", []))
        for table in s.get("tables", []):
            all_columns.extend(table.get("columns", []))
        for sheet in s.get("sheets", []):
            all_columns.extend(sheet.get("columns", []))

        for col in all_columns:
            flags = col.get("flags", [])
            if "date_like" in flags:
                samples = col.get("sample", [])
                date_sources.append((entry.file_path, col.get("name", ""), samples))

    # Compare date ranges across sources
    relations: list[CrossSourceRelation] = []
    for i, (path_a, col_a, samples_a) in enumerate(date_sources):
        for path_b, col_b, samples_b in date_sources[i + 1:]:
            if path_a == path_b:
                continue
            if samples_a and samples_b:
                relations.append(CrossSourceRelation(
                    source_a=path_a,
                    source_b=path_b,
                    relation=(
                        f"Both have date columns ({col_a}, {col_b}) — "
                        f"check temporal alignment for joins"
                    ),
                    confidence=0.4,
                ))

    return relations
