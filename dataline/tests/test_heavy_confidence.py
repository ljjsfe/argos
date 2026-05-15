"""Tests for HeavySkill confidence signal."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from dataline.agents.heavy_confidence import evaluate_confidence, is_confident


# ── lightweight TaskResult stand-in to avoid circular import ──
@dataclass
class _MockResult:
    success: bool = True
    error: str = ""
    observations: dict[str, Any] = field(default_factory=dict)


def _make_iteration(
    *,
    judge_action: str = "finish",
    code_success: bool = True,
    warn_rules: list[str] | None = None,
    block_rules: list[str] | None = None,
    multi_candidate_disagree: bool = False,
) -> dict:
    flags = []
    for r in (warn_rules or []):
        flags.append({"rule": r, "severity": "warn"})
    for r in (block_rules or []):
        flags.append({"rule": r, "severity": "block"})
    return {
        "judge": {"action": judge_action},
        "code_success": code_success,
        "harness_flags": flags,
        "multi_candidate_disagree": multi_candidate_disagree,
    }


def _result(iterations: list[dict], success: bool = True, error: str = "") -> _MockResult:
    return _MockResult(
        success=success,
        error=error,
        observations={"iterations": iterations},
    )


# ─────────────── confident cases (should accept) ───────────────

def test_clean_single_iteration_finish_is_confident():
    """One iteration, judge=finish, no warns, no errors → ship it."""
    r = _result([_make_iteration()])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is True
    assert rep.reasons == ()


def test_single_warn_still_confident():
    """One non-repeated WARN is acceptable."""
    r = _result([_make_iteration(warn_rules=["nan_answer"])])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is True


def test_multi_iteration_eventually_finish_is_confident():
    """Iterate a bit, eventually judge finishes cleanly."""
    r = _result([
        _make_iteration(judge_action="continue", warn_rules=["agg_type"]),
        _make_iteration(judge_action="finish"),
    ])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is True


# ─────────────── not-confident cases (should trigger heavy) ───────────────

def test_pipeline_failure_not_confident():
    r = _result([_make_iteration()], success=False, error="exec failed")
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert "pipeline_error" in rep.reasons


def test_max_iterations_exhausted_not_confident():
    """8 iterations = exhausted = model wasn't sure."""
    iters = [_make_iteration() for _ in range(8)]
    r = _result(iters)
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert "max_iterations_reached" in rep.reasons


def test_judge_continue_not_confident():
    r = _result([_make_iteration(judge_action="continue")])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert any("judge_continue" in x for x in rep.reasons)


def test_judge_backtrack_not_confident():
    r = _result([_make_iteration(judge_action="backtrack")])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False


def test_code_failed_not_confident():
    r = _result([_make_iteration(code_success=False)])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert "code_failed" in rep.reasons


def test_harness_block_not_confident():
    r = _result([_make_iteration(block_rules=["nan_answer"])])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert any("harness_block" in x for x in rep.reasons)


def test_two_warns_not_confident():
    """≥2 WARNs on the accepted iteration is yellow flag."""
    r = _result([_make_iteration(warn_rules=["nan_answer", "qa_column_count"])])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert any("warn_count" in x for x in rep.reasons)


def test_repeated_escalatable_warn_not_confident():
    """Same WARN firing 2+ times across iterations → model isn't fixing it."""
    r = _result([
        _make_iteration(judge_action="continue", warn_rules=["agg_type"]),
        _make_iteration(judge_action="finish", warn_rules=["agg_type"]),
    ])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert any("repeated_warn:agg_type" in x for x in rep.reasons)


def test_repeated_non_escalatable_warn_is_confident():
    """nan_answer is NOT in ESCALATABLE_WARN_RULES, so 2x is still OK."""
    r = _result([
        _make_iteration(judge_action="continue", warn_rules=["nan_answer"]),
        _make_iteration(judge_action="finish", warn_rules=["nan_answer"]),
    ])
    rep = evaluate_confidence(r, max_iterations=8)
    # Last iteration only has 1 WARN so warn_count check doesn't fire,
    # and nan_answer isn't escalatable. Should still be confident.
    assert rep.is_confident is True


def test_multi_candidate_disagree_not_confident():
    r = _result([_make_iteration(multi_candidate_disagree=True)])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert "multi_candidate_disagree" in rep.reasons


def test_empty_iterations_not_confident():
    """No iteration recorded → can't trust the answer."""
    r = _result([])
    rep = evaluate_confidence(r, max_iterations=8)
    assert rep.is_confident is False
    assert "no_iteration_recorded" in rep.reasons


# ─────────────── is_confident() boolean wrapper ───────────────

def test_is_confident_wrapper_true():
    r = _result([_make_iteration()])
    assert is_confident(r, max_iterations=8) is True


def test_is_confident_wrapper_false():
    r = _result([_make_iteration(judge_action="continue")])
    assert is_confident(r, max_iterations=8) is False
