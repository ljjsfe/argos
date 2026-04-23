"""PlannerCoder: unified planning + code generation in a single LLM call.

Merges the former Planner and Coder agents. The LLM sees the full context
(question, schema, domain knowledge, prior results, judge guidance) and produces
both the reasoning (plan) and the executable artifact (code candidates) in one shot.

Key design decisions:
- Single call avoids information loss between plan→code translation.
- Multi-candidate output: LLM proposes up to 3 code candidates (SQL or Python).
  Sandbox tries them in order — first success wins. Extra candidates are free.
- SQL-focused prompt section activates when data is structured (CSV/SQLite).
- Falls back naturally to Python for complex/multi-step analysis.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.context_manager import ContextManager, Section
from ..core.types import AnalysisState, PlanStep, StepRecord

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = (Path(__file__).parent.parent / "prompts" / "planner_coder.md").read_text()


@dataclass(frozen=True)
class PlannerCoderOutput:
    """Output from the unified PlannerCoder agent."""
    plan: PlanStep
    candidates: tuple[str, ...]  # ordered code candidates (SQL or Python)
    language: str  # "sql" | "python" — primary language chosen
    reasoning: str = ""


def generate(
    question: str,
    manifest_json: str,
    data_profile: str,
    steps_done: list[StepRecord],
    llm: Any,
    *,
    state: AnalysisState | None = None,
    cm: ContextManager | None = None,
    qa_guidance: str = "",
) -> PlannerCoderOutput:
    """Generate plan + code candidates in a single LLM call.

    Uses ContextManager to assemble full context within token budget.
    Returns PlannerCoderOutput with plan and ordered candidates.
    """
    if state and cm:
        prompt = _build_context_managed_prompt(state, cm, llm, qa_guidance=qa_guidance)
    else:
        prompt = _build_legacy_prompt(question, manifest_json, data_profile, steps_done)

    response = llm.chat(
        system=_PROMPT_TEMPLATE,
        user=prompt,
    )

    return _parse_response(response)


def _build_context_managed_prompt(
    state: AnalysisState,
    cm: ContextManager,
    llm: Any,
    *,
    qa_guidance: str = "",
) -> str:
    """Build budget-managed context with all information in one prompt."""
    sections = []

    # Question — highest priority, never compress
    sections.append(Section(
        name="question",
        content=f"## Question\n{state.question}",
        priority=100,
        compressible=False,
        heading="",
    ))

    # Judge guidance — must address (second highest priority)
    if state.judge_guidance:
        sections.append(Section(
            name="judge_guidance",
            content=f"## Judge Guidance (MUST ADDRESS)\n{state.judge_guidance}",
            priority=95,
            compressible=False,
            heading="",
        ))

    # QA guidance — structural hints from QuestionAnalyzer
    if qa_guidance:
        sections.append(Section(
            name="qa_guidance",
            content=f"## Answer Shape Hints (from question analysis)\n{qa_guidance}",
            priority=85,
            compressible=False,
            heading="",
        ))

    # Data manifest (schema) — critical for code generation
    sections.append(Section(
        name="manifest",
        content=f"## Data Schema\n{state.manifest_summary}",
        priority=90,
        compressible=False,
        heading="",
    ))

    # Domain rules from documentation
    if state.domain_rules:
        sections.append(Section(
            name="domain_rules",
            content=f"## Domain Knowledge\n{state.domain_rules}",
            priority=80,
            compressible=True,
            heading="",
        ))

    # Data profile (column stats, distributions)
    if state.data_profile_summary:
        sections.append(Section(
            name="data_profile",
            content=f"## Data Profile\n{state.data_profile_summary}",
            priority=70,
            compressible=True,
            heading="",
        ))

    # Question analysis / strategy (from prior decomposition, if any)
    if state.question_analysis:
        sections.append(Section(
            name="question_analysis",
            content=f"## Analysis Strategy\n{state.question_analysis}",
            priority=65,
            compressible=True,
            heading="",
        ))

    # Variables in scope (pickled intermediates from prior steps)
    if state.variables_in_scope:
        vars_text = "\n".join(
            f"- `{name}`: {desc}" for name, desc in state.variables_in_scope
        )
        sections.append(Section(
            name="variables",
            content=f"## Available Variables (in TEMP_DIR)\n{vars_text}",
            priority=75,
            compressible=False,
            heading="",
        ))

    # Prior steps — full detail for recent, summary for older
    if state.full_step_details:
        prior_text = _format_prior_steps(state)
        sections.append(Section(
            name="prior_steps",
            content=f"## Prior Steps\n{prior_text}",
            priority=60,
            compressible=True,
            heading="",
        ))

    return cm.assemble(sections, llm=llm)


def _format_prior_steps(state: AnalysisState) -> str:
    """Format prior steps: full detail for last 2, summary for older."""
    parts: list[str] = []
    details = state.full_step_details

    # Older steps: 1-line summary
    if len(details) > 2:
        for step_line in state.completed_steps[:-2]:
            parts.append(f"  {step_line}")

    # Last 2 steps: full code + output
    recent = details[-2:] if len(details) >= 2 else details
    for step in recent:
        parts.append(f"\n### Step {step.step_index}: {step.plan.step_description}")
        parts.append(f"```python\n{step.code}\n```")
        stdout_preview = step.result.stdout[:3000] if step.result.stdout else "(no output)"
        parts.append(f"Output:\n```\n{stdout_preview}\n```")
        if step.result.return_code != 0:
            parts.append(f"Error: {step.result.stderr[:500]}")

    return "\n".join(parts)


def _build_legacy_prompt(
    question: str,
    manifest_json: str,
    data_profile: str,
    steps_done: list[StepRecord],
) -> str:
    """Fallback for when state/cm not provided."""
    parts = [
        f"## Question\n{question}",
        f"## Data Schema\n{manifest_json}",
    ]
    if data_profile:
        parts.append(f"## Data Profile\n{data_profile}")
    if steps_done:
        prior = "\n".join(
            f"Step {s.step_index}: {s.plan.step_description} → {s.result.stdout[:200]}"
            for s in steps_done[-3:]
        )
        parts.append(f"## Prior Steps\n{prior}")
    return "\n\n".join(parts)


def _parse_response(response: str) -> PlannerCoderOutput:
    """Parse LLM response into PlannerCoderOutput.

    Expected format:
    1. JSON block with plan + language + reasoning
    2. One or more code blocks (```sql or ```python)
    """
    # Extract JSON plan block
    json_match = re.search(r"```json\s*(\{.*?\})\s*```", response, re.DOTALL)
    if json_match:
        try:
            plan_data = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            plan_data = {}
    else:
        # Try to find inline JSON
        plan_data = _extract_inline_json(response)

    # Extract code candidates
    candidates = _extract_code_candidates(response)

    # If no candidates found, try to extract any code-like content
    if not candidates:
        # Last resort: treat entire response as a single Python candidate
        candidates = (_clean_response_as_code(response),)

    # Build PlanStep from parsed plan
    plan = PlanStep(
        step_description=plan_data.get("plan", plan_data.get("step_description", "Execute analysis step")),
        data_sources=tuple(plan_data.get("data_sources", [])),
        depends_on_prior=plan_data.get("depends_on_prior", False),
        expected_output=plan_data.get("expected_output", ""),
    )

    language = plan_data.get("language", "python")
    reasoning = plan_data.get("reasoning", "")

    return PlannerCoderOutput(
        plan=plan,
        candidates=candidates,
        language=language,
        reasoning=reasoning,
    )


def _extract_code_candidates(response: str) -> tuple[str, ...]:
    """Extract all code blocks from response as candidates."""
    # Match ```sql, ```python, or plain ``` blocks
    pattern = r"```(?:sql|python|py)?\s*\n(.*?)\n```"
    matches = re.findall(pattern, response, re.DOTALL)

    # Filter out the JSON plan block
    candidates = []
    for match in matches:
        stripped = _strip_fence_lines(match.strip())
        # Skip JSON blocks
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                json.loads(stripped)
                continue  # This was the plan JSON, skip
            except json.JSONDecodeError:
                pass
        if stripped:
            candidates.append(stripped)

    return tuple(candidates)


def _strip_fence_lines(code: str) -> str:
    """Remove residual markdown fence lines from extracted code.

    Some models (e.g. Qwen) wrap output in double fences:
        ```python
        ```python        ← captured as line 1, causes SyntaxError
        import pandas...
        ```
    Strip any lines that are purely a code fence marker.
    """
    lines = code.split("\n")
    cleaned = [ln for ln in lines if not re.match(r"^\s*```", ln)]
    return "\n".join(cleaned)


def _extract_inline_json(response: str) -> dict:
    """Try to extract JSON object from response without code fences."""
    # Find first { ... } that looks like plan
    match = re.search(r'\{[^{}]*"plan"[^{}]*\}', response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Try broader match
    match = re.search(r'\{[^{}]*"step_description"[^{}]*\}', response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def _clean_response_as_code(response: str) -> str:
    """Last resort: extract code-like content from response."""
    lines = response.split("\n")
    code_lines = [
        line for line in lines
        if not line.startswith("#") or line.startswith("# ")  # keep Python comments
        if not line.startswith("```")
        if not line.strip().startswith("{") or "=" in line  # skip JSON-only lines
    ]
    return "\n".join(code_lines).strip() or "print('No code generated')"
