"""Pre-execution code validation: check column references against manifest.

Deterministic, zero LLM cost. Runs after coder generates code, before sandbox
executes it. Catches column name typos and missing references early to reduce
wasted debugger iterations.
"""

from __future__ import annotations

import re

from ..core.types import Manifest, ManifestEntry


# Patterns that capture column name references in pandas-style code.
# IMPORTANT: capture group uses [^'"\n]+ (excludes newlines) to prevent
# multi-line string expressions from being matched and injected as broken
# comment lines (which would cause SyntaxError in the annotated code).
_COLUMN_PATTERNS = (
    # df["col"], df['col']
    re.compile(r"""(?:df|data|table|merged|filtered|result|joined)\[['"]([^'"\n]+)['"]\]"""),
    # .groupby("col"), .groupby(["col1", "col2"])
    re.compile(r"""\.groupby\(\[?['"]([^'"\n]+)['"]\]?\)"""),
    # .sort_values("col")
    re.compile(r"""\.sort_values\(\[?['"]([^'"\n]+)['"]\]?\)"""),
    # .drop_duplicates("col") or .drop_duplicates(subset=["col"])
    re.compile(r"""\.drop_duplicates\([^)]*['"]([^'"\n]+)['"][^)]*\)"""),
    # .merge(..., on="col") or on=["col"]
    re.compile(r"""\bon=['"]([^'"\n]+)['"]"""),
    # .rename(columns={"old": ...})
    re.compile(r"""\.rename\(columns=\{['"]([^'"\n]+)['"]"""),
)


def validate_column_references(
    code: str,
    manifest: Manifest,
) -> tuple[str, list[str], list[str]]:
    """Check column references in generated code against manifest columns.

    Phase 0.7 audit found 5/7 v80 "persistent struggle" failures had the
    Planner ignoring a close-match suggestion (e.g., task_199 used
    `CDSCode_str` while validator pointed at `CDSCode`). To break that
    pattern we now split warnings into:

    - **blocking_warnings**: high-confidence column typos where a close
      match exists. Orchestrator BLOCKs execution on these — Planner
      must use the suggested name (or rename a real column) before
      proceeding.
    - **soft warnings**: column not found and no close match (could be
      a legitimate Planner-created intermediate variable). Annotated
      into code as a comment only; execution allowed.

    Returns:
        (annotated_code, all_warnings, blocking_warnings)
    """
    referenced = extract_column_references(code)

    known_columns = _collect_all_columns(manifest)
    known_lower = {c.lower(): c for c in known_columns}

    warnings: list[str] = []
    blocking: list[str] = []
    for col in referenced:
        if col in known_columns:
            continue
        # Case-insensitive check — high confidence: same column, wrong case
        if col.lower() in known_lower:
            actual = known_lower[col.lower()]
            msg = (
                f"Column '{col}' not found exactly — did you mean '{actual}'? "
                "(case mismatch)"
            )
            warnings.append(msg)
            blocking.append(msg)
        else:
            close = _find_close_matches(col, known_columns)
            if close:
                msg = (
                    f"Column '{col}' not found in manifest — close matches: "
                    f"{close}. Use one of these (or explain why a new name is correct)."
                )
                warnings.append(msg)
                blocking.append(msg)
            else:
                # Soft warning — could be a legitimate intermediate variable.
                warnings.append(
                    f"Column '{col}' not found in any data source "
                    "(may be a Planner-created intermediate; verify)."
                )

    # B-fix 2: detect raw json.load / open(...).read() on JSON when the
    # helper safe_read_json_df is available but unused. Audit data
    # (docs/AUDIT_PLANNER_CAPABILITY.md): task_163/352/418 all hand-rolled
    # JSON parsing despite the helper being available, and all failed.
    helper_warning = _check_raw_json_load(code)
    if helper_warning:
        warnings.append(helper_warning)

    if not warnings:
        return code, [], []

    warning_block = "# === CODE VALIDATOR WARNINGS ===\n"
    for w in warnings:
        warning_block += f"# WARNING: {w}\n"
    warning_block += "# Verify column names before running. Use df.columns to check.\n"
    warning_block += "# ================================\n\n"

    return warning_block + code, warnings, blocking


# Patterns for raw JSON loading (Planner re-implementing what
# safe_read_json_df already does).
_RAW_JSON_LOAD_RE = re.compile(
    r"""(?:^|\s)(json\.load\s*\(|json\.loads\s*\(|"""
    r"""open\s*\([^)]*\.json[^)]*\)\.read\s*\()""",
    re.MULTILINE,
)
_HELPER_USED_RE = re.compile(r"\bsafe_read_json(?:_df)?\b")


def _check_raw_json_load(code: str) -> str | None:
    """If code uses raw json.load on a .json file but doesn't use
    safe_read_json / safe_read_json_df, suggest the helper.

    Returns a single warning string, or None if not applicable.
    """
    if _HELPER_USED_RE.search(code):
        return None  # helper already in use
    if not _RAW_JSON_LOAD_RE.search(code):
        return None
    return (
        "Raw json.load detected. Use `safe_read_json_df(filename)` from "
        "data_helpers — it auto-unwraps nested `{\"table\": ..., \"records\": [...]}` "
        "structures into a DataFrame. Example: "
        "`from data_helpers import safe_read_json_df; df = safe_read_json_df('context/json/foo.json')`."
    )


def extract_column_references(code: str) -> list[str]:
    """Extract column name references from pandas-style code."""
    columns: list[str] = []
    seen: set[str] = set()

    for pattern in _COLUMN_PATTERNS:
        for match in pattern.finditer(code):
            col = match.group(1)
            if col not in seen:
                seen.add(col)
                columns.append(col)

    return columns


def _collect_all_columns(manifest: Manifest) -> set[str]:
    """Collect all column names from all entries in the manifest."""
    columns: set[str] = set()
    for entry in manifest.entries:
        for col in _get_columns_from_entry(entry):
            name = col.get("name", "")
            if name:
                columns.add(name)
    return columns


def _get_columns_from_entry(entry: ManifestEntry) -> list[dict]:
    """Extract column dicts from any entry type."""
    s = entry.summary
    columns: list[dict] = []

    if "columns" in s:
        columns.extend(s["columns"])
    for table in s.get("tables", []):
        columns.extend(table.get("columns", []))
    for sheet in s.get("sheets", []):
        columns.extend(sheet.get("columns", []))

    return columns


def _find_close_matches(target: str, candidates: set[str], max_results: int = 3) -> list[str]:
    """Find columns with similar names (simple substring + prefix matching)."""
    target_lower = target.lower()
    matches: list[str] = []

    for c in sorted(candidates):
        c_lower = c.lower()
        # Substring match
        if target_lower in c_lower or c_lower in target_lower:
            matches.append(c)
        # Shared prefix of length >= 3
        elif len(target_lower) >= 3 and c_lower.startswith(target_lower[:3]):
            matches.append(c)

        if len(matches) >= max_results:
            break

    return matches
