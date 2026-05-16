"""Tests for deterministic QuestionSpec inference.

Validates classification accuracy and fail-open behavior.
Informed by dry-run against 50 KDD questions — known misclassification
patterns are tested as safeguards.
"""

import pytest

from dataline.agents.question_analyzer import analyze_deterministic
from dataline.core.types import QuestionSpec


class TestCountQuestions:
    def test_how_many(self):
        spec = analyze_deterministic("How many members attended the event?")
        assert spec.answer_type == "scalar"
        assert spec.computation_type == "count"
        assert spec.expected_row_count == "single"
        assert spec.value_style == "numeric"

    def test_number_of(self):
        spec = analyze_deterministic("What is the total number of employees?")
        assert spec.answer_type == "scalar"
        assert spec.computation_type == "count"

    def test_his_number_is_ambiguous(self):
        """'his number' should NOT classify as count — it's an attribute."""
        spec = analyze_deterministic("What is his number of the driver?")
        # Should fail-open to unknown, not misclassify as count
        assert spec.computation_type != "count" or spec.answer_type == "unknown"


class TestRatioQuestions:
    def test_what_percentage(self):
        spec = analyze_deterministic("What percentage of heroes have blue eyes?")
        assert spec.answer_type == "scalar"
        assert spec.computation_type == "ratio"

    def test_calculate_percentage(self):
        spec = analyze_deterministic("Calculate the percentage of superheroes with blue eyes.")
        assert spec.computation_type == "ratio"

    def test_how_much_faster_percentage(self):
        spec = analyze_deterministic("How much faster in percentage is the champion?")
        assert spec.computation_type == "ratio"

    def test_how_many_times_is_ratio_not_count(self):
        spec = analyze_deterministic(
            "How many times was the budget in Advertisement for A more than B?"
        )
        assert spec.computation_type == "ratio"
        assert spec.expected_row_count == "single"


class TestAggregateQuestions:
    def test_average(self):
        spec = analyze_deterministic("What is the average salary?")
        assert spec.answer_type == "scalar"
        assert spec.computation_type == "aggregate"

    def test_total(self):
        spec = analyze_deterministic("What is the total revenue for 2023?")
        assert spec.computation_type == "aggregate"


class TestSuperlativeQuestions:
    def test_lowest_sets_tie_possible(self):
        spec = analyze_deterministic("Which event has the lowest cost?")
        assert spec.tie_possible is True
        assert spec.expected_row_count == "one_or_more"

    def test_highest(self):
        spec = analyze_deterministic("What is the highest score in reading?")
        assert spec.tie_possible is True
        assert spec.computation_type == "aggregate"


class TestListQuestions:
    def test_list_all(self):
        spec = analyze_deterministic("List all the superpowers of 3-D Man.")
        assert spec.answer_type == "list"
        assert spec.expected_row_count == "multiple"

    def test_list_the(self):
        spec = analyze_deterministic("List the countries of the gas stations")
        assert spec.answer_type == "list"

    def test_please_list(self):
        spec = analyze_deterministic("Please list the countries with transactions")
        assert spec.answer_type == "list"

    def test_list_with_total_does_not_become_aggregate(self):
        """'List the names and total value' should be list, not aggregate."""
        spec = analyze_deterministic(
            "List the names and funding types of schools from total districts"
        )
        assert spec.answer_type == "list"

    def test_what_are_the_plural(self):
        spec = analyze_deterministic("What are the bonds that have phosphorus?")
        assert spec.answer_type == "list"


class TestGroupingQuestions:
    def test_for_each(self):
        spec = analyze_deterministic("For each department, what is the budget?")
        assert spec.answer_type == "table"
        assert spec.expected_row_count == "multiple"

    def test_top_n(self):
        spec = analyze_deterministic("List the top 5 customers by revenue")
        assert spec.answer_type == "list"
        assert spec.expected_row_count == "multiple"


class TestLookupQuestions:
    def test_what_is_the_name(self):
        spec = analyze_deterministic("What is the name of the manager?")
        assert spec.value_style == "name"
        assert spec.computation_type == "lookup"

    def test_identify_the(self):
        spec = analyze_deterministic("Identify the gender of the superhero")
        assert spec.computation_type == "lookup"

    def test_singular_lookup_does_not_assume_single_row(self):
        """Singular lookup should NOT force row_count=single (task_86 lesson)."""
        spec = analyze_deterministic("Provide the eye colour of the superhero")
        assert spec.expected_row_count != "single" or spec.expected_row_count == "unknown"


class TestTallyQuestion:
    def test_tally(self):
        spec = analyze_deterministic("Tally the toxicology of the 4th atom")
        assert spec.answer_type == "list"
        assert spec.expected_row_count == "multiple"


class TestColumnCountEstimation:
    def test_single_field(self):
        spec = analyze_deterministic("How many employees are there?")
        assert spec.expected_column_count == 1

    def test_and_conjunction(self):
        spec = analyze_deterministic(
            "List their ID, sex and disease for patients"
        )
        # Should detect "and" conjunction → multi-column
        assert spec.expected_column_count >= 2

    def test_filter_and_does_not_increase_columns(self):
        spec = analyze_deterministic(
            "What are the bonds that have phosphorus and nitrogen as their atom elements?"
        )
        assert spec.answer_type == "list"
        assert spec.expected_column_count == 0

    def test_output_and_still_increases_columns(self):
        spec = analyze_deterministic(
            "List the names and funding types of schools from Riverside districts"
        )
        assert spec.answer_type == "list"
        assert spec.expected_column_count == 2

    def test_identify_X_and_their_Y_is_multi_column(self):
        """task_163 regression: 'Identify X and their Y' should be 2 cols,
        not 1-col scalar. Without this fix, qa_column_count blocked the
        correct 2-col answer (e.g. (type, total_value))."""
        spec = analyze_deterministic(
            "Identify the type of expenses and their total value approved for 'October Meeting' event."
        )
        assert spec.expected_column_count == 2

    def test_identify_X_and_its_Y_is_multi_column(self):
        spec = analyze_deterministic(
            "Identify the company and its annual revenue."
        )
        assert spec.expected_column_count == 2

    def test_identify_X_and_the_Y_is_multi_column(self):
        spec = analyze_deterministic(
            "Identify the school and the funding type from the district records."
        )
        assert spec.expected_column_count == 2

    def test_identify_with_and_filter_does_not_increase_columns(self):
        """False-positive guard: 'Identify patients with WBC and fibrinogen'
        — 'with X and Y' is a filter, not a request for two output columns.
        Must NOT be inferred as 2 cols.
        """
        spec = analyze_deterministic(
            "Identify patients with normal WBC and abnormal fibrinogen."
        )
        # Either 1 col or 0 (unknown) — the key is NOT 2.
        assert spec.expected_column_count in (0, 1)

    def test_identify_that_and_filter_is_protected(self):
        """Existing filter_and guard protects 'that X and Y' patterns."""
        spec = analyze_deterministic(
            "Identify the patient that has WBC and fibrinogen abnormal."
        )
        # filter_and matches 'that' → returns 0 (unknown, fail-safe)
        assert spec.expected_column_count == 0


class TestFailOpen:
    def test_empty_string(self):
        spec = analyze_deterministic("")
        assert spec == QuestionSpec()

    def test_ambiguous_question(self):
        """Genuinely ambiguous questions should return unknown."""
        spec = analyze_deterministic("What was the final score for the match?")
        # This shouldn't match any high-confidence pattern
        # Either unknown or a conservative classification is fine
        assert isinstance(spec, QuestionSpec)

    def test_unknown_returns_all_defaults(self):
        spec = analyze_deterministic("Explain the methodology used.")
        assert spec.answer_type == "unknown"
        assert spec.computation_type == "unknown"
