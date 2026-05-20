"""Tests for A3 — heavy deliberator majority-vote safeguard."""

from __future__ import annotations

import pytest

from dataline.agents.heavy_deliberator import (
    _normalize_csv,
    _majority_vote,
    deliberate,
)
from dataline.core.types import HeavyTrajectory, HeavyDecision


def _t(traj_id: int, csv: str, temp: float = 0.7) -> HeavyTrajectory:
    return HeavyTrajectory(
        traj_id=traj_id,
        temperature=temp,
        final_answer_csv=csv,
        final_code="",
        final_code_lang="python",
        raw_stdout_tail="",
        judge_action="finish",
        judge_reasoning="",
    )


# ---- _normalize_csv ----

class TestNormalizeCsv:
    def test_empty(self):
        assert _normalize_csv("") == ""
        assert _normalize_csv("   \n  \n") == ""

    def test_lowercase_and_strip(self):
        a = _normalize_csv("Name\nAlice\nBob")
        b = _normalize_csv("name\n  alice \n  bob  ")
        assert a == b

    def test_row_order_invariant(self):
        a = _normalize_csv("id\n1\n2\n3")
        b = _normalize_csv("id\n3\n1\n2")
        assert a == b

    def test_numeric_rounded_to_2dp(self):
        a = _normalize_csv("ratio\n2.727272727272727")
        b = _normalize_csv("ratio\n2.73")
        assert a == b

    def test_numeric_vs_string_differ(self):
        # Strings stay strings, only digit-parseable cells round.
        a = _normalize_csv("name\nbrawn")
        b = _normalize_csv("name\nmclaren")
        assert a != b

    def test_empty_data_row_is_dropped(self):
        """task_352-style: 'ratio\\n""\\n' must NOT produce a non-empty signature.

        Previously this caused two trajectories to wrongly agree on the
        empty-string data row → false majority. The fix: rows that are
        all-blank after normalisation are dropped, and if no real data
        rows remain the signature is empty.
        """
        assert _normalize_csv('ratio\n""') == ""
        assert _normalize_csv("ratio\n  ,  ") == ""
        assert _normalize_csv("ratio\nNaN\n") == ""

    def test_partial_blank_row_still_kept(self):
        # If at least one cell is non-blank, the row remains.
        sig = _normalize_csv("name,age\nalice,")
        assert sig != ""
        # Header dropped, sig should reflect 2 columns.
        assert sig.startswith("cols=2")

    def test_exact_mode_keeps_header(self):
        sig = _normalize_csv("Country\nUSA", tolerance="exact")
        assert "header=country" in sig

    def test_exact_mode_no_rounding(self):
        # Under exact, 2.7273 and 2.73 must NOT collapse to the same sig.
        a = _normalize_csv("ratio\n2.7273", tolerance="exact")
        b = _normalize_csv("ratio\n2.73", tolerance="exact")
        assert a != b

    def test_kdd_mode_rounds(self):
        # Under kdd_2dp (default), 2.7273 and 2.73 ARE the same.
        a = _normalize_csv("ratio\n2.7273")
        b = _normalize_csv("ratio\n2.73")
        assert a == b

    def test_exact_mode_different_header_names_differ(self):
        # Two CSVs with same data but different headers should NOT match
        # under exact tolerance (DABstep-style scoring).
        a = _normalize_csv("Country\nUSA", tolerance="exact")
        b = _normalize_csv("country\nUSA", tolerance="exact")
        # Note: both headers lowercase to 'country' so they DO match —
        # exact mode is whitespace+case-normalised, not byte-exact.
        assert a == b
        # But a real header difference (column name) WILL differ.
        c = _normalize_csv("name\nUSA", tolerance="exact")
        assert c != a


# ---- _majority_vote ----

class TestMajorityVote:
    def test_unanimous_picks_lowest_temp(self):
        trajs = [
            _t(0, "x\n1", temp=0.0),
            _t(1, "x\n1", temp=0.7),
            _t(2, "x\n1", temp=0.7),
        ]
        winner = _majority_vote(trajs)
        assert winner is not None
        assert winner.traj_id == 0  # lowest temperature on tie

    def test_two_of_three_majority_wins(self):
        """task_415 scenario: 1 wrong baseline, 2 correct heavy trajectories.
        Majority of 2/3 must win (the previous LLM deliberator wrongly chose
        baseline based on hallucinated F1 history)."""
        trajs = [
            _t(0, "constructorRef,url\nbrawn,http://en.wikipedia.org/wiki/Brawn_GP", temp=0.0),
            _t(1, "constructorRef,website\nmclaren,http://en.wikipedia.org/wiki/McLaren", temp=0.7),
            _t(2, "constructorRef,url\nmclaren,http://en.wikipedia.org/wiki/McLaren", temp=0.7),
        ]
        winner = _majority_vote(trajs)
        assert winner is not None
        # The 2 correct mclaren trajectories use different header casing, so
        # the normalized signature should match.
        assert "mclaren" in winner.final_answer_csv.lower()
        assert winner.traj_id in (1, 2)

    def test_all_disagree_returns_none(self):
        """When no answer has ≥2/3 support → fall through to LLM deliberator."""
        trajs = [
            _t(0, "x\n1"),
            _t(1, "x\n2"),
            _t(2, "x\n3"),
        ]
        assert _majority_vote(trajs) is None

    def test_empty_answer_skipped(self):
        """An empty trajectory cannot win the majority even if multiple are empty."""
        trajs = [
            _t(0, ""),
            _t(1, ""),
            _t(2, "x\n42"),
        ]
        assert _majority_vote(trajs) is None

    def test_empty_data_row_does_not_win(self):
        """Two trajectories with all-empty data rows must not form a majority
        (task_352 bug). The third trajectory is the only real candidate."""
        trajs = [
            _t(0, 'ratio\n""'),
            _t(1, 'ratio\n""'),
            _t(2, "ratio\n2.73"),
        ]
        # No majority on real data → fall through to LLM.
        assert _majority_vote(trajs) is None

    def test_single_trajectory(self):
        trajs = [_t(0, "x\n1")]
        # K=1 → threshold=1 → single trajectory wins.
        winner = _majority_vote(trajs)
        assert winner is not None
        assert winner.traj_id == 0

    def test_k4_strict_majority(self):
        """For K=4, threshold = 4//2 + 1 = 3 (strict majority)."""
        trajs = [
            _t(0, "x\n1"),
            _t(1, "x\n2"),
            _t(2, "x\n2"),
            _t(3, "x\n2"),
        ]
        winner = _majority_vote(trajs)
        assert winner is not None
        assert winner.final_answer_csv == "x\n2"

    def test_k4_tie_no_majority(self):
        """For K=4 with 2-2 split, no strict majority → None."""
        trajs = [
            _t(0, "x\n1"),
            _t(1, "x\n1"),
            _t(2, "x\n2"),
            _t(3, "x\n2"),
        ]
        assert _majority_vote(trajs) is None


# ---- deliberate() short-circuits when majority found ----

class TestDeliberateMajorityShortCircuit:
    def test_majority_skips_llm(self, monkeypatch):
        """When majority exists, deliberate() must not call the LLM."""
        called = {"chat": 0}

        class _FakeLLM:
            def chat(self, *a, **kw):
                called["chat"] += 1
                return '{"final_answer_csv":"x","reasoning":"r","matched_trajectory_id":0}'

        trajs = [
            _t(0, "x\n1", temp=0.0),
            _t(1, "x\n1", temp=0.7),
            _t(2, "x\n2", temp=0.7),
        ]
        decision = deliberate("dummy?", trajs, _FakeLLM())
        assert called["chat"] == 0, "LLM should not be called when majority is present"
        assert isinstance(decision, HeavyDecision)
        assert "majority_vote" in decision.reasoning
        assert decision.matched_trajectory_id in (0, 1)

    def test_disagreement_calls_llm(self):
        """When all trajectories disagree, LLM must be called as before."""
        seen_prompts = []

        class _FakeLLM:
            def chat(self, system, user):
                seen_prompts.append(system)
                return '{"final_answer_csv":"x\\n7","reasoning":"chose","matched_trajectory_id":-1}'

        trajs = [
            _t(0, "x\n1", temp=0.0),
            _t(1, "x\n2", temp=0.7),
            _t(2, "x\n3", temp=0.7),
        ]
        decision = deliberate("dummy?", trajs, _FakeLLM())
        assert len(seen_prompts) == 1
        assert decision.final_answer_csv == "x\n7"
