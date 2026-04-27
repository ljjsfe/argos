"""Debugger agent: fix failed code using traceback + data context.

Receives question + QuestionSpec constraints so the fix stays aligned with
the goal (not just fixing the error in isolation).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.context_manager import ContextManager, Section
from ..core.llm_client import LLMClient

from ..core.types import AnalysisState, QuestionSpec, SandboxResult


def fix(
    failed_code: str,
    result: SandboxResult,
    manifest_json: str,
    data_profile: str,
    llm: LLMClient,
    *,
    state: AnalysisState | None = None,
    cm: ContextManager | None = None,
    retry_number: int = 0,
    previous_attempts: list[tuple[str, str]] | None = None,
    question: str = "",
    question_spec: QuestionSpec | None = None,
) -> str:
    """Fix failed code using error info and data context.

    If state + cm are provided, uses budget-managed context via ContextManager.
    If only state, uses structured context for data context (legacy).
    Otherwise falls back to legacy manifest_json + data_profile.
    """
    prompt_path = Path(__file__).parent.parent / "prompts" / "debugger.md"
    template = prompt_path.read_text(encoding="utf-8")

    error_type, error_message, full_traceback = _parse_error(result.stderr)

    if state is not None and cm is not None:
        sections = _build_sections(state, question_spec)
        data_context = cm.assemble(sections, llm=llm)
    else:
        data_context = f"Manifest:\n{manifest_json[:3000]}\n\nProfile:\n{data_profile[:2000]}"

    # Build question context for the template
    question_text = question or (state.question if state else "")
    question_context = ""
    if question_text:
        question_context = f"Question: {question_text}"
    if question_spec:
        spec_guidance = question_spec.to_guidance()
        if spec_guidance:
            question_context += f"\n\nAnswer constraints:\n{spec_guidance}"

    retry_context = ""
    if previous_attempts:
        parts = []
        for i, (prev_code_snippet, prev_error) in enumerate(previous_attempts):
            parts.append(
                f"### Attempt {i + 1}\n"
                f"Code tried:\n```python\n{prev_code_snippet}\n```\n"
                f"Result: {prev_error}"
            )
        retry_context = "\n\n".join(parts)

    system_prompt = (
        template
        .replace("{failed_code}", failed_code)
        .replace("{full_traceback}", full_traceback)
        .replace("{error_type}", error_type)
        .replace("{error_message}", error_message)
        .replace("{retry_context}", retry_context)
        .replace("{data_context}", data_context)
        .replace("{question_context}", question_context)
    )

    response = llm.chat(system_prompt, "Fix the code now.")
    return _extract_code(response)


def _build_sections(
    state: AnalysisState,
    question_spec: QuestionSpec | None = None,
) -> list[Section]:
    """Build prioritized sections for debugger data context."""
    sections: list[Section] = []

    sections.append(Section(
        "manifest", state.manifest_summary,
        priority=70, heading="## Data Sources",
    ))

    if state.domain_rules:
        sections.append(Section(
            "domain_rules", state.domain_rules,
            priority=75, heading="## Domain Rules",
        ))

    if state.data_profile_summary:
        sections.append(Section(
            "data_profile", state.data_profile_summary,
            priority=50, heading="## Data Profile",
        ))

    if question_spec:
        guidance = question_spec.to_guidance()
        if guidance:
            sections.append(Section(
                "question_constraints", guidance,
                priority=65, compressible=False,
                heading="## Answer Constraints (from question analysis)",
            ))

    return sections


def _parse_error(stderr: str) -> tuple[str, str, str]:
    """Extract error type, message, and truncated traceback from stderr."""
    lines = stderr.strip().split("\n")
    if not lines:
        return "UnknownError", "No error output", ""

    last_line = lines[-1].strip()

    # Extract error type and message from last line
    if ": " in last_line:
        parts = last_line.split(": ", 1)
        error_type, error_msg = parts[0], parts[1]
    else:
        error_type, error_msg = "RuntimeError", last_line

    # Keep last 500 chars of full traceback for context
    full_tb = stderr.strip()[-500:] if len(stderr.strip()) > 500 else stderr.strip()

    return error_type, error_msg, full_tb


def _extract_code(response: str) -> str:
    match = re.search(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return response.strip()
