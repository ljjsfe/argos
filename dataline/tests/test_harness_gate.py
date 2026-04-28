"""Tests for HarnessGate deterministic verification rules.

Focus on v17 BLOCK promotions and new error_string_answer rule.
Tests verify both positive detection AND false-positive safeguards.
"""

import json

import pytest

from dataline.agents.harness_gate import (
    _check_agg_type,
    _check_dict_string_answer,
    _check_empty_answer,
    _check_empty_output,
    _check_error_string_answer,
    _check_excuse_answer,
    _check_extra_columns,
    _check_nan_values,
    _check_value_embellishment,
    check,
)


def _make_structured(answer: dict) -> str:
    """Helper: wrap answer dict in the structured_json format."""
    return json.dumps({"answer": answer})


# ---------------------------------------------------------------------------
# empty_output: BLOCK severity
# ---------------------------------------------------------------------------

class TestEmptyOutput:
    def test_blocks_on_empty_dataframe(self):
        flags = _check_empty_output("What is the average?", "Empty DataFrame\nColumns: []")
        assert len(flags) == 1
        assert flags[0].severity == "block"
        assert flags[0].rule == "empty_output"

    def test_blocks_on_zero_rows(self):
        flags = _check_empty_output("List all countries", "0 rows × 3 columns")
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_existence_questions(self):
        """Existence/count questions where 0 is a valid answer."""
        flags = _check_empty_output("How many users have admin access?", "0 rows")
        assert len(flags) == 0

    def test_skips_normal_output(self):
        flags = _check_empty_output("What is the average?", "Result: 42.5\nResult rows: 1")
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# empty_answer: BLOCK severity
# ---------------------------------------------------------------------------

class TestEmptyAnswer:
    def test_blocks_on_null_placeholder(self):
        sj = _make_structured({"result": ["null"]})
        flags = _check_empty_answer("Calculate the total revenue", sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_na(self):
        sj = _make_structured({"answer": ["N/A"]})
        flags = _check_empty_answer("Calculate the total revenue", sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_retrieval_questions(self):
        """Retrieval questions where N/A might be a legit answer."""
        sj = _make_structured({"answer": ["N/A"]})
        flags = _check_empty_answer("What is the name of the manager?", sj)
        assert len(flags) == 0

    def test_skips_real_values(self):
        sj = _make_structured({"count": [42]})
        flags = _check_empty_answer("How many?", sj)
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# dict_string_answer: BLOCK severity
# ---------------------------------------------------------------------------

class TestDictStringAnswer:
    def test_blocks_on_stringified_dict(self):
        sj = _make_structured({"result": ["{'pct': 31.2, 'n': 750}"]})
        flags = _check_dict_string_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_normal_string(self):
        sj = _make_structured({"name": ["Alice"]})
        flags = _check_dict_string_answer(sj)
        assert len(flags) == 0

    def test_skips_numeric(self):
        sj = _make_structured({"value": [42.5]})
        flags = _check_dict_string_answer(sj)
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# value_embellishment: BLOCK severity
# ---------------------------------------------------------------------------

class TestValueEmbellishment:
    def test_blocks_on_dollar_sign(self):
        sj = _make_structured({"cost": ["$100.50"]})
        flags = _check_value_embellishment(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_percent(self):
        sj = _make_structured({"rate": ["45.2%"]})
        flags = _check_value_embellishment(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_unit_suffix(self):
        sj = _make_structured({"duration": ["5 days"]})
        flags = _check_value_embellishment(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_raw_numbers(self):
        sj = _make_structured({"value": [100.50]})
        flags = _check_value_embellishment(sj)
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# error_string_answer: BLOCK severity (new rule)
# ---------------------------------------------------------------------------

class TestErrorStringAnswer:
    def test_blocks_on_error_message(self):
        sj = _make_structured({
            "answer": ["Error during data processing: file not found at data.csv"]
        })
        flags = _check_error_string_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"
        assert flags[0].rule == "error_string_answer"

    def test_blocks_on_no_code_generated(self):
        sj = _make_structured({"answer": ["No code generated"]})
        flags = _check_error_string_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_could_not(self):
        sj = _make_structured({
            "result": ["Could not calculate percentage due to missing data"]
        })
        flags = _check_error_string_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_traceback(self):
        sj = _make_structured({
            "result": ["Traceback (most recent call last): File ..."]
        })
        flags = _check_error_string_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_short_values(self):
        """Values under 10 chars should not trigger even if they match."""
        sj = _make_structured({"answer": ["Error"]})
        flags = _check_error_string_answer(sj)
        assert len(flags) == 0

    def test_skips_normal_string(self):
        sj = _make_structured({"city": ["Errorville Heights"]})
        flags = _check_error_string_answer(sj)
        assert len(flags) == 0

    def test_skips_numeric(self):
        sj = _make_structured({"value": [42.5]})
        flags = _check_error_string_answer(sj)
        assert len(flags) == 0

    def test_skips_empty_structured_json(self):
        flags = _check_error_string_answer("")
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# excuse_answer: BLOCK severity (new rule)
# ---------------------------------------------------------------------------

class TestExcuseAnswer:
    def test_blocks_on_data_unavailable(self):
        sj = _make_structured({"answer": ["Budget data unavailable"]})
        flags = _check_excuse_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"
        assert flags[0].rule == "excuse_answer"

    def test_blocks_on_cannot_complete(self):
        sj = _make_structured({
            "result": ["Cannot complete analysis without budget data"]
        })
        flags = _check_excuse_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_file_not_found(self):
        sj = _make_structured({
            "answer": ["The required data file not found in the directory"]
        })
        flags = _check_excuse_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_no_data_found(self):
        sj = _make_structured({"answer": ["No matching records found"]})
        flags = _check_excuse_answer(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_short_values(self):
        sj = _make_structured({"answer": ["No data"]})
        flags = _check_excuse_answer(sj)
        assert len(flags) == 0

    def test_skips_normal_string(self):
        sj = _make_structured({"city": ["San Francisco"]})
        flags = _check_excuse_answer(sj)
        assert len(flags) == 0

    def test_skips_numeric(self):
        sj = _make_structured({"value": [42.5]})
        flags = _check_excuse_answer(sj)
        assert len(flags) == 0

    def test_skips_empty_structured_json(self):
        flags = _check_excuse_answer("")
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# nan_answer: BLOCK severity (unchanged, verify still works)
# ---------------------------------------------------------------------------

class TestNanAnswer:
    def test_blocks_on_all_null(self):
        """All columns all-null → BLOCK."""
        sj = _make_structured({"value": ["nan"], "name": ["null"]})
        flags = _check_nan_values(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_blocks_on_key_column_nan(self):
        """Key-like column (name ending _id) with NaN → BLOCK."""
        sj = _make_structured({"customer_id": ["nan", "nan"], "amount": [100, 200]})
        flags = _check_nan_values(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"
        assert "customer_id" in flags[0].message

    def test_blocks_on_name_column_nan(self):
        """Column named 'name' with NaN → BLOCK (key-like)."""
        sj = _make_structured({"name": [None, "Alice"], "score": [42, 99]})
        flags = _check_nan_values(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_warns_on_nonkey_partial_nan(self):
        """Non-key column with partial NaN → WARN (may be legitimate missing data)."""
        sj = _make_structured({
            "school_name": ["Lincoln", "Washington"],
            "charter_funding_type": ["Grant", "null"],
        })
        flags = _check_nan_values(sj)
        assert len(flags) == 1
        assert flags[0].severity == "warn"
        assert "charter_funding_type" in flags[0].message

    def test_blocks_on_single_column_all_null(self):
        """Single column entirely null → BLOCK."""
        sj = _make_structured({"result": ["nan"]})
        flags = _check_nan_values(sj)
        assert len(flags) == 1
        assert flags[0].severity == "block"

    def test_skips_valid_values(self):
        sj = _make_structured({"value": [42]})
        flags = _check_nan_values(sj)
        assert len(flags) == 0

    def test_skips_empty_structured_json(self):
        flags = _check_nan_values("")
        assert len(flags) == 0


class TestExtraColumns:
    def test_count_is_valid_answer_column_name(self):
        sj = _make_structured({"count": [4]})
        flags = _check_extra_columns(sj)
        assert flags == []

    def test_answer_value_result_are_valid_answer_column_names(self):
        sj = _make_structured({"answer": [4], "value": [5], "result": [6]})
        flags = _check_extra_columns(sj)
        assert flags == []


# ---------------------------------------------------------------------------
# Integration: check() returns correct severity mix
# ---------------------------------------------------------------------------

class TestCheckIntegration:
    def test_block_on_empty_output(self):
        flags = check(
            question="What is the average salary?",
            code="SELECT AVG(salary) FROM employees",
            stdout="Empty DataFrame",
            return_code=0,
            data_profile="employees [100 rows]",
            question_spec=None,
            structured_json=_make_structured({"answer": []}),
        )
        block_rules = {f.rule for f in flags if f.severity == "block"}
        assert "empty_output" in block_rules

    def test_no_flags_on_clean_result(self):
        flags = check(
            question="What is the average salary?",
            code="SELECT AVG(salary) FROM employees",
            stdout="avg_salary\n50000.0\n\nResult rows: 1",
            return_code=0,
            data_profile="employees [100 rows]",
            question_spec=None,
            structured_json=_make_structured({"avg_salary": [50000.0]}),
        )
        blocks = [f for f in flags if f.severity == "block"]
        assert len(blocks) == 0

    def test_skips_on_nonzero_return_code(self):
        """HarnessGate should skip all checks when code execution failed."""
        flags = check(
            question="anything",
            code="broken code",
            stdout="",
            return_code=1,
            data_profile="",
            question_spec=None,
            structured_json="",
        )
        assert len(flags) == 0


# ---------------------------------------------------------------------------
# agg_type: SUM-as-average false-positive guard (P0-B)
# ---------------------------------------------------------------------------


class TestAggTypeSumAsAverage:
    """Skip agg_type WARN when code uses SUM(x)/N or SUM(x)/COUNT(...).

    These are valid decomposed-average forms — e.g. "average monthly
    consumption" → SUM(consumption)/12, not AVG(consumption). The pre-fix
    rule wrongly flagged this as a mismatch and locked task_169 in v21.
    """

    def test_sum_divided_by_literal_is_valid_average(self):
        flags = _check_agg_type(
            "What is the average monthly consumption?",
            "SELECT SUM(c.Consumption) / 12.0 AS avg_monthly FROM yearmonth c",
        )
        assert flags == []

    def test_sum_divided_by_count_is_valid_average(self):
        flags = _check_agg_type(
            "What is the average salary across departments?",
            "SELECT SUM(salary) / COUNT(DISTINCT dept_id) FROM employees",
        )
        assert flags == []

    def test_pandas_sum_divided_by_n_is_valid_average(self):
        flags = _check_agg_type(
            "What is the average value?",
            "result = df['x'].sum() / 12",
        )
        assert flags == []

    def test_plain_sum_without_division_still_flags_average(self):
        flags = _check_agg_type(
            "What is the average salary?",
            "SELECT SUM(salary) FROM employees",
        )
        assert any(f.rule == "agg_type" for f in flags)

    def test_sum_with_min_keyword_still_flags(self):
        flags = _check_agg_type(
            "What is the lowest salary?",
            "SELECT SUM(salary) / 10 FROM employees",
        )
        assert any(f.rule == "agg_type" for f in flags)
