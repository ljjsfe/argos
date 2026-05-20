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
    _check_filter_no_effect,
    _check_nan_values,
    _check_value_embellishment,
    check,
)
from dataline.core.types import HarnessFlag, QuestionSpec


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
            "entity_name": ["Alpha", "Beta"],
            "optional_notes": ["valid", "null"],
        })
        flags = _check_nan_values(sj)
        assert len(flags) == 1
        assert flags[0].severity == "warn"
        assert "optional_notes" in flags[0].message

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


# ---------------------------------------------------------------------------
# P1: filter-no-effect (scalar zero on aggregation-with-filter question)
# ---------------------------------------------------------------------------

class TestFilterNoEffect:
    """Catches v92 Cluster-A pattern: code runs successfully, returns scalar
    zero on a question that implies a non-zero result. Universal — not
    benchmark-specific."""

    def _spec(self):
        return QuestionSpec()

    def test_count_question_scalar_zero_warns(self):
        s = _make_structured({"count": [0]})
        flags = _check_filter_no_effect(
            "How many patients have abnormal creatinine?", s, self._spec(), []
        )
        assert any(f.rule == "filter_no_effect" and f.severity == "warn" for f in flags)

    def test_ratio_question_scalar_zero_warns(self):
        s = _make_structured({"ratio": [0.0]})
        flags = _check_filter_no_effect(
            "How many times was X more than Y?", s, self._spec(), []
        )
        assert any(f.rule == "filter_no_effect" and f.severity == "warn" for f in flags)

    def test_percentage_zero_warns(self):
        s = _make_structured({"pct": [0]})
        flags = _check_filter_no_effect(
            "What is the percentage of heroes published by Marvel?",
            s, self._spec(), [],
        )
        assert any(f.rule == "filter_no_effect" and f.severity == "warn" for f in flags)

    def test_combined_signal_escalates_to_block(self):
        s = _make_structured({"count": [0]})
        existing = [
            HarnessFlag(rule="sql_where_value", severity="warn",
                        message="WHERE foo='bar' not in DISTINCT"),
        ]
        flags = _check_filter_no_effect(
            "How many patients?", s, self._spec(), existing
        )
        assert any(f.rule == "filter_no_effect" and f.severity == "block" for f in flags)

    def test_non_aggregation_question_does_not_fire(self):
        # Lookup/retrieval questions can legit return 0.
        s = _make_structured({"value": [0]})
        flags = _check_filter_no_effect(
            "What is the score for team X?", s, self._spec(), []
        )
        assert not any(f.rule == "filter_no_effect" for f in flags)

    def test_nonzero_scalar_does_not_fire(self):
        s = _make_structured({"count": [42]})
        flags = _check_filter_no_effect(
            "How many patients?", s, self._spec(), []
        )
        assert not any(f.rule == "filter_no_effect" for f in flags)

    def test_multi_row_does_not_fire(self):
        # Rule scope: scalar 1-row 1-col only.
        s = _make_structured({"count": [0, 0, 0]})
        flags = _check_filter_no_effect(
            "How many patients?", s, self._spec(), []
        )
        assert not any(f.rule == "filter_no_effect" for f in flags)

    def test_multi_column_does_not_fire(self):
        s = _make_structured({"a": [0], "b": [0]})
        flags = _check_filter_no_effect(
            "How many patients?", s, self._spec(), []
        )
        assert not any(f.rule == "filter_no_effect" for f in flags)

    def test_non_numeric_value_does_not_fire(self):
        s = _make_structured({"name": ["foo"]})
        flags = _check_filter_no_effect(
            "How many entries?", s, self._spec(), []
        )
        assert not any(f.rule == "filter_no_effect" for f in flags)

    # --- Non-KDD synthetic coverage (audit 2026-05-20) ---
    # The rule must work on shapes that have nothing to do with the KDD
    # benchmark — financial domain, IoT/sensor domain, epidemiology.

    def test_financial_rate_zero_warns(self):
        """Finance: 'chargeback rate' = 0 is suspicious on a filter
        question — likely the WHERE on payment_type or month missed."""
        s = _make_structured({"chargeback_rate": [0.0]})
        flags = _check_filter_no_effect(
            "What is the chargeback rate for Visa transactions in Q3?",
            s, self._spec(), [],
        )
        assert any(f.rule == "filter_no_effect" for f in flags)

    def test_sensor_threshold_count_zero_warns(self):
        """IoT: 'how many sensor readings exceeded threshold' = 0 is
        suspicious — likely the threshold parsed wrong."""
        s = _make_structured({"cnt": [0]})
        flags = _check_filter_no_effect(
            "How many sensor readings exceeded the 75dB threshold last week?",
            s, self._spec(), [],
        )
        assert any(f.rule == "filter_no_effect" for f in flags)

    def test_epidemiology_prevalence_zero_warns(self):
        """Clinical: 'prevalence of X' = 0 is suspicious."""
        s = _make_structured({"prevalence": [0]})
        flags = _check_filter_no_effect(
            "What was the prevalence of hypertension in the 50-65 age group?",
            s, self._spec(), [],
        )
        assert any(f.rule == "filter_no_effect" for f in flags)

    def test_share_synonym_fires(self):
        """'share of X' is a finance/market synonym for percentage."""
        s = _make_structured({"share": [0]})
        flags = _check_filter_no_effect(
            "What share of orders shipped on time in November?",
            s, self._spec(), [],
        )
        assert any(f.rule == "filter_no_effect" for f in flags)


# ---------------------------------------------------------------------------
# P2: internal-id columns in answer (extra_columns BLOCK)
# ---------------------------------------------------------------------------

class TestWhereNullGuard:
    """C1 (#22): LEFT JOIN + WHERE on right-side col without NULL guard.

    Pure SQL semantic principle — three-valued logic. Universal across
    domains, not benchmark-specific."""

    def _run(self, sql: str):
        from dataline.agents.harness_gate import _check_sql_static
        return _check_sql_static(sql, "", QuestionSpec())

    def test_textbook_left_join_filter_fires(self):
        flags = self._run(
            "SELECT u.id FROM users u "
            "LEFT JOIN orders o ON u.id = o.user_id "
            "WHERE o.status = 'paid'"
        )
        assert any(f.rule == "where_null_guard" and f.severity == "warn" for f in flags)

    def test_inner_join_does_not_fire(self):
        flags = self._run(
            "SELECT u.id FROM users u "
            "INNER JOIN orders o ON u.id = o.user_id "
            "WHERE o.status = 'paid'"
        )
        assert not any(f.rule == "where_null_guard" for f in flags)

    def test_null_guard_present_suppresses(self):
        flags = self._run(
            "SELECT u.id FROM users u "
            "LEFT JOIN orders o ON u.id = o.user_id "
            "WHERE o.status = 'paid' OR o.status IS NULL"
        )
        assert not any(f.rule == "where_null_guard" for f in flags)

    def test_where_on_left_side_does_not_fire(self):
        flags = self._run(
            "SELECT u.id FROM users u "
            "LEFT JOIN orders o ON u.id = o.user_id "
            "WHERE u.name = 'Alice'"  # filter on LEFT table, fine
        )
        assert not any(f.rule == "where_null_guard" for f in flags)

    def test_multiple_null_unsafe_ops_each_flagged(self):
        flags = self._run(
            "SELECT u.id FROM users u "
            "LEFT JOIN orders o ON u.id = o.user_id "
            "WHERE o.status = 'paid' AND o.amount > 100"
        )
        # Both o.status and o.amount should each generate one flag
        njg = [f for f in flags if f.rule == "where_null_guard"]
        assert len(njg) >= 1  # at least one fires; impl may de-dup


class TestTiePossibleTopN:
    """C2 (#23): top-N tie boundary. Question 'top N' + code 'LIMIT N' →
    tie at rank N may be silently dropped."""

    def _run(self, q, code, sj):
        from dataline.agents.harness_gate import _check_tie_possible
        return _check_tie_possible(QuestionSpec(), q, code, sj)

    def test_top_3_with_limit_3_fires(self):
        sj = _make_structured({"name": ["a","b","c"], "score": [10,9,8]})
        flags = self._run(
            "Give me the top 3 highest-scoring users.",
            "SELECT name, score FROM users ORDER BY score DESC LIMIT 3",
            sj,
        )
        assert any(f.rule == "tie_possible" for f in flags)

    def test_top_5_pandas_nlargest_fires(self):
        sj = _make_structured({"name": list("abcde")})
        flags = self._run(
            "What are the top 5 largest cities?",
            "df.nlargest(5, 'population')",
            sj,
        )
        assert any(f.rule == "tie_possible" for f in flags)

    def test_top_n_with_dense_rank_does_not_fire(self):
        # Code uses DENSE_RANK without LIMIT — proper tie handling.
        sj = _make_structured({"name": list("abc")})
        flags = self._run(
            "Top 3 by score.",
            "SELECT name FROM (SELECT name, DENSE_RANK() OVER (ORDER BY score DESC) AS r FROM users) t WHERE r <= 3",
            sj,
        )
        assert not any(f.rule == "tie_possible" for f in flags)

    def test_top_n_row_count_mismatch_does_not_fire(self):
        # Code used LIMIT but result has != N rows — already detects
        # something else upstream; tie rule shouldn't fire.
        sj = _make_structured({"name": list("ab")})  # 2 rows, not 3
        flags = self._run(
            "Top 3 by score.",
            "SELECT name FROM users ORDER BY score DESC LIMIT 3",
            sj,
        )
        assert not any(f.rule == "tie_possible" for f in flags)

    def test_top_1_keeps_single_row_branch(self):
        # 'top 1' = highest 1 → handled by original single-row branch via
        # _TIE_QUESTION_PATTERNS keyword match
        sj = _make_structured({"name": ["a"]})
        flags = self._run(
            "Who is the highest scorer?",
            "SELECT name FROM users ORDER BY score DESC LIMIT 1",
            sj,
        )
        assert any(f.rule == "tie_possible" for f in flags)


class TestCountDistinctNeeded:
    """C3 (#24): question asks for different/unique/distinct count but
    code uses non-deduplicating count. Dual-language: SQL + pandas."""

    def _run(self, q, code):
        from dataline.agents.harness_gate import _check_count_distinct_needed
        return _check_count_distinct_needed(q, code)

    def test_unique_question_count_star_fires(self):
        flags = self._run(
            "How many unique customers placed orders?",
            "SELECT COUNT(*) FROM orders",
        )
        assert any(f.rule == "count_distinct_needed" for f in flags)

    def test_different_question_pandas_len_fires(self):
        flags = self._run(
            "How many different products were sold?",
            "result = len(df_sales)",
        )
        assert any(f.rule == "count_distinct_needed" for f in flags)

    def test_distinct_question_with_count_distinct_passes(self):
        flags = self._run(
            "How many distinct cities are represented?",
            "SELECT COUNT(DISTINCT city) FROM users",
        )
        assert not any(f.rule == "count_distinct_needed" for f in flags)

    def test_distinct_question_with_nunique_passes(self):
        flags = self._run(
            "Count of unique cities?",
            "df['city'].nunique()",
        )
        assert not any(f.rule == "count_distinct_needed" for f in flags)

    def test_distinct_question_with_drop_duplicates_passes(self):
        flags = self._run(
            "How many different products?",
            "len(df.drop_duplicates(subset=['product_id']))",
        )
        assert not any(f.rule == "count_distinct_needed" for f in flags)

    def test_no_distinct_keyword_does_not_fire(self):
        flags = self._run(
            "How many orders were placed?",
            "SELECT COUNT(*) FROM orders",
        )
        assert not any(f.rule == "count_distinct_needed" for f in flags)

    def test_set_python_usage_passes(self):
        flags = self._run(
            "How many unique users?",
            "users = set(df['user_id']); print(len(users))",
        )
        assert not any(f.rule == "count_distinct_needed" for f in flags)


class TestSqlIdentifierTypo:
    """#13: SQL column typo / schema-mismatch detection via manifest
    fuzzy match. Catches `constructorId` vs `constructor_id` style
    failures BEFORE execution. Universal SQL hygiene."""

    DP = (
        "- constructor_id (integer, 100 distinct): 1, 2, 3\n"
        "- constructor_ref (text, 100 distinct): mercedes, ferrari\n"
        "- race_id (integer, 200 distinct): 1, 2, 3\n"
    )

    def _run(self, sql, dp=None):
        from dataline.agents.harness_gate import _check_sql_static
        # explicit None check — "" is intentionally falsy ≠ default
        return _check_sql_static(sql, self.DP if dp is None else dp, QuestionSpec())

    def test_camelcase_vs_snakecase_fires(self):
        flags = self._run("SELECT * FROM t WHERE constructorId = 1")
        assert any(f.rule == "sql_identifier_typo" for f in flags)

    def test_exact_match_does_not_fire(self):
        flags = self._run("SELECT * FROM t WHERE constructor_id = 1")
        assert not any(f.rule == "sql_identifier_typo" for f in flags)

    def test_no_manifest_does_not_fire(self):
        flags = self._run("SELECT * FROM t WHERE constructorId = 1", dp="")
        assert not any(f.rule == "sql_identifier_typo" for f in flags)

    def test_far_off_name_does_not_suggest(self):
        # 'foobarbaz' shouldn't suggest any manifest col (cutoff 0.7)
        flags = self._run("SELECT * FROM t WHERE foobarbaz = 1")
        assert not any(f.rule == "sql_identifier_typo" for f in flags)

    def test_suggestion_includes_close_matches(self):
        flags = self._run("SELECT * FROM t WHERE race_idx = 1")  # race_id close
        njg = [f for f in flags if f.rule == "sql_identifier_typo"]
        assert njg
        assert "race_id" in njg[0].message


class TestInternalIdColumnBlock:
    """Catches v92 task_330: pred had `home_team_api_id`, `away_team_api_id`
    leaking as columns alongside the legitimate answer. These are universal
    'internal identifier' naming conventions across most schemas — not
    benchmark-specific."""

    def test_api_id_column_blocks(self):
        s = _make_structured({
            "final_score": ["1 - 1"],
            "home_team_api_id": [8203],
            "away_team_api_id": [8342],
            "home_team_goal": [1],
            "away_team_goal": [1],
        })
        flags = _check_extra_columns(s)
        blocks = [f for f in flags if f.severity == "block"]
        assert len(blocks) >= 2, "Both *_api_id columns should BLOCK"
        assert all(f.rule == "extra_columns" for f in blocks)

    def test_legitimate_id_named_columns_not_blocked(self):
        # 'id' alone is too generic — many tables have legit `id` columns.
        # Only specific suffixes (_api_id, _pk, _internal_id) should block.
        s = _make_structured({"id": [1, 2], "name": ["a", "b"]})
        flags = _check_extra_columns(s)
        assert not any(f.severity == "block" for f in flags)

    def test_unnamed_column_still_warns(self):
        # Regression: debug-column WARN behavior preserved.
        s = _make_structured({"Unnamed: 0": [1, 2], "name": ["a", "b"]})
        flags = _check_extra_columns(s)
        warns = [f for f in flags if f.severity == "warn"]
        assert any(f.rule == "extra_columns" for f in warns)
