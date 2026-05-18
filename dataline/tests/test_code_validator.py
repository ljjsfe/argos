"""Tests for pre-execution code validator."""

from dataline.agents.code_validator import (
    extract_column_references,
    validate_column_references,
)
from dataline.core.types import Manifest, ManifestEntry


def _make_manifest(*col_names: str) -> Manifest:
    columns = [{"name": c} for c in col_names]
    entry = ManifestEntry(
        file_path="data.csv", file_type="csv", size_bytes=100,
        summary={"columns": columns, "row_count": 10},
    )
    return Manifest(entries=(entry,))


class TestExtractColumnReferences:
    def test_bracket_access(self) -> None:
        code = 'result = df["user_id"]'
        assert "user_id" in extract_column_references(code)

    def test_single_quote(self) -> None:
        code = "result = df['status']"
        assert "status" in extract_column_references(code)

    def test_groupby(self) -> None:
        code = 'df.groupby("department")'
        assert "department" in extract_column_references(code)

    def test_sort_values(self) -> None:
        code = 'df.sort_values("created_at")'
        assert "created_at" in extract_column_references(code)

    def test_no_duplicates(self) -> None:
        code = 'x = df["col"]; y = df["col"]'
        refs = extract_column_references(code)
        assert refs.count("col") == 1

    def test_no_references(self) -> None:
        code = "x = 1 + 2"
        assert extract_column_references(code) == []


class TestValidateColumnReferences:
    def test_all_columns_valid(self) -> None:
        manifest = _make_manifest("user_id", "status")
        code = 'df["user_id"]; df["status"]'
        _, warnings = validate_column_references(code, manifest)
        assert warnings == []

    def test_case_mismatch_warning(self) -> None:
        manifest = _make_manifest("Status")
        code = 'df["status"]'
        _, warnings = validate_column_references(code, manifest)
        assert len(warnings) == 1
        assert "case mismatch" in warnings[0]

    def test_missing_column_warning(self) -> None:
        manifest = _make_manifest("user_id")
        code = 'df["nonexistent_col"]'
        _, warnings = validate_column_references(code, manifest)
        assert len(warnings) == 1
        assert "not found" in warnings[0]

    def test_close_match_suggestion(self) -> None:
        manifest = _make_manifest("user_id", "user_name")
        code = 'df["user_email"]'
        _, warnings = validate_column_references(code, manifest)
        assert len(warnings) == 1
        assert "close matches" in warnings[0]

    def test_warning_comments_injected(self) -> None:
        manifest = _make_manifest("col1")
        code = 'df["wrong_col"]'
        annotated, warnings = validate_column_references(code, manifest)
        assert warnings
        assert "CODE VALIDATOR WARNINGS" in annotated
        assert code in annotated

    def test_no_code_modification_when_valid(self) -> None:
        manifest = _make_manifest("col1")
        code = 'df["col1"]'
        annotated, warnings = validate_column_references(code, manifest)
        assert annotated == code
        assert warnings == []

    def test_sqlite_columns(self) -> None:
        entry = ManifestEntry(
            file_path="db.sqlite", file_type="sqlite", size_bytes=100,
            summary={"tables": [{"name": "users", "columns": [{"name": "id"}, {"name": "name"}]}]},
        )
        manifest = Manifest(entries=(entry,))
        code = 'df["id"]'
        _, warnings = validate_column_references(code, manifest)
        assert warnings == []
