"""Tests for profiler column_stats and join_validator."""

import pytest
import pandas as pd

from dataline.profiler.column_stats import (
    compute_column_stats,
    compressed_value_repr,
    detect_anomalies,
)
from dataline.profiler.join_validator import validate_join_keys, discover_cross_name_fks, JoinHint, _is_fk_like
from dataline.core.types import ManifestEntry


# --- compute_column_stats ---


class TestComputeColumnStats:
    def test_numeric_column(self):
        series = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        stats = compute_column_stats(series)

        assert stats["completeness"] == 1.0
        assert stats["uniqueness_ratio"] == 1.0
        assert "mean" in stats
        assert stats["mean"] == pytest.approx(5.5, rel=0.01)
        assert "median" in stats
        assert "q25" in stats
        assert "q75" in stats
        assert "constant" not in stats["flags"]

    def test_constant_column(self):
        series = pd.Series([42, 42, 42, 42])
        stats = compute_column_stats(series)

        assert stats["uniqueness_ratio"] == pytest.approx(0.25, rel=0.01)
        assert "constant" in stats["flags"]

    def test_categorical_column(self):
        series = pd.Series(["a", "b", "a", "c", "a", "b"])
        stats = compute_column_stats(series)

        assert stats["completeness"] == 1.0
        assert "top_values" in stats
        assert len(stats["top_values"]) <= 5
        # 'a' should be most frequent
        assert stats["top_values"][0]["value"] == "a"
        assert stats["top_values"][0]["pct"] == pytest.approx(0.5, rel=0.01)

    def test_null_column(self):
        series = pd.Series([None, None, None])
        stats = compute_column_stats(series)

        assert stats["completeness"] == 0.0
        assert stats["uniqueness_ratio"] == 0.0

    def test_id_candidate_detection(self):
        series = pd.Series([f"id_{i}" for i in range(20)])
        stats = compute_column_stats(series)

        assert "id_candidate" in stats["flags"]

    def test_date_like_detection(self):
        series = pd.Series(["2024-01-15", "2024-02-20", "2024-03-10",
                           "2024-04-05", "2024-05-01", "2024-06-15"])
        stats = compute_column_stats(series)

        assert "date_like" in stats["flags"]

    def test_mixed_type_no_false_positive(self):
        """All-string columns should NOT be flagged as mixed_type."""
        series = pd.Series(["hello", "world", "foo"])
        stats = compute_column_stats(series)

        assert "mixed_type" not in stats["flags"]

    def test_with_nulls(self):
        series = pd.Series([1.0, 2.0, None, 4.0, None])
        stats = compute_column_stats(series)

        assert stats["completeness"] == pytest.approx(0.6, rel=0.01)
        assert "mean" in stats


# --- compressed_value_repr ---


class TestCompressedValueRepr:
    def test_numeric_repr(self):
        series = pd.Series([10, 20, 30, 40, 50])
        repr_dict = compressed_value_repr(series)

        assert repr_dict["value_type"] == "numeric"
        assert repr_dict["cardinality"] == 5
        assert repr_dict["range"] == [10, 50]

    def test_low_cardinality_string(self):
        series = pd.Series(["yes", "no", "yes", "yes", "no"])
        repr_dict = compressed_value_repr(series)

        assert repr_dict["value_type"] == "string"
        assert repr_dict["cardinality"] == 2
        assert "all_values" in repr_dict
        assert sorted(repr_dict["all_values"]) == ["no", "yes"]

    def test_high_cardinality_string(self):
        series = pd.Series([f"val_{i}" for i in range(20)])
        repr_dict = compressed_value_repr(series)

        assert repr_dict["value_type"] == "string"
        assert repr_dict["cardinality"] == 20
        assert "sample" in repr_dict
        assert len(repr_dict["sample"]) == 3

    def test_empty_series(self):
        series = pd.Series([], dtype=object)
        repr_dict = compressed_value_repr(series)

        assert repr_dict["value_type"] == "empty"
        assert repr_dict["cardinality"] == 0


# --- detect_anomalies ---


class TestDetectAnomalies:
    def test_date_like_anomaly(self):
        series = pd.Series(["2024-01-01", "2024-02-01", "2024-03-01",
                           "2024-04-01", "2024-05-01", "2024-06-01"])
        anomalies = detect_anomalies(series, "created_at")

        assert any("date-like" in a for a in anomalies)

    def test_no_anomalies_for_clean_numeric(self):
        series = pd.Series([1, 2, 3, 4, 5])
        anomalies = detect_anomalies(series, "count")

        assert anomalies == []

    def test_empty_series_no_crash(self):
        series = pd.Series([], dtype=float)
        anomalies = detect_anomalies(series, "empty_col")

        assert anomalies == []


# --- validate_join_keys ---


class TestValidateJoinKeys:
    def _make_entry(self, file_path: str, columns: list[dict]) -> ManifestEntry:
        return ManifestEntry(
            file_path=file_path,
            file_type="csv",
            size_bytes=100,
            summary={"columns": columns},
        )

    def test_overlap_detected(self):
        # Use non-sequential IDs (1001+) so they aren't filtered as row counters
        entry_a = self._make_entry("a.csv", [
            {"name": "id", "sample": ["1001", "1002", "1003"]},
        ])
        entry_b = self._make_entry("b.csv", [
            {"name": "id", "sample": ["1002", "1003", "1004"]},
        ])

        hints = validate_join_keys(entry_a, entry_b, {"id"})

        assert len(hints) == 1
        assert hints[0].column_name == "id"
        assert hints[0].value_overlap_pct > 0.0
        assert hints[0].confidence > 0.3

    def test_no_overlap(self):
        # Use non-sequential IDs so they aren't filtered as row counters
        entry_a = self._make_entry("a.csv", [
            {"name": "id", "sample": ["1001", "1002", "1003"]},
        ])
        entry_b = self._make_entry("b.csv", [
            {"name": "id", "sample": ["2001", "2002", "2003"]},
        ])

        hints = validate_join_keys(entry_a, entry_b, {"id"})

        assert len(hints) == 1
        assert hints[0].value_overlap_pct == 0.0
        assert hints[0].confidence == 0.3  # name-only confidence

    def test_missing_samples_fallback(self):
        entry_a = self._make_entry("a.csv", [
            {"name": "id", "sample": []},
        ])
        entry_b = self._make_entry("b.csv", [
            {"name": "id", "sample": ["1", "2"]},
        ])

        hints = validate_join_keys(entry_a, entry_b, {"id"})

        assert len(hints) == 1
        assert hints[0].confidence == 0.3  # low confidence: no values to compare

    def test_sequential_id_skipped(self):
        """Row-counter id columns (1,2,3…) produce false 100% overlap — must be skipped."""
        entry_a = self._make_entry("a.csv", [
            {"name": "id", "sample": ["1", "2", "3"]},
        ])
        entry_b = self._make_entry("b.csv", [
            {"name": "id", "sample": ["1", "2", "3"]},
        ])

        hints = validate_join_keys(entry_a, entry_b, {"id"})
        assert len(hints) == 0  # filtered as sequential row-counter


class TestIsFkLike:
    """Unit tests for _is_fk_like — camelCase and snake_case FK column detection."""

    def test_snake_case_matches(self):
        assert _is_fk_like("customer_id")
        assert _is_fk_like("theme_code")
        assert _is_fk_like("set_key")
        assert _is_fk_like("parent_ref")

    def test_bare_names_match(self):
        assert _is_fk_like("id")
        assert _is_fk_like("code")
        assert _is_fk_like("key")

    def test_camel_case_matches(self):
        assert _is_fk_like("setCode")
        assert _is_fk_like("languageId")
        assert _is_fk_like("themeKey")
        assert _is_fk_like("cardRef")

    def test_non_fk_columns_excluded(self):
        assert not _is_fk_like("name")
        assert not _is_fk_like("total")
        assert not _is_fk_like("description")
        assert not _is_fk_like("price")

    def test_camel_case_all_lower_excluded(self):
        # "setcode" (all lowercase) should NOT match camelCase pattern
        assert not _is_fk_like("setcode")


class TestDiscoverCrossNameFKs:
    """Integration test for camelCase FK discovery (task_214 regression)."""

    def _make_entry(self, file_path: str, columns: list[dict]) -> ManifestEntry:
        return ManifestEntry(
            file_path=file_path,
            file_type="csv",
            size_bytes=100,
            summary={"columns": columns},
        )

    def test_camel_case_fk_detected(self):
        """setCode in set_translations should map to code in sets — the task_214 join."""
        entry_a = self._make_entry("set_translations.csv", [
            {"name": "id", "sample": ["1", "2", "3"]},
            {"name": "setCode", "sample": ["LEA", "ARN", "ATQ", "LEG"]},
            {"name": "language", "sample": ["EN", "FR", "DE", "EN"]},
        ])
        entry_b = self._make_entry("sets.csv", [
            {"name": "code", "sample": ["LEA", "ARN", "ATQ", "LEG", "M19"]},
            {"name": "name", "sample": ["Alpha", "Arabian Nights", "Antiquities", "Legends", "Core 2019"]},
        ])

        hints = discover_cross_name_fks([entry_a, entry_b])

        # Must detect setCode → code FK
        fk_cols = {(h.left_column, h.right_column) for h in hints}
        assert ("setCode", "code") in fk_cols, f"Expected setCode→code FK, got: {fk_cols}"

        fk = next(h for h in hints if h.left_column == "setCode" and h.right_column == "code")
        assert fk.overlap >= 0.5
        assert fk.confidence >= 0.7
