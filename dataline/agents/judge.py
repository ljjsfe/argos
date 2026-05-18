"""Judge agent: unified sufficiency check + routing + guidance.

Replaces the separate Verifier → Router pipeline with a single LLM call,
saving ~30% token cost per iteration while providing richer guidance.

HarnessGate warn flags are passed in via harness_warnings parameter
(block flags are handled by the orchestrator before Judge is called).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.context_manager import ContextManager, Section
from ..core.llm_client import LLMClient

from ..core.token_estimator import cap_text
from ..core.types import AnalysisState, HarnessFlag, JudgeDecision, QuestionSpec, StepRecord


# ---------------------------------------------------------------------------
# Selective rubric routing (Phase 2.1)
# ---------------------------------------------------------------------------
#
# Phase 0.3 production-replay experiments (docs/JUDGE_REPLAY_*.md) showed
# the rubric_v2 prompt is shape-dependent on this benchmark:
#   - Helps on scalar/{count,aggregate,ratio} (silent catch +18/+22/+15pp,
#     clean false-positive 0-17%).
#   - Hurts on list/lookup (clean false-positive 43% — Judge mistakes
#     legitimately-short list answers for "missing rows").
#   - Neutral on scalar/lookup and unknown (limited sample).
#
# This routing exposes a SHAPE WHITELIST: questions whose computation_type
# falls in the whitelist get the rubric prompt; everything else stays on
# the baseline judge.md. The whitelist is data-driven on KDD eval and
# should be re-tuned on any new benchmark (e.g., BIRD, Spider, DABstep).
# Fail-open: unknown or missing computation_type → baseline prompt.
RUBRIC_PROMPT_SHAPES: frozenset[str] = frozenset({"count", "aggregate", "ratio"})

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_BASELINE_PROMPT = _PROMPTS_DIR / "judge.md"
_RUBRIC_PROMPT = _PROMPTS_DIR / "judge_rubric_v2.md"


def _select_prompt_path(question_spec: QuestionSpec | None) -> Path:
    """Route to rubric prompt only on whitelisted question shapes.

    Universal logic: looks only at QuestionSpec.computation_type (a benchmark-
    neutral enum). No question text inspection, no schema dependency.
    """
    if question_spec is None:
        return _BASELINE_PROMPT
    if question_spec.computation_type in RUBRIC_PROMPT_SHAPES:
        # Defensive: if rubric file is missing in a stripped deployment,
        # fall back to baseline so the loop never dies on a routing decision.
        if _RUBRIC_PROMPT.exists():
            return _RUBRIC_PROMPT
    return _BASELINE_PROMPT


def evaluate(
    question: str,
    steps_done: list[StepRecord],
    llm: LLMClient,
    *,
    state: AnalysisState | None = None,
    cm: ContextManager | None = None,
    iteration: int = 0,
    max_iterations: int = 8,
    harness_warnings: list[HarnessFlag] | None = None,
    question_spec: QuestionSpec | None = None,
) -> JudgeDecision:
    """Evaluate progress and decide next action in a single LLM call.

    If state + cm are provided, uses budget-managed context via ContextManager.
    Otherwise falls back to legacy steps_done formatting.
    """
    prompt_path = _select_prompt_path(question_spec)
    template = prompt_path.read_text(encoding="utf-8")

    # Pre-compute iteration thresholds for the template
    max_iter_minus_2 = max(0, max_iterations - 2)
    max_iter_minus_1 = max(0, max_iterations - 1)

    if state is not None and cm is not None:
        sections = _build_sections(state)

        # Inject HarnessGate warn flags as evidence for the LLM judge
        if harness_warnings:
            flags_text = "\n".join(f"- [{f.rule}] {f.message}" for f in harness_warnings)
            sections.append(Section(
                "harness_warnings", flags_text,
                priority=92, compressible=False,
                heading="## HarnessGate Warnings (deterministic — address in your reasoning)",
            ))

        if question_spec is not None and question_spec.tie_possible:
            sections.append(Section(
                "question_spec",
                _format_tie_possible_note(question_spec),
                priority=93,
                compressible=False,
                heading="## Tie-Possible Note (deterministic QuestionSpec)",
            ))

        context = cm.assemble(sections, llm=llm)
        system_prompt = (
            template
            .replace("{question}", state.question)
            .replace("{analysis_context}", context)
            .replace("{iteration}", str(iteration))
            .replace("{max_iterations}", str(max_iterations))
            .replace("{max_iterations_minus_2}", str(max_iter_minus_2))
            .replace("{max_iterations_minus_1}", str(max_iter_minus_1))
        )

    else:
        context = _format_steps(steps_done)
        system_prompt = (
            template
            .replace("{question}", question)
            .replace("{analysis_context}", context)
            .replace("{iteration}", str(iteration))
            .replace("{max_iterations}", str(max_iterations))
            .replace("{max_iterations_minus_2}", str(max_iter_minus_2))
            .replace("{max_iterations_minus_1}", str(max_iter_minus_1))
        )

    response = llm.chat(system_prompt, "Evaluate progress and decide the next action now.")

    try:
        data = json.loads(_extract_json(response))
    except (json.JSONDecodeError, ValueError):
        return JudgeDecision(
            action="continue",
            reasoning="Parse error, defaulting to continue",
        )

    return JudgeDecision(
        action=data.get("action", "continue"),
        reasoning=data.get("reasoning", ""),
        missing=data.get("missing", ""),
        guidance_for_next_step=data.get("guidance_for_next_step", ""),
        truncate_to=data.get("truncate_to", 0),
        quoted_answer=data.get("quoted_answer", ""),
    )


def _build_sections(state: AnalysisState) -> list[Section]:
    """Build prioritized sections for judge context.

    Excludes question (already in template {question}).
    """
    sections: list[Section] = []

    sections.append(Section(
        "manifest", state.manifest_summary,
        priority=70, heading="## Data Sources",
    ))

    if state.domain_rules:
        sections.append(Section(
            "domain_rules", state.domain_rules,
            priority=80, heading="## Domain Rules (use to verify code logic)",
        ))

    if state.question_analysis:
        sections.append(Section(
            "question_analysis", state.question_analysis,
            priority=75, compressible=True,
            heading="## Question Analysis (expected strategy — use to audit code logic)",
        ))

    if state.data_profile_summary:
        sections.append(Section(
            "data_profile_summary", state.data_profile_summary,
            priority=58, compressible=True,
            heading="## Data Profile (column stats — use to sanity-check filter values)",
        ))

    if state.key_findings:
        sections.append(Section(
            "key_findings",
            "\n".join(f"- {f}" for f in state.key_findings),
            priority=75, heading="## Key Findings",
        ))

    if state.completed_steps:
        sections.append(Section(
            "completed_steps",
            "\n".join(state.completed_steps),
            priority=60, heading="## Completed Steps",
        ))

    # Last step with code + output for logic auditing (high priority)
    if state.full_step_details:
        last = state.full_step_details[-1]
        code_text = last.code or "(no code)"
        sections.append(Section(
            "latest_code", f"```python\n{code_text}\n```",
            priority=90, compressible=False,
            heading="## Latest Step Code",
        ))

        stdout = cap_text(last.result.stdout) if last.result.stdout else "(no output)"
        sections.append(Section(
            "latest_output", stdout,
            priority=85, heading="## Latest Step Output",
        ))

        if last.result.return_code != 0 and last.result.stderr:
            sections.append(Section(
                "latest_error", last.result.stderr,
                priority=88, compressible=False,
                heading="## Latest Step Error",
            ))

    if state.judge_guidance:
        sections.append(Section(
            "prior_guidance", state.judge_guidance,
            priority=70, heading="## Prior Guidance",
        ))

    return sections


def _format_tie_possible_note(spec: QuestionSpec) -> str:
    """Format a narrow Judge hint for tie-prone min/max style questions."""
    return (
        "- tie_possible: true\n"
        "- If the latest result has a small number of rows for a highest/lowest/"
        "most/least style entity lookup, verify whether they are tied before "
        "rejecting the result for row count alone.\n"
        "- This note is not a general shape contract; still judge count, ratio, "
        "average, and percentage answers strictly."
    )


def _format_steps(steps: list[StepRecord]) -> str:
    """Legacy formatting when AnalysisState is not available."""
    if not steps:
        return "No steps completed."
    parts: list[str] = []
    for s in steps:
        stdout = s.result.stdout[:800] if s.result.stdout else "(no output)"
        status = "OK" if s.result.return_code == 0 else f"ERROR (rc={s.result.return_code})"
        parts.append(
            f"Step {s.step_index}: {s.plan.step_description}\n"
            f"  Status: {status}\n"
            f"  Output: {stdout}"
        )
    return "\n\n".join(parts)


def _extract_json(text: str) -> str:
    """Extract JSON from response, handling markdown wrapping."""
    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return text
