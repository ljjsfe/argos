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

# Task mode hints — injected by deterministic router in orchestrator
_TASK_MODE_HINTS = {
    "single_sql": (
        "Task mode: single_sql\n"
        "All data is structured. Use a single SQL query."
    ),
    "multi_sql": (
        "Task mode: multi_sql\n"
        "Multiple structured tables with join candidates. "
        "Use SQL, validate join keys match the schema, avoid row explosion."
    ),
    "python_extract": (
        "Task mode: python_extract\n"
        "Data includes unstructured files (documents/PDFs). "
        "Use Python to extract relevant data first, then analyze."
    ),
    "document_needed": (
        "Task mode: document_needed\n"
        "Domain knowledge documents are available. "
        "Reference domain rules when interpreting column values or thresholds."
    ),
}


@dataclass(frozen=True)
class PlannerCoderOutput:
    """Output from the unified PlannerCoder agent."""
    plan: PlanStep
    candidates: tuple[str, ...]  # ordered code candidates (SQL or Python)
    language: str  # "sql" | "python" — primary language chosen
    reasoning: str = ""
    parse_status: str = "ok"  # "ok" | "fence_missing_recovered" | "fence_missing_failed"


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
    iteration: int = 0,
    max_iterations: int = 8,
) -> PlannerCoderOutput:
    """Generate plan + code candidates in a single LLM call.

    Uses ContextManager to assemble full context within token budget.
    Returns PlannerCoderOutput with plan and ordered candidates.
    """
    if state and cm:
        prompt = _build_context_managed_prompt(
            state, cm, llm,
            iteration=iteration, max_iterations=max_iterations,
        )
    else:
        prompt = _build_legacy_prompt(question, manifest_json, data_profile, steps_done)

    response = llm.chat(
        system=_PROMPT_TEMPLATE,
        user=prompt,
    )

    prior_plan = steps_done[-1].plan if steps_done else None
    return _parse_response(response, prior_plan=prior_plan)


def _build_context_managed_prompt(
    state: AnalysisState,
    cm: ContextManager,
    llm: Any,
    *,
    iteration: int = 0,
    max_iterations: int = 8,
) -> str:
    """Build budget-managed context — clean and focused.

    Only includes: question, schema, domain rules, harness feedback, prior steps.
    No redundant data_profile, QA guidance, or question_analysis sections.
    """
    sections = []

    # Question — highest priority, never compress
    sections.append(Section(
        name="question",
        content=f"## Question\n{state.question}",
        priority=100,
        compressible=False,
        heading="",
    ))

    # Harness feedback — deterministic block/warn signals
    if state.harness_feedback:
        sections.append(Section(
            name="harness_feedback",
            content=(
                f"## HarnessGate Feedback (FIX THESE)\n"
                f"{state.harness_feedback}"
            ),
            priority=96,
            compressible=False,
            heading="",
        ))

    # Judge guidance — semantic steering from prior iteration
    if state.judge_guidance:
        sections.append(Section(
            name="judge_guidance",
            content=(
                f"## Judge Guidance (FOLLOW THIS)\n"
                f"{state.judge_guidance}"
            ),
            priority=94,
            compressible=False,
            heading="",
        ))

    # Task mode hint — deterministic routing
    if state.task_mode:
        hint = _TASK_MODE_HINTS.get(state.task_mode, "")
        if hint:
            sections.append(Section(
                name="task_mode",
                content=f"## Task Mode\n{hint}",
                priority=92,
                compressible=False,
                heading="",
            ))

    # Data manifest (rich schema with DISTINCT values, sample rows)
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

    # Last iteration warning
    if iteration >= max_iterations - 1:
        sections.append(Section(
            name="budget",
            content="## LAST ITERATION — output your best answer now.",
            priority=98,
            compressible=False,
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


def _parse_response(
    response: str,
    prior_plan: PlanStep | None = None,
) -> PlannerCoderOutput:
    """Parse LLM response into PlannerCoderOutput.

    Expected format:
    1. JSON block with plan + language + reasoning
    2. One or more code blocks (```sql or ```python)

    When the JSON plan block is missing or unparseable, recover by:
      - Inferring language from the first code candidate (SQL vs Python).
      - Reusing prior_plan.step_description so downstream sees a real plan
        instead of the placeholder "Execute analysis step".

    The recovery path is reported via PlannerCoderOutput.parse_status so
    upstream observers (orchestrator trace, eval) can detect format failures.
    """
    plan_data: dict = {}
    parse_status = "ok"

    json_match = re.search(r"```json\s*(\{.*?\})\s*```", response, re.DOTALL)
    if json_match:
        try:
            plan_data = json.loads(json_match.group(1))
        except json.JSONDecodeError:
            plan_data = {}

    if not plan_data:
        plan_data = _extract_inline_json(response)

    json_missing = not plan_data

    candidates = _extract_code_candidates(response)
    code_blocks_present = bool(candidates)
    if not candidates:
        candidates = (_clean_response_as_code(response),)

    # Build PlanStep — fall back to prior plan when the LLM omitted JSON.
    fallback_desc = (
        prior_plan.step_description
        if (json_missing and prior_plan and prior_plan.step_description)
        else "Execute analysis step"
    )
    step_description = plan_data.get(
        "plan",
        plan_data.get("step_description", fallback_desc),
    )

    plan = PlanStep(
        step_description=step_description,
        data_sources=tuple(plan_data.get("data_sources", [])),
        depends_on_prior=plan_data.get("depends_on_prior", False),
        expected_output=plan_data.get("expected_output", ""),
    )

    # Language: when JSON is missing, infer from the first candidate instead
    # of silently defaulting to "python" (the long-standing bug that masked
    # SQL-capable LLM outputs as broken Python attempts).
    if "language" in plan_data:
        language = plan_data["language"]
    elif json_missing:
        language = _infer_language_from_candidate(candidates[0] if candidates else "")
        parse_status = "fence_missing_recovered"
    else:
        language = "python"

    reasoning = plan_data.get("reasoning", "")

    if json_missing and not code_blocks_present:
        parse_status = "fence_missing_failed"

    return PlannerCoderOutput(
        plan=plan,
        candidates=candidates,
        language=language,
        reasoning=reasoning,
        parse_status=parse_status,
    )


def _infer_language_from_candidate(code: str) -> str:
    """Infer language when JSON plan is missing.

    Conservative SQL detection: starts with SELECT/WITH/CREATE and contains
    no Python-only keywords. Anything else falls back to python.
    """
    stripped = code.strip()
    if not stripped:
        return "python"
    first_word = stripped.split()[0].upper()
    sql_starters = ("SELECT", "WITH", "CREATE", "ATTACH", "PRAGMA", "EXPLAIN")
    has_python_keywords = any(
        kw in code for kw in ("import ", "def ", "print(", "from ", "class ")
    )
    if first_word in sql_starters and not has_python_keywords:
        return "sql"
    return "python"


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
