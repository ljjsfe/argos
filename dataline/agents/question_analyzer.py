"""QuestionAnalyzer: infer answer shape before the main loop.

One LLM call to produce a QuestionSpec (answer_type, column_count, row_count,
value_style). Fed to HarnessGate (Rules 10-13) and PlannerCoder (guidance).

Design principles:
- Fail-open: any error returns all-unknown spec, never blocks the pipeline.
- notes field is NOT injected into downstream prompts (may contain wrong
  semantic interpretation). Only structural hints (to_guidance()) are used.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..core.types import QuestionSpec

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = (
    Path(__file__).parent.parent / "prompts" / "question_analyzer.md"
).read_text(encoding="utf-8")

_UNKNOWN_SPEC = QuestionSpec()


def analyze(
    question: str,
    manifest_summary: str,
    domain_rules: str,
    llm: Any,
) -> QuestionSpec:
    """Infer structural shape of the expected answer.

    Returns QuestionSpec. On any failure, returns all-unknown spec (fail-open).
    """
    if not question.strip():
        return _UNKNOWN_SPEC

    system_prompt = (
        _PROMPT_TEMPLATE
        .replace("{question}", question)
        .replace("{manifest_summary}", manifest_summary)
        .replace("{domain_rules}", domain_rules or "(no domain documentation)")
    )

    try:
        response = llm.chat(system_prompt, "Analyze the question shape now.")
    except Exception as e:
        logger.warning("QuestionAnalyzer LLM error: %s", e)
        return _UNKNOWN_SPEC

    return _parse_response(response)


def _parse_response(response: str) -> QuestionSpec:
    """Parse LLM response into QuestionSpec. Fail-open on any error."""
    try:
        raw_json = _extract_json(response)
        if not raw_json:
            logger.warning("QuestionAnalyzer: no JSON found in response")
            return _UNKNOWN_SPEC

        data = json.loads(raw_json)
        if not isinstance(data, dict):
            return _UNKNOWN_SPEC

        return QuestionSpec(
            answer_type=_validated_choice(
                data.get("answer_type", "unknown"),
                ("scalar", "list", "table", "unknown"),
                "unknown",
            ),
            expected_column_count=max(0, int(data.get("expected_column_count", 0))),
            expected_row_count=_validated_choice(
                data.get("expected_row_count", "unknown"),
                ("single", "multiple", "unknown"),
                "unknown",
            ),
            value_style=_validated_choice(
                data.get("value_style", "unknown"),
                ("numeric", "exact_term", "name", "mixed", "unknown"),
                "unknown",
            ),
            notes=str(data.get("notes", "")),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("QuestionAnalyzer parse error: %s", e)
        return _UNKNOWN_SPEC


def _validated_choice(value: str, choices: tuple[str, ...], default: str) -> str:
    """Return value if it's in choices, else default."""
    return value if value in choices else default


def _extract_json(text: str) -> str:
    """Extract JSON object from response, handling markdown fences."""
    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return ""
