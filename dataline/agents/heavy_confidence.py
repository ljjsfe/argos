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

import re
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

    # ── 7. Final-answer sanity checks ──
    # These are intentionally conservative and generic. They do not reject
    # the answer; they only say "do not trust the cheap single trajectory".
    # This targets the recurring v75 failure mode where a clean 1-shot SQL
    # answer passed Judge/Harness but answered the wrong requested entity
    # (e.g. "countries" → Date column, "finish time" → time+milliseconds).
    reasons.extend(_answer_sanity_reasons(result))

    return ConfidenceReport(len(reasons) == 0, tuple(reasons))


def is_confident(result: Any, max_iterations: int) -> bool:
    """Boolean convenience wrapper around evaluate_confidence."""
    return evaluate_confidence(result, max_iterations).is_confident


# ---------------------------------------------------------------------------
# Final-answer sanity checks
# ---------------------------------------------------------------------------

_SCALAR_Q_RE = re.compile(
    r"\b(how many|what is|what's|calculate|compute|percentage|percent|ratio|"
    r"average|mean|sum|total|count|number of)\b",
    re.IGNORECASE,
)
_LIST_Q_RE = re.compile(r"\b(list|give|show|which|what are|who are)\b", re.IGNORECASE)

_REQUESTED_COLUMN_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("country", ("country", "countries", "nation")),
    ("date", ("date", "day", "month", "year")),
    ("time", ("time", "duration", "milliseconds")),
    ("name", ("name", "title")),
    ("phone", ("phone", "number")),
    ("text", ("text", "comment", "body", "description", "content")),
    ("type", ("type", "category", "status")),
    ("funding", ("funding", "fund")),
    ("id", ("id", "identifier")),
)

_AMBIGUOUS_HINTS = {"number"}  # "driver number" is valid; "number of" is scalar.


def _answer_sanity_reasons(result: Any) -> list[str]:
    """Return uncertainty reasons derived from final answer shape/columns.

    Uses only the final answer dict and natural-language question. The checks
    are precision-biased: they fire on obvious shape/entity mismatches and
    avoid domain-specific labels.
    """
    answer = getattr(result, "answer", None)
    question = (getattr(result, "question", "") or "").lower()
    if not isinstance(answer, dict):
        return []

    reasons: list[str] = []
    columns = [str(c) for c in answer.keys()]
    normalized_cols = [_norm_col(c) for c in columns]
    row_count = _answer_row_count(answer)

    if not answer or row_count == 0:
        reasons.append("answer_empty")
        return reasons

    if _looks_scalar_question(question) and not _looks_compound_scalar_question(question) and len(columns) > 1:
        # One scalar question should not produce extra data columns. This is a
        # common KDD scoring failure because extra columns reduce matching.
        reasons.append(f"answer_extra_columns:{len(columns)}")

    if _looks_list_question(question) and len(columns) == 1:
        requested = _requested_hints(question)
        if requested and not _columns_match_any_hint(normalized_cols, requested):
            reasons.append("answer_column_semantic_mismatch")

    if _asks_for_tally(question) and not _has_count_column(normalized_cols):
        reasons.append("answer_missing_count_column")

    return reasons


def _answer_row_count(answer: dict) -> int:
    if not answer:
        return 0
    lengths = []
    for value in answer.values():
        if isinstance(value, list):
            lengths.append(len(value))
        elif value is None or str(value).strip() == "":
            lengths.append(0)
        else:
            lengths.append(1)
    return max(lengths) if lengths else 0


def _looks_scalar_question(question: str) -> bool:
    # "number of" is scalar, but "driver number" usually asks for a list
    # column named number. Avoid over-triggering on "which ... number".
    if re.search(r"\bwhich\b.+\bnumber\b", question):
        return False
    return bool(_SCALAR_Q_RE.search(question))


def _looks_compound_scalar_question(question: str) -> bool:
    """Return True for questions that legitimately ask for multiple scalars."""
    metric_hits = len(re.findall(
        r"\b(average|avg|mean|sum|total|count|percentage|percent|ratio|number of)\b",
        question,
    ))
    return metric_hits >= 2 and bool(re.search(r"\b(and|,)\b", question))


def _looks_list_question(question: str) -> bool:
    return bool(_LIST_Q_RE.search(question))


def _asks_for_tally(question: str) -> bool:
    return bool(re.search(r"\b(tally|count by|counts by|frequency|distribution)\b", question))


def _norm_col(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", col.lower()).strip("_")


def _requested_hints(question: str) -> set[str]:
    hints: set[str] = set()
    for canonical, terms in _REQUESTED_COLUMN_HINTS:
        for term in terms:
            if term in _AMBIGUOUS_HINTS:
                continue
            if re.search(rf"\b{re.escape(term)}s?\b", question):
                hints.add(canonical)
                break
    # "countries" is not covered by the simple optional-s regex above.
    if re.search(r"\bcountries\b", question):
        hints.add("country")
    return hints


def _columns_match_any_hint(cols: list[str], hints: set[str]) -> bool:
    for col in cols:
        for hint in hints:
            if hint in col:
                return True
            if hint == "country" and ("nation" in col or "country" in col):
                return True
            if hint == "id" and (col == "id" or col.endswith("_id") or col.endswith("id")):
                return True
    return False


def _has_count_column(cols: list[str]) -> bool:
    return any(
        col in {"count", "cnt", "n", "frequency", "freq", "tally"}
        or col.endswith("_count")
        or col.endswith("_cnt")
        for col in cols
    )
