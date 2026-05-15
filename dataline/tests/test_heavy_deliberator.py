"""Tests for HeavySkill deliberator agent (parser + fallback paths)."""

from dataclasses import dataclass
from typing import Any

import pytest

from dataline.agents.heavy_deliberator import _parse_response, _format_trajectories, deliberate
from dataline.core.types import HeavyTrajectory


# ── Fake LLM client that returns a canned response ──

class _FakeLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        # Save last prompt for inspection
        self.last_system = system
        self.last_user = user
        return self.response


def _traj(
    traj_id: int = 0,
    *,
    answer: str = "x\n1",
    code: str = "SELECT 1",
    lang: str = "sql",
    judge: str = "finish",
    flags: tuple = (),
    success: bool = True,
) -> HeavyTrajectory:
    return HeavyTrajectory(
        traj_id=traj_id,
        temperature=0.0 if traj_id == 0 else 0.7,
        final_answer_csv=answer,
        final_code=code,
        final_code_lang=lang,
        raw_stdout_tail="",
        judge_action=judge,
        judge_reasoning="",
        harness_flags=flags,
        steps_executed=1,
        success=success,
    )


# ─────────────── _parse_response ───────────────

def test_parse_clean_json():
    r = '{"reasoning":"x","matched_trajectory_id":0,"final_answer_csv":"a\\n1"}'
    out = _parse_response(r)
    assert out["matched_trajectory_id"] == 0
    assert out["final_answer_csv"] == "a\n1"


def test_parse_fenced_json():
    r = 'Here is my decision:\n```json\n{"reasoning":"y","matched_trajectory_id":1,"final_answer_csv":"col\\n5"}\n```'
    out = _parse_response(r)
    assert out["matched_trajectory_id"] == 1


def test_parse_prose_with_embedded_json():
    r = 'My analysis: blah blah. {"reasoning":"z","matched_trajectory_id":-1,"final_answer_csv":"c\\n9"} done.'
    out = _parse_response(r)
    assert out["matched_trajectory_id"] == -1


def test_parse_returns_none_on_garbage():
    assert _parse_response("not json at all") is None


def test_parse_returns_none_on_empty():
    assert _parse_response("") is None
    assert _parse_response("   ") is None


# ─────────────── _format_trajectories ───────────────

def test_format_includes_traj_id_and_temp():
    trajs = [_traj(0, answer="a\n1"), _traj(1, answer="a\n2")]
    out = _format_trajectories(trajs)
    assert "traj_id=0" in out
    assert "traj_id=1" in out
    assert "T=0.0" in out
    assert "T=0.7" in out


def test_format_separator():
    trajs = [_traj(0), _traj(1)]
    out = _format_trajectories(trajs)
    # blocks joined by horizontal rule
    assert "\n---\n" in out


# ─────────────── deliberate() main flow ───────────────

def test_deliberate_uses_parsed_answer():
    trajs = [_traj(0, answer="x\n1"), _traj(1, answer="x\n2"), _traj(2, answer="x\n3")]
    llm = _FakeLLM(
        '{"reasoning":"trajectory 1 had right logic","matched_trajectory_id":1,"final_answer_csv":"x\\n2"}'
    )
    dec = deliberate("Q?", trajs, llm)
    assert dec.final_answer_csv == "x\n2"
    assert dec.matched_trajectory_id == 1
    assert dec.trajectories_seen == 3


def test_deliberate_synthesized_answer():
    trajs = [_traj(0, answer="x\n1"), _traj(1, answer="x\n2")]
    llm = _FakeLLM(
        '{"reasoning":"all wrong, recomputed","matched_trajectory_id":-1,"final_answer_csv":"x\\n42"}'
    )
    dec = deliberate("Q?", trajs, llm)
    assert dec.final_answer_csv == "x\n42"
    assert dec.matched_trajectory_id == -1


def test_deliberate_fallback_on_parse_error():
    """Garbage response → fall back to trajectory with longest answer."""
    trajs = [_traj(0, answer="x\n1"), _traj(1, answer="x\n1234567")]
    llm = _FakeLLM("This is not JSON at all, sorry.")
    dec = deliberate("Q?", trajs, llm)
    # Should fall back to the longest answer (traj 1)
    assert dec.final_answer_csv == "x\n1234567"
    assert "parse_fallback" in dec.reasoning


def test_deliberate_fallback_prefers_baseline_on_tie():
    """If trajectories tie on answer length, prefer traj_id=0 (baseline)."""
    trajs = [_traj(0, answer="x\n5"), _traj(1, answer="x\n9")]  # equal length
    llm = _FakeLLM("nope")
    dec = deliberate("Q?", trajs, llm)
    # Both same length; baseline (traj_id=0) wins
    assert dec.matched_trajectory_id == 0


def test_deliberate_empty_csv_fallback():
    """LLM returns valid JSON but empty CSV → fall back."""
    trajs = [_traj(0, answer="x\n1"), _traj(1, answer="x\n22")]
    llm = _FakeLLM('{"reasoning":"hmm","matched_trajectory_id":-1,"final_answer_csv":""}')
    dec = deliberate("Q?", trajs, llm)
    assert "empty_output_fallback" in dec.reasoning
    # Longest non-empty answer
    assert dec.final_answer_csv == "x\n22"


def test_deliberate_no_trajectories():
    """Edge case: caller passes empty list."""
    dec = deliberate("Q?", [], _FakeLLM("anything"))
    assert dec.final_answer_csv == ""
    assert dec.trajectories_seen == 0
    assert "no trajectories" in dec.reasoning


def test_deliberate_prompt_template_substitution():
    """Verify {question}, {K}, {trajectories} all get substituted."""
    trajs = [_traj(0, answer="x\n1"), _traj(1, answer="x\n2"), _traj(2, answer="x\n3")]
    llm = _FakeLLM('{"reasoning":"ok","matched_trajectory_id":0,"final_answer_csv":"x\\n1"}')
    deliberate("What is the count?", trajs, llm, domain_rules="metric = COUNT(*)")
    sys = llm.last_system
    assert "What is the count?" in sys
    assert "3" in sys  # K=3
    assert "metric = COUNT(*)" in sys
    # Should NOT have leftover unsubstituted placeholders
    assert "{question}" not in sys
    assert "{K}" not in sys


def test_deliberate_seed_makes_shuffling_deterministic():
    """Same seed → same trajectory order in prompt."""
    trajs = [_traj(0, answer="A"), _traj(1, answer="B"), _traj(2, answer="C")]
    llm_a = _FakeLLM('{"reasoning":"x","matched_trajectory_id":-1,"final_answer_csv":"r"}')
    llm_b = _FakeLLM('{"reasoning":"x","matched_trajectory_id":-1,"final_answer_csv":"r"}')
    deliberate("Q", trajs, llm_a, seed=42)
    deliberate("Q", trajs, llm_b, seed=42)
    assert llm_a.last_system == llm_b.last_system
