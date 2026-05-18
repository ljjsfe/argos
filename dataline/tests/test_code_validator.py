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
        _, warnings, _blocking = validate_column_references(code, manifest)
        assert warnings == []

    def test_case_mismatch_warning(self) -> None:
        manifest = _make_manifest("Status")
        code = 'df["status"]'
        _, warnings, _blocking = validate_column_references(code, manifest)
        assert len(warnings) == 1
        assert "case mismatch" in warnings[0]

    def test_missing_column_warning(self) -> None:
        manifest = _make_manifest("user_id")
        code = 'df["nonexistent_col"]'
        _, warnings, _blocking = validate_column_references(code, manifest)
        assert len(warnings) == 1
        assert "not found" in warnings[0]

    def test_close_match_suggestion(self) -> None:
        manifest = _make_manifest("user_id", "user_name")
        code = 'df["user_email"]'
        _, warnings, _blocking = validate_column_references(code, manifest)
        assert len(warnings) == 1
        assert "close matches" in warnings[0]

    def test_warning_comments_injected(self) -> None:
        manifest = _make_manifest("col1")
        code = 'df["wrong_col"]'
        annotated, warnings, _blocking = validate_column_references(code, manifest)
        assert warnings
        assert "CODE VALIDATOR WARNINGS" in annotated
        assert code in annotated

    def test_no_code_modification_when_valid(self) -> None:
        manifest = _make_manifest("col1")
        code = 'df["col1"]'
        annotated, warnings, _blocking = validate_column_references(code, manifest)
        assert annotated == code
        assert warnings == []

    def test_sqlite_columns(self) -> None:
        entry = ManifestEntry(
            file_path="db.sqlite", file_type="sqlite", size_bytes=100,
            summary={"tables": [{"name": "users", "columns": [{"name": "id"}, {"name": "name"}]}]},
        )
        manifest = Manifest(entries=(entry,))
        code = 'df["id"]'
        _, warnings, _blocking = validate_column_references(code, manifest)
        assert warnings == []


# ---------------------------------------------------------------------------
# Phase 0.7 B-fix: blocking_warnings (close-match = high-confidence typo)
# ---------------------------------------------------------------------------

class TestBlockingClosematch:
    def _make_manifest(self, *col_names: str) -> Manifest:
        cols = [{"name": n} for n in col_names]
        entry = ManifestEntry(
            file_path="t.csv", file_type="csv", size_bytes=100,
            summary={"columns": cols},
        )
        return Manifest(entries=(entry,))

    def test_case_mismatch_is_blocking(self):
        manifest = self._make_manifest("CDSCode")
        code = 'df["CDSCode_str"]'
        _, warnings, blocking = validate_column_references(code, manifest)
        # Substring match — close to CDSCode
        assert any("close matches" in w.lower() or "case mismatch" in w.lower() for w in blocking)

    def test_exact_case_mismatch_is_blocking(self):
        manifest = self._make_manifest("CDSCode")
        code = 'df["cdscode"]'
        _, warnings, blocking = validate_column_references(code, manifest)
        assert blocking, "case mismatch must be a blocking warning"
        assert any("case mismatch" in w.lower() for w in blocking)

    def test_close_match_is_blocking(self):
        manifest = self._make_manifest("CDSCode", "FundingType")
        code = 'df["CDSCode_str"]'  # close to CDSCode
        _, warnings, blocking = validate_column_references(code, manifest)
        assert blocking, "close-match must block"
        assert any("CDSCode" in w for w in blocking)

    def test_no_match_is_soft_warning_not_blocking(self):
        """Column not in manifest AND no close match = likely intermediate
        variable. Allow execution, only warn."""
        manifest = self._make_manifest("apple", "banana")
        code = 'df["totally_unrelated_xyz"]'
        _, warnings, blocking = validate_column_references(code, manifest)
        assert warnings, "should warn"
        assert blocking == [], "no close-match → not blocking"

    def test_exact_match_no_warning(self):
        manifest = self._make_manifest("id", "name")
        code = 'df["id"]\ndf.groupby("name")'
        _, warnings, blocking = validate_column_references(code, manifest)
        assert warnings == []
        assert blocking == []

    def test_mixed_some_blocking_some_soft(self):
        manifest = self._make_manifest("CDSCode")
        code = 'df["CDSCode_str"]; df["my_intermediate_var"]'
        _, warnings, blocking = validate_column_references(code, manifest)
        # First col has close match → blocking
        # Second col has no close match → only soft warning
        assert blocking, "CDSCode_str should block"
        assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# Phase 0.7 B-fix 2: raw json.load → suggest safe_read_json_df helper
# ---------------------------------------------------------------------------

class TestRawJsonLoadDetection:
    def _empty_manifest(self) -> Manifest:
        return Manifest(entries=())

    def test_raw_json_load_suggests_helper(self):
        code = """
import json
with open('context/json/foo.json') as f:
    data = json.load(f)
"""
        _, warnings, blocking = validate_column_references(code, self._empty_manifest())
        assert any("safe_read_json_df" in w for w in warnings)
        # Soft warning, not blocking
        assert blocking == []

    def test_json_loads_string_also_flagged(self):
        code = """
import json
data = json.loads(open('context/json/foo.json').read())
"""
        _, warnings, _ = validate_column_references(code, self._empty_manifest())
        assert any("safe_read_json_df" in w for w in warnings)

    def test_helper_already_used_no_warning(self):
        code = """
from data_helpers import safe_read_json_df
df = safe_read_json_df('context/json/foo.json')
"""
        _, warnings, _ = validate_column_references(code, self._empty_manifest())
        # No safe_read_json_df recommendation when already used
        assert not any("Raw json.load detected" in w for w in warnings)

    def test_pure_sql_code_no_warning(self):
        code = "SELECT a FROM t"
        _, warnings, _ = validate_column_references(code, self._empty_manifest())
        assert warnings == []

    def test_csv_only_no_json_warning(self):
        code = """
import pandas as pd
df = pd.read_csv('data.csv')
"""
        _, warnings, _ = validate_column_references(code, self._empty_manifest())
        assert not any("json" in w.lower() for w in warnings)
