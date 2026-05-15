"""Confidence signal for HeavySkill Phase-1 trajectory.

After the baseline trajectory (temp=0) finishes, we need a deterministic
check to decide whether to ship the answer as-is or trigger heavy mode
(K-1 additional trajectories + deliberator).

Design philosophy: skip heavy when every signal says "this is a solid
single-shot answer." Trigger heavy when ANY signal indicates uncertainty.
This way the ~52% of tasks where 3/3 trajectories agree (Exp 2 data) can
keep their cheap M@1 cost, while the ~48% disagreement tasks pay the
heavy-mode tax.

Rules calibrated from Exp 2 (v68/v70 multi-trajectory diversity):
  3/3 agreement → 77% vote-correct → safe to finish
  2/3 majority  → 31% vote-correct → MUST trigger heavy
  1/3 all diff  →  9% vote-correct → MUST trigger heavy
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.types import StepRecord


# Whitelist of HarnessGate WARN rules that, if repeated or accumulated,
# strongly indicate the trajectory is unreliable. These match the rules
# the existing HarnessGate escalates after 3 consecutive fires.
ESCALATABLE_WARN_RULES = frozenset({
    "agg_type",
    "extra_columns",
    "join",
    "qa_column_count",
    "where_value",
    "shape_mismatch",
})


@dataclass(frozen=True)
class ConfidenceReport:
    """Output of is_confident: the decision + a list of failure reasons.

    `is_confident == True` only when reasons is empty. Storing reasons
    makes the trace richer for post-hoc analysis ("why did this task
    trigger heavy mode?").
    """
    is_confident: bool
    reasons: tuple[str, ...] = ()


def evaluate_confidence(
    result: Any,                 # orchestrator.TaskResult — avoid circular import
    max_iterations: int,
) -> ConfidenceReport:
    """Decide whether the baseline trajectory is trustworthy enough to ship.

    Returns ConfidenceReport with the boolean decision and a tuple of
    failure reasons (empty when confident). Reasons are short labels
    suitable for logging.
    """
    reasons: list[str] = []

    # ── 1. Hard failure mode: the pipeline itself reported an error ──
    if not result.success:
        reasons.append("pipeline_error")
    if result.error:
        reasons.append(f"error:{result.error[:40]}")

    # ── 2. Iteration budget exhausted ──
    # Walking the full max_iterations means HarnessGate or Judge kept
    # asking for retries — the model isn't sure. Even if the last
    # iteration was "accepted" (no block), it's a low-confidence accept.
    obs = result.observations or {}
    iterations: list[dict] = obs.get("iterations", []) or []
    steps_executed = len(iterations)
    if steps_executed >= max_iterations:
        reasons.append("max_iterations_reached")

    # ── 3. Final iteration's judge + harness ──
    last_iter = iterations[-1] if iterations else None
    if last_iter is None:
        reasons.append("no_iteration_recorded")
        return ConfidenceReport(False, tuple(reasons))

    # Judge action — only a clean "finish" is a confident accept.
    # NOTE: orchestrator stores this as a FLAT `judge_action` key
    # (e.g. "finish" / "continue" / "continue (warn_soft_retry)" /
    #       "continue (harness_blocked)" / "backtrack"), NOT as a nested
    # `judge.action` dict. The previous code read `last_iter["judge"]["action"]`
    # which silently returned "" and disabled this check.
    judge_action = (last_iter.get("judge_action") or "").lower()
    if judge_action and judge_action != "finish":
        # Truncate to keep observation logs compact; full string is in trace.
        reasons.append(f"judge_{judge_action[:40]}")

    # Sandbox return code — anything non-zero means the final code errored
    if last_iter.get("code_success") is False:
        reasons.append("code_failed")

    # ── 4. HarnessGate flags on final iteration ──
    flags = last_iter.get("harness_flags") or []
    block_count = sum(1 for f in flags if (f.get("severity") or "").lower() == "block")
    warn_rules = [f.get("rule", "") for f in flags if (f.get("severity") or "").lower() == "warn"]

    if block_count:
        reasons.append(f"harness_block:{block_count}")
    if len(warn_rules) >= 2:
        # Multiple WARNs on the accepted iteration is a yellow flag.
        reasons.append(f"warn_count:{len(warn_rules)}")

    # ── 5. Repeated escalatable WARN across iterations ──
    # Same WARN rule firing 2+ times means the model isn't fixing it,
    # just iterating around it.
    if iterations:
        warn_history: dict[str, int] = {}
        for it in iterations:
            for f in (it.get("harness_flags") or []):
                if (f.get("severity") or "").lower() == "warn":
                    rule = f.get("rule", "")
                    if rule in ESCALATABLE_WARN_RULES:
                        warn_history[rule] = warn_history.get(rule, 0) + 1
        repeated = [r for r, n in warn_history.items() if n >= 2]
        if repeated:
            reasons.append(f"repeated_warn:{','.join(sorted(repeated))}")

    # ── 6. Multi-candidate disagreement (intra-call signal) ──
    # If PlannerCoder produced multiple candidates and they didn't all
    # converge on the same answer, that's a strong uncertainty signal.
    # Today the sandbox first-success-wins so we only see this if the
    # observation explicitly records it; future Phase 2 may instrument
    # this more carefully.
    if last_iter.get("multi_candidate_disagree"):
        reasons.append("multi_candidate_disagree")

    return ConfidenceReport(len(reasons) == 0, tuple(reasons))


def is_confident(result: Any, max_iterations: int) -> bool:
    """Boolean convenience wrapper around evaluate_confidence."""
    return evaluate_confidence(result, max_iterations).is_confident
