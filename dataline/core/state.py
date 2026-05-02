"""AnalysisState management: create, update, render for each agent role.

All functions are pure — they return new AnalysisState instances (immutable).

Information priority (high → low):
- Domain rules, formulas, definitions → full content
- Column statistics, distributions → full content (API has 200K context)
- Raw data samples → brief (like LIMIT 10)
- Recent step outputs → full content
- Older step outputs → 1-line summary (already in completed_steps)
"""

from __future__ import annotations

from dataclasses import replace

from ..core.types import AnalysisState, Manifest, StepRecord


def create_initial_state(
    task_id: str,
    question: str,
    manifest: Manifest,
    data_profile: str,
    domain_rules: str = "",
) -> AnalysisState:
    """Initialize state from profiler + analyzer outputs."""
    return AnalysisState(
        task_id=task_id,
        question=question,
        manifest_summary=compress_manifest(manifest),
        data_profile_summary=data_profile,
        domain_rules=domain_rules,
        question_analysis="",
        key_findings=(),
        variables_in_scope=(),
        judge_guidance="",
        completed_steps=(),
        full_step_details=(),
    )


def set_question_analysis(state: AnalysisState, analysis: str) -> AnalysisState:
    """Return new state with question_analysis set (called once after QuestionAnalyzer)."""
    return replace(state, question_analysis=analysis)


def set_task_mode(state: AnalysisState, task_mode: str) -> AnalysisState:
    """Return new state with task_mode set (called once after routing)."""
    return replace(state, task_mode=task_mode)


def compress_manifest(manifest: Manifest) -> str:
    """Compress manifest to rich schema context.

    Outputs per-table/file:
    - Column list with dtype, cardinality, null%
    - Sample rows (up to 3)
    - DISTINCT values for low-cardinality text columns
    - .md/doc files omitted (already in domain_rules channel)
    """
    parts: list[str] = []

    for entry in manifest.entries:
        s = entry.summary

        # Skip documentation files — content flows via domain_rules channel
        if entry.file_type in ("markdown", "pdf", "docx", "image"):
            parts.append(f"[doc] {entry.file_path} ({entry.file_type})")
            continue

        # Flat columns (CSV, JSON, Parquet)
        if "columns" in s:
            parts.append(_format_flat_table(
                entry.file_path, s.get("row_count", "?"),
                s["columns"], s.get("sample_rows", []),
            ))

        # SQLite tables
        elif "tables" in s:
            for table in s["tables"]:
                label = f"{entry.file_path}/{table.get('name', '?')}"
                parts.append(_format_flat_table(
                    label, table.get("row_count", "?"),
                    table.get("columns", []), table.get("sample_rows", []),
                ))
            # Foreign keys
            for fk in s.get("foreign_keys", []):
                parts.append(
                    f"FK: {fk['from_table']}.{fk['from_col']} -> "
                    f"{fk['to_table']}.{fk['to_col']}"
                )

        # Excel sheets
        elif "sheets" in s:
            for sheet in s["sheets"]:
                label = f"{entry.file_path}/{sheet.get('name', '?')}"
                parts.append(_format_flat_table(
                    label, sheet.get("row_count", "?"),
                    sheet.get("columns", []), sheet.get("sample_rows", []),
                ))

        # Other/error
        else:
            error = s.get("error", "")
            if error:
                parts.append(f"{entry.file_path}: ERROR {error}")
            else:
                parts.append(f"{entry.file_path} ({entry.file_type})")

    # Cross-source relations
    for rel in manifest.cross_source_relations:
        parts.append(
            f"RELATION: {rel.source_a} <-> {rel.source_b}: "
            f"{rel.relation} (conf={rel.confidence})"
        )

    return "\n\n".join(parts)


def _format_flat_table(
    label: str,
    row_count: object,
    columns: list[dict],
    sample_rows: list[dict],
) -> str:
    """Format a single table/file with rich schema context."""
    lines: list[str] = [f"### {label} [{row_count} rows]"]

    # Column list: name(dtype, N unique) or name(dtype, null=X%)
    col_parts: list[str] = []
    for c in columns:
        name = c.get("name", "?")
        dtype = c.get("dtype", "?")
        card = c.get("cardinality")
        null_pct = c.get("null_pct", 0)

        parts_inner = [f"{name}({dtype}"]
        if card is not None:
            parts_inner.append(f", {card} unique")
        if null_pct and null_pct > 0.01:
            parts_inner.append(f", null={round(null_pct * 100)}%")
        col_parts.append("".join(parts_inner) + ")")

    lines.append("Columns: " + ", ".join(col_parts))

    # Sample rows (up to 3)
    if sample_rows:
        lines.append("Sample rows:")
        for row in sample_rows[:3]:
            row_str = " | ".join(
                f"{k}={_compact_val(v)}" for k, v in list(row.items())[:8]
            )
            lines.append(f"  {row_str}")

    # DISTINCT values for low-cardinality text columns
    for c in columns:
        dv = c.get("distinct_values")
        if dv:
            name = c.get("name", "?")
            vals_str = ", ".join(f"'{v}'" for v in dv[:30])
            lines.append(f"DISTINCT {name} ({len(dv)}): {vals_str}")

    # Semi-structured / type-anomaly flags worth surfacing to the planner.
    # Lets the agent reach for parse_jsonish_column or coerce_numeric_id_columns
    # instead of treating dict-strings as opaque text.
    flag_callouts = {
        "dict_like_string": (
            "holds stringified dict/list — use parse_jsonish_column / "
            "explode_jsonish_column"
        ),
        "mixed_type": "mixed Python types in cells — coerce before joining",
    }
    for c in columns:
        flags = c.get("flags") or []
        for flag in flags:
            note = flag_callouts.get(flag)
            if note:
                lines.append(f"FLAG {c.get('name', '?')}: {note}")

    return "\n".join(lines)


def _compact_val(v: object) -> str:
    """Compact representation of a sample value."""
    if v is None:
        return "NULL"
    s = str(v)
    if len(s) > 60:
        return s[:57] + "..."
    return s


def add_step(
    state: AnalysisState,
    step: StepRecord,
    finding_summary: str,
) -> AnalysisState:
    """Return new state with step appended. Stdout compressed to 1-line finding.

    Args:
        state: Current immutable state.
        step: The completed step record (with full stdout).
        finding_summary: 1-line summary of what this step discovered.
    """
    step_line = f"Step {step.step_index}: {step.plan.step_description} → {finding_summary}"

    # Detect variables saved to disk (pickle files)
    new_vars = state.variables_in_scope
    if "pickle.dump" in step.code or ".to_pickle" in step.code:
        import re
        pkl_matches = re.findall(r'["\']([^"\']+\.pkl)["\']', step.code)
        for pkl in pkl_matches:
            if not any(v[0] == pkl for v in new_vars):
                new_vars = new_vars + ((pkl, step.plan.step_description),)

    return replace(
        state,
        key_findings=state.key_findings + (finding_summary,),
        variables_in_scope=new_vars,
        completed_steps=state.completed_steps + (step_line,),
        full_step_details=state.full_step_details + (step,),
    )


def update_judge_guidance(state: AnalysisState, guidance: str) -> AnalysisState:
    """Return new state with guidance appended.

    Maintains a rolling log of the last 3 guidance records so the model can
    see what was already tried. Each record is separated by a divider.
    Empty guidance clears the log (used on backtrack).
    """
    if not guidance or not guidance.strip():
        return replace(state, judge_guidance="")

    # Parse existing records
    separator = "\n---\n"
    existing = state.judge_guidance.strip()
    if existing:
        records = [r.strip() for r in existing.split(separator) if r.strip()]
    else:
        records = []

    records.append(guidance.strip())

    # Cap at last 3 records
    records = records[-3:]

    return replace(state, judge_guidance=separator.join(records))


def update_repl_state_summary(state: AnalysisState, summary: str) -> AnalysisState:
    """Set the REPL globals summary for next PlannerCoder invocation."""
    return replace(state, repl_state_summary=summary)


def update_harness_feedback(state: AnalysisState, feedback: str) -> AnalysisState:
    """Return new state with updated harness feedback."""
    return replace(state, harness_feedback=feedback)


def truncate_to_step(state: AnalysisState, step_index: int) -> AnalysisState:
    """Backtrack: truncate state to given step index.

    Clears stale guidance/feedback to prevent misleading the next iteration.
    Keeps variables_in_scope (pickles still on disk).
    """
    return replace(
        state,
        key_findings=state.key_findings[:step_index],
        completed_steps=state.completed_steps[:step_index],
        full_step_details=state.full_step_details[:step_index],
        judge_guidance="",
        harness_feedback="",
    )


def summarize_step_output(stdout: str, max_len: int = 100) -> str:
    """Create a 1-line summary from step stdout. Deterministic extraction."""
    if not stdout or not stdout.strip():
        return "no output"

    lines = [line.strip() for line in stdout.strip().split("\n") if line.strip()]
    if not lines:
        return "empty output"

    first_line = lines[0]
    if len(first_line) <= max_len:
        return first_line
    return first_line[:max_len] + "..."
