"""Finalizer agent: format accumulated results into answer table."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

from ..core.context_manager import ContextManager, Section
from ..core.llm_client import LLMClient

from ..core.token_estimator import cap_text
from ..core.types import AnalysisState, QuestionSpec, StepRecord

def format_answer(
    question: str,
    steps_done: list[StepRecord],
    llm: LLMClient,
    *,
    state: AnalysisState | None = None,
    question_spec: QuestionSpec | None = None,
    cm: ContextManager | None = None,
    benchmark: str = "kdd",
    guidelines: str = "",
) -> dict:
    """Format step results into final answer dict.

    If benchmark == "dabstep", uses a scalar-answer prompt and returns {"answer": [val]}.
    If benchmark == "kdd" (default), uses table-format prompt and returns {"col": [values]}.
    If state + cm are provided, uses budget-managed context via ContextManager.
    """
    if benchmark == "dabstep":
        return _format_dabstep(question, steps_done, llm, state=state, cm=cm, guidelines=guidelines)
    return _format_kdd(question, steps_done, llm, state=state, cm=cm, question_spec=question_spec)


def _format_kdd(
    question: str,
    steps_done: list[StepRecord],
    llm: LLMClient,
    *,
    state: AnalysisState | None = None,
    cm: ContextManager | None = None,
    question_spec: QuestionSpec | None = None,
) -> dict:
    """KDD table-format answer.

    Path 1 (structured): last step wrote save_result() → read answer directly.
                         No LLM, no text parsing, exact column names preserved.
    Path 2 (stdout JSON): last step printed a JSON dict → extract directly.
    Path 3 (LLM):         fall back to LLM formatting with column-structure hint.
    """
    # --- Path 1: structured output from save_result() ---
    structured = _try_structured_extract(state)
    if structured is not None:
        return _trim_extra_columns(structured, question_spec, question)

    # --- Path 2: LLM formatting ---
    prompt_path = Path(__file__).parent.parent / "prompts" / "finalizer.md"
    template = prompt_path.read_text(encoding="utf-8")

    if state is not None and cm is not None:
        sections = _build_sections(state)
        context = cm.assemble(sections, llm=llm)
        system_prompt = (
            template
            .replace("{question}", state.question)
            .replace("{steps_summary}", context)
        )
    else:
        steps_summary = _format_steps(steps_done)
        system_prompt = (
            template
            .replace("{question}", question)
            .replace("{steps_summary}", steps_summary)
        )

    response = llm.chat(system_prompt, "Format the final answer now.")

    try:
        data = json.loads(_extract_json(response))
        if "columns" in data:
            return data["columns"]
        return data
    except (json.JSONDecodeError, ValueError):
        return _fallback_extract(steps_done, state)


def _format_dabstep(
    question: str,
    steps_done: list[StepRecord],
    llm: LLMClient,
    *,
    state: AnalysisState | None = None,
    cm: ContextManager | None = None,
    guidelines: str = "",
) -> dict:
    """DABstep scalar-answer format.

    Fast path: if the last successful step's stdout contains a single clear
    scalar value (number, short string), extract it directly.
    """
    # --- Fast path: try direct scalar extraction ---
    direct_scalar = _try_direct_scalar_extract(steps_done, state)
    if direct_scalar is not None:
        return {"answer": [direct_scalar]}

    # --- Slow path: LLM formatting ---
    prompt_path = Path(__file__).parent.parent / "prompts" / "finalizer_dabstep.md"
    template = prompt_path.read_text(encoding="utf-8")

    if state is not None and cm is not None:
        sections = _build_sections(state)
        context = cm.assemble(sections, llm=llm)
        system_prompt = (
            template
            .replace("{question}", state.question)
            .replace("{guidelines}", guidelines)
            .replace("{steps_summary}", context)
        )
    else:
        steps_summary = _format_steps(steps_done)
        system_prompt = (
            template
            .replace("{question}", question)
            .replace("{guidelines}", guidelines)
            .replace("{steps_summary}", steps_summary)
        )

    response = llm.chat(system_prompt, "Format the final answer now.")

    try:
        data = json.loads(_extract_json(response))
        scalar_val = data.get("answer", "")
        return {"answer": [scalar_val]}
    except (json.JSONDecodeError, ValueError):
        # Try to use raw response as scalar answer
        cleaned = response.strip()
        if cleaned:
            return {"answer": [cleaned]}
        return _fallback_extract(steps_done, state)


def _last_successful_stdout(
    steps_done: list[StepRecord],
    state: AnalysisState | None,
) -> str | None:
    """Return stdout from the last step with return_code == 0, or None."""
    details = state.full_step_details if state is not None else tuple(steps_done)
    for step in reversed(details):
        if step.result.return_code == 0 and step.result.stdout.strip():
            return step.result.stdout.strip()
    return None


def _last_successful_step(state: AnalysisState) -> "StepRecord":
    """Return the last StepRecord with rc==0 and non-trivial stdout.

    Falls back to the last step if none qualify (e.g. all steps failed).
    Used for column-structure detection — the last *successful* step holds the
    answer data, which may differ from the chronologically last step (which
    could be a verification/count step with minimal output).
    """
    for step in reversed(state.full_step_details):
        if step.result.return_code == 0 and step.result.stdout and step.result.stdout.strip():
            return step
    return state.full_step_details[-1]


def _try_structured_extract(state: AnalysisState | None) -> dict | None:
    """Path 1: read answer from the last step's save_result() structured output.

    Returns a non-empty column dict, or None if not available.
    This path requires NO text parsing — column names and values come directly
    from the code that computed them.
    """
    if state is None or not state.full_step_details:
        return None

    # Walk backwards to find the last step that wrote a structured result
    for step in reversed(state.full_step_details):
        if not step.result.structured_json:
            continue
        try:
            data = json.loads(step.result.structured_json)
        except (json.JSONDecodeError, ValueError):
            continue
        answer = data.get("answer", {})
        if not isinstance(answer, dict) or not answer:
            continue
        # Drop metadata-only keys that some models add (e.g. "summary", "metadata",
        # "explanation", "notes") — these are never real data columns.
        _METADATA_KEYS = {"summary", "metadata", "explanation", "notes", "description"}
        answer = {k: v for k, v in answer.items() if k.lower() not in _METADATA_KEYS}
        if not answer:
            continue
        # Normalize: scalar values → single-element lists so downstream is uniform
        answer = {
            k: v if isinstance(v, list) else [v]
            for k, v in answer.items()
        }
        # Validate: all values must be lists of the same length
        lengths = {len(v) for v in answer.values()}
        if len(lengths) != 1:
            continue

        # Stdout leak guard: reject if any value contains newlines or is very long
        # (indicates save_result() was called with raw stdout instead of structured data)
        if _has_stdout_leak(answer):
            logger.warning(
                "Structured extract rejected for step %d: "
                "answer values contain newlines or are >300 chars (stdout leak). "
                "Trying earlier step.",
                step.step_index,
            )
            continue

        return answer
    return None


def _has_stdout_leak(answer: dict) -> bool:
    """Detect if answer values contain leaked stdout (multi-line debug strings).

    A legitimate answer value is a short scalar or a compact identifier.
    If a value contains newlines or exceeds 300 characters, it was almost
    certainly captured from raw stdout rather than a structured computation.
    """
    for vals in answer.values():
        if not isinstance(vals, list):
            continue
        for v in vals:
            if isinstance(v, str) and ("\n" in v or len(v) > 300):
                return True
    return False



_ID_SUFFIX_RE = re.compile(r"(?i)^.+[_]?id$|^index$|^rowid$|^row_id$|^row_number$")


def _trim_extra_columns(
    answer: dict,
    spec: QuestionSpec | None,
    question: str = "",
) -> dict:
    """Drop extra join-key/ID columns when QuestionSpec expects fewer columns.

    Conservative strategy: only drops columns whose name matches an ID pattern
    (ends with _id/ID, or is "index"/"rowid") AND whose name does NOT appear
    as a keyword in the question. This avoids false positives where the question
    actually asks for an ID column (e.g. "List the transaction IDs").

    Fail-open: returns original answer if not enough ID columns can be identified.
    """
    if spec is None or spec.expected_column_count <= 0:
        return answer
    expected = spec.expected_column_count
    actual = len(answer)
    if actual <= expected:
        return answer

    q_lower = question.lower()
    to_drop = actual - expected

    # Identify droppable columns: ID-like name + NOT mentioned in question
    droppable: list[str] = []
    for col_name in answer:
        if not _ID_SUFFIX_RE.match(col_name):
            continue
        # Check if column name (or its stem) appears in the question
        col_stem = col_name.lower().replace("_id", "").replace("id", "").strip("_")
        if col_stem and len(col_stem) >= 3 and col_stem in q_lower:
            continue  # Question mentions this entity — keep it
        droppable.append(col_name)

    if len(droppable) < to_drop:
        return answer

    # Drop the first `to_drop` ID columns (order preserves original dict order)
    drop_set = set(droppable[:to_drop])
    trimmed = {k: v for k, v in answer.items() if k not in drop_set}
    logger.info(
        "Column trim: dropped %s (spec expected %d cols, had %d)",
        drop_set, expected, actual,
    )
    return trimmed


def _try_direct_scalar_extract(
    steps_done: list[StepRecord],
    state: AnalysisState | None,
) -> str | None:
    """Try to extract a scalar answer directly from the last successful stdout.

    Returns the scalar value as a string, or None if LLM formatting is needed.

    Only fires on unambiguous JSON {"answer": ...} — never guesses from free text.
    """
    stdout = _last_successful_stdout(steps_done, state)
    if stdout is None:
        return None

    for candidate in _json_candidates(stdout):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and "answer" in data:
            return str(data["answer"])

    return None


def _json_candidates(stdout: str) -> list[str]:
    """Yield JSON candidate strings from stdout: whole text, then last non-empty line."""
    candidates = [stdout]
    lines = [ln for ln in stdout.split("\n") if ln.strip()]
    if lines and lines[-1].strip() != stdout:
        candidates.append(lines[-1].strip())
    return candidates


def _build_sections(state: AnalysisState) -> list[Section]:
    """Build prioritized sections for finalizer context.

    Excludes question (already in template {question}).
    """
    sections: list[Section] = []

    if state.domain_rules:
        sections.append(Section(
            "domain_rules", state.domain_rules,
            priority=80, heading="## Domain Rules",
        ))

    if state.question_analysis:
        sections.append(Section(
            "question_analysis", state.question_analysis,
            priority=72, compressible=True,
            heading="## Question Analysis (expected output types — verify column structure)",
        ))

    # Step results: split into earlier steps (compressible) and last step (non-compressible).
    # The last step's output contains the final answer — must not be compressed.
    if state.full_step_details:
        if len(state.full_step_details) > 1:
            earlier_parts = []
            for s in state.full_step_details[:-1]:
                stdout = cap_text(s.result.stdout) if s.result.stdout else "(no output)"
                earlier_parts.append(
                    f"Step {s.step_index}: {s.plan.step_description}\n"
                    f"  Output:\n{stdout}"
                )
            sections.append(Section(
                "earlier_step_results", "\n\n".join(earlier_parts),
                priority=75, heading="## Earlier Step Results",
            ))

        last = state.full_step_details[-1]
        last_stdout = cap_text(last.result.stdout) if last.result.stdout else "(no output)"
        sections.append(Section(
            "last_step_result",
            f"Step {last.step_index}: {last.plan.step_description}\n"
            f"  Output:\n{last_stdout}",
            priority=95, compressible=False,
            heading="## Latest Step Result (primary answer source)",
        ))

        # Column hint: prefer save_result, then walk backwards to the last step
        # with successful output (rc==0, non-empty stdout).  Using [-1] directly
        # is wrong when the last step is a verification/count step with trivial output.
        answer_step = _last_successful_step(state)
        struct_cols = _structured_column_names(answer_step.result.structured_json)
        stdout_cols = struct_cols or _extract_stdout_columns(answer_step.result.stdout or "")
        if stdout_cols:
            col_list = ", ".join(stdout_cols)
            source = "save_result()" if struct_cols else "stdout"
            # Reference the actual step so the LLM knows where to look,
            # especially when answer_step differs from the last step.
            step_ref = (
                f"Step {answer_step.step_index}"
                if answer_step.step_index != last.step_index
                else "the latest step"
            )
            sections.append(Section(
                "required_column_structure",
                f"Columns from {step_ref} ({source}): {col_list}\n"
                f"Output MUST contain EXACTLY these {len(stdout_cols)} columns — no more, no fewer. "
                f"Do NOT add extra columns, explanation columns, index columns, or metadata columns. "
                f"Do NOT merge or rename any of them.\n"
                f"**Every extra column beyond this list reduces your score.**",
                priority=98, compressible=False,
                heading="## Required Column Structure (EXACTLY these columns, no extras)",
            ))

    if state.key_findings:
        sections.append(Section(
            "key_findings",
            "\n".join(f"- {f}" for f in state.key_findings),
            priority=70, heading="## Key Findings",
        ))

    return sections


def _structured_column_names(structured_json: str) -> list[str]:
    """Extract column names from save_result() structured output.

    Returns column keys from the answer dict, or empty list if not available.
    These names are authoritative — they came directly from the code.
    """
    if not structured_json:
        return []
    try:
        data = json.loads(structured_json)
        answer = data.get("answer", {})
        if isinstance(answer, dict) and answer:
            return list(answer.keys())
    except (json.JSONDecodeError, ValueError):
        pass
    return []



def _extract_stdout_columns(stdout: str) -> list[str]:
    """Extract column names from the last step's stdout.

    Tries three formats in order:
    1. JSON dict-of-lists: {"col": [...], ...}
    2. Pandas DataFrame header: aligned text table with integer index column
    3. CSV header: comma-separated first line

    Returns empty list if no unambiguous column structure is found.
    Column names come from the data — no hardcoding or pattern matching.
    """
    if not stdout:
        return []

    # 1. JSON: keys of a column dict
    for candidate in _json_candidates(stdout):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data:
            if "columns" in data and isinstance(data["columns"], dict):
                return list(data["columns"].keys())
            if all(isinstance(v, list) for v in data.values()):
                return list(data.keys())

    # 2. DataFrame printout: header line precedes a line starting with an integer index.
    # The integer-index pattern is a strong guard, so allow single-column results too.
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    for i in range(len(lines) - 1):
        next_ln = lines[i + 1]
        if re.match(r'^\s*\d+\s{2,}', next_ln):
            # lines[i] is the header
            cols = re.split(r'\s{2,}', lines[i].strip())
            cols = [c.strip() for c in cols if c.strip()]
            if len(cols) >= 1:
                return cols

    # 3. CSV header: first line with 2+ comma-separated tokens
    first = lines[0] if lines else ""
    if "," in first and not first.startswith("{"):
        cols = [c.strip() for c in first.split(",") if c.strip()]
        if len(cols) >= 2:
            return cols

    return []


def _format_steps(steps: list[StepRecord]) -> str:
    parts = []
    for s in steps:
        stdout = s.result.stdout[:1500] if s.result.stdout else "(no output)"
        parts.append(
            f"Step {s.step_index}: {s.plan.step_description}\n"
            f"  Output:\n{stdout}"
        )
    return "\n\n".join(parts) if parts else "No steps."


def _extract_json(text: str) -> str:
    match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return text


def _fallback_extract(
    steps_done: list[StepRecord],
    state: AnalysisState | None = None,
) -> dict:
    """Try to extract answer from last step's stdout."""
    details = state.full_step_details if state is not None else tuple(steps_done)
    if not details:
        return {}
    last_stdout = details[-1].result.stdout
    try:
        return json.loads(last_stdout)
    except (json.JSONDecodeError, ValueError):
        pass
    return {"answer": [last_stdout.strip()]}
