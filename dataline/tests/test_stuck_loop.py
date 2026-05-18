"""Tests for D7 stuck-loop detection helper (Phase 2.5).

Audit-validated invariant: a candidate ≥97% similar to a prior winning
code is a Planner stuck loop. Universal property of iterative coding
agents — applies on any benchmark, any data agent.
"""

from dataline.agents.orchestrator import (
    STUCK_SIMILARITY_THRESHOLD,
    _is_stuck_candidate,
)


class TestStuckCandidate:
    def test_empty_candidate_not_stuck(self):
        assert _is_stuck_candidate("", ["SELECT 1"], STUCK_SIMILARITY_THRESHOLD) is False

    def test_no_prior_codes_not_stuck(self):
        assert _is_stuck_candidate("SELECT 1", [], STUCK_SIMILARITY_THRESHOLD) is False

    def test_identical_code_is_stuck(self):
        code = "SELECT a, b FROM tbl WHERE x > 10"
        assert _is_stuck_candidate(code, [code], STUCK_SIMILARITY_THRESHOLD) is True

    def test_minor_whitespace_diff_still_stuck(self):
        a = "SELECT a, b FROM tbl WHERE x > 10"
        b = "SELECT a, b FROM tbl WHERE x >  10"  # extra space
        assert _is_stuck_candidate(a, [b], STUCK_SIMILARITY_THRESHOLD) is True

    def test_substantively_different_not_stuck(self):
        a = "SELECT a, b FROM tbl_one WHERE x > 10"
        b = "SELECT c, d FROM tbl_two WHERE y < 5 GROUP BY z"
        assert _is_stuck_candidate(a, [b], STUCK_SIMILARITY_THRESHOLD) is False

    def test_matches_any_prior_not_all(self):
        cand = "SELECT * FROM users WHERE id = 1"
        priors = [
            "import pandas as pd; df = pd.read_csv('a.csv')",  # unrelated
            "SELECT * FROM users WHERE id = 1",  # exact match
            "SELECT count(*) FROM orders",
        ]
        assert _is_stuck_candidate(cand, priors, STUCK_SIMILARITY_THRESHOLD) is True

    def test_threshold_respected(self):
        """A change >3% of the code should fall below 0.97 threshold."""
        a = "SELECT count(*) FROM users WHERE active = 1 AND created_at > '2025-01-01'"
        # Change one literal: 2025 → 2024
        b = "SELECT count(*) FROM users WHERE active = 1 AND created_at > '2024-01-01'"
        # 0.97 threshold should still consider these stuck (only 1 char diff)
        assert _is_stuck_candidate(a, [b], STUCK_SIMILARITY_THRESHOLD) is True

    def test_skips_empty_prior_entries(self):
        cand = "SELECT 1"
        # Empty string in priors list should be skipped, not error
        assert _is_stuck_candidate(cand, ["", "  ", ""], STUCK_SIMILARITY_THRESHOLD) is False
