"""Analyzer agent: deep data profiling via code execution.

Returns two separate outputs:
- data_profile: column statistics, distributions, sample rows
- domain_rules: extracted from documentation files (manual, README, knowledge)
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from ..core.llm_client import LLMClient
from ..core.sandbox import Sandbox
from ..core.token_estimator import estimate_tokens
from ..core.types import Manifest
from ..profiler.manifest import manifest_to_json

logger = logging.getLogger(__name__)

# Compilation threshold: compile when domain rules exceed this fraction
# of the CM budget. At 30% of 175K budget ≈ 52K tokens (~200K chars),
# this only fires for truly large docs that would risk token overflow.
# Tune based on eval results.
_COMPILE_BUDGET_FRACTION = 0.30

# Chunk size for extremely large docs (in chars).
# Each chunk must fit within the LLM's context window for compilation.
# ~80K chars ≈ ~20K tokens, leaving room for prompt + output.
_CHUNK_MAX_CHARS = 80_000


def analyze(manifest: Manifest, llm: LLMClient, sandbox: Sandbox) -> tuple[str, str]:
    """Generate and execute profiling code, return (data_profile, domain_rules).

    Strategy:
    1. Extract domain rules from documentation files (deterministic, no LLM)
    2. LLM generates profiling code for structured data → execute
    3. If execution fails, retry with a simpler prompt
    4. If all retries fail, use deterministic fallback (manifest summary)
    5. Always append deterministic value distributions section (P1b)
    """
    # Step 1: Extract domain rules from documentation files (independent channel)
    domain_rules = _extract_domain_rules(manifest)

    # Step 2: Profile structured data
    manifest_json = manifest_to_json(manifest)

    profile = _attempt_llm_profile(manifest_json, llm, sandbox)
    if not profile:
        profile = _attempt_simple_profile(manifest_json, llm, sandbox)
    if not profile:
        profile = _deterministic_fallback(manifest_json)

    # Step 3: Always append deterministic value distributions (zero LLM cost).
    # This guarantees PlannerCoder sees exact filter values even if LLM profiling
    # code was incomplete or missed columns.
    value_dists = _deterministic_value_distributions(manifest)
    if value_dists:
        profile = profile + "\n\n" + value_dists

    return profile, domain_rules


def compile_domain_rules(
    raw_rules: str,
    llm: LLMClient,
    budget_tokens: int,
) -> str:
    """Compile domain rules if they exceed a fraction of the token budget.

    Layer 1 compilation: recall-priority, question-agnostic.
    Extracts ALL rules, formulas, definitions, and constraints into a
    structured format that preserves exact quotes while reducing token count.

    Args:
        raw_rules: Full text of domain documentation files.
        llm: LLM client for compilation.
        budget_tokens: Total token budget from ContextManager.

    Returns:
        Compiled rules if compilation was needed and succeeded,
        otherwise the original raw_rules unchanged.
    """
    if not raw_rules or not raw_rules.strip():
        return raw_rules

    rules_tokens = estimate_tokens(raw_rules)
    threshold = int(budget_tokens * _COMPILE_BUDGET_FRACTION)

    if rules_tokens <= threshold:
        logger.debug(
            "Domain rules fit within budget: %d tokens ≤ %d threshold",
            rules_tokens, threshold,
        )
        return raw_rules

    # Skip compilation if the content looks like data (lookup tables, CSV-like rows).
    # Compiling data tables risks dropping actual values the agents need.
    if _looks_like_data(raw_rules):
        logger.info(
            "Domain rules contain data tables — skipping compilation to preserve values."
        )
        return raw_rules

    logger.info(
        "Domain rules exceed budget fraction: %d tokens > %d threshold. Compiling...",
        rules_tokens, threshold,
    )

    # For docs that fit in a single LLM call, compile directly
    if len(raw_rules) <= _CHUNK_MAX_CHARS:
        compiled = _compile_single(raw_rules, llm)
        if compiled:
            compiled_tokens = estimate_tokens(compiled)
            logger.info(
                "Compiled domain rules: %d → %d tokens (%.0f%% reduction)",
                rules_tokens, compiled_tokens,
                (1 - compiled_tokens / rules_tokens) * 100,
            )
            return compiled
        logger.warning("Domain rules compilation failed, returning raw text")
        return raw_rules

    # Chunked compilation for extremely large docs
    return _compile_chunked(raw_rules, llm, rules_tokens)


def _looks_like_data(text: str) -> bool:
    """Heuristic: return True if text contains significant data tables or CSV-like content.

    Triggers when the content has many pipe-delimited rows (markdown tables),
    dense numeric lines, or a high ratio of numbers to text — signals that
    the document is a data file, not a pure rule/formula document.
    """
    lines = text.splitlines()
    if not lines:
        return False

    pipe_lines = sum(1 for ln in lines if ln.count("|") >= 2)
    numeric_lines = sum(1 for ln in lines if re.search(r"\b\d+\.?\d*\b", ln))

    # >15% pipe-delimited lines → markdown data table
    if pipe_lines / len(lines) > 0.15:
        return True

    # >60% lines contain numbers → data-heavy document
    if numeric_lines / len(lines) > 0.60:
        return True

    return False


def _compile_single(raw_rules: str, llm: LLMClient) -> str:
    """Compile domain rules in a single LLM call."""
    prompt_path = Path(__file__).parent.parent / "prompts" / "domain_extractor.md"
    template = prompt_path.read_text(encoding="utf-8")
    system_prompt = template.replace("{domain_rules_text}", raw_rules)

    try:
        result = llm.chat(system_prompt, "Extract all structured rules now.")
        if result and len(result.strip()) > 50:
            return result.strip()
    except Exception as exc:
        logger.warning("Domain rules compilation failed: %s", exc)

    return ""


def _compile_chunked(raw_rules: str, llm: LLMClient, total_tokens: int) -> str:
    """Compile domain rules in chunks for extremely large documents.

    Strategy:
    1. Split raw text into chunks at paragraph/section boundaries
    2. Compile each chunk independently
    3. Merge compiled chunks into a single document
    """
    chunks = _split_into_chunks(raw_rules)
    logger.info("Splitting %d-token domain rules into %d chunks", total_tokens, len(chunks))

    compiled_parts: list[str] = []
    for i, chunk in enumerate(chunks):
        compiled = _compile_single(chunk, llm)
        if compiled:
            compiled_parts.append(f"<!-- chunk {i + 1}/{len(chunks)} -->\n{compiled}")
        else:
            # Fallback: keep raw chunk but truncated
            truncated = chunk[:_CHUNK_MAX_CHARS // 2]
            compiled_parts.append(f"<!-- chunk {i + 1}/{len(chunks)} (raw, truncated) -->\n{truncated}")
            logger.warning("Chunk %d/%d compilation failed, using truncated raw", i + 1, len(chunks))

    result = "\n\n---\n\n".join(compiled_parts)
    compiled_tokens = estimate_tokens(result)

    # Guard: if compilation didn't actually reduce size, return raw
    if compiled_tokens >= total_tokens:
        logger.warning(
            "Chunked compilation did not reduce size (%d >= %d tokens). "
            "Returning raw domain rules.",
            compiled_tokens, total_tokens,
        )
        return raw_rules

    logger.info(
        "Chunked compilation complete: %d → %d tokens (%.0f%% reduction)",
        total_tokens, compiled_tokens,
        (1 - compiled_tokens / total_tokens) * 100,
    )
    return result


def _split_into_chunks(text: str) -> list[str]:
    """Split text into chunks at section boundaries.

    Prefers splitting at markdown headings or file separators (=== filename ===).
    Falls back to paragraph boundaries.
    """
    # Try splitting at file separators first (=== filename ===)
    file_sections = re.split(r"(?=^===\s.+\s===$)", text, flags=re.MULTILINE)
    file_sections = [s for s in file_sections if s.strip()]

    chunks: list[str] = []
    current_chunk = ""

    for section in file_sections:
        if len(current_chunk) + len(section) > _CHUNK_MAX_CHARS and current_chunk:
            chunks.append(current_chunk)
            current_chunk = section
        else:
            current_chunk += ("\n\n" if current_chunk else "") + section

    if current_chunk:
        chunks.append(current_chunk)

    # If still too few chunks (single giant file), split by headings
    if len(chunks) == 1 and len(chunks[0]) > _CHUNK_MAX_CHARS:
        return _split_by_headings(chunks[0])

    return chunks


def _split_by_headings(text: str) -> list[str]:
    """Split a single large document by markdown headings."""
    parts = re.split(r"(?=^#{1,4}\s)", text, flags=re.MULTILINE)
    parts = [p for p in parts if p.strip()]

    chunks: list[str] = []
    current = ""

    for part in parts:
        if len(current) + len(part) > _CHUNK_MAX_CHARS and current:
            chunks.append(current)
            current = part
        else:
            current += part

    if current:
        chunks.append(current)

    # Final fallback: hard split
    if len(chunks) == 1 and len(chunks[0]) > _CHUNK_MAX_CHARS:
        text = chunks[0]
        return [text[i:i + _CHUNK_MAX_CHARS] for i in range(0, len(text), _CHUNK_MAX_CHARS)]

    return chunks


# --- Deterministic value distributions (P1b) ---

_MAX_FULL_CARDINALITY = 100   # show all values if unique count ≤ this
_TOP_N_HIGH_CARDINALITY = 20  # show top-N when unique count > above
_MAX_NUMERIC_CARDINALITY = 30 # show all values for low-cardinality numeric cols


def _deterministic_value_distributions(manifest: Manifest) -> str:
    """Compute exact value counts for structured data files. Zero LLM cost.

    Always runs after LLM profiling. Provides PlannerCoder with the exact
    categorical values it needs to write correct filter conditions (e.g.
    WHERE city = 'Riverside' instead of guessing).
    """
    try:
        import pandas as pd
    except ImportError:
        logger.warning("pandas not available for deterministic value distributions")
        return ""

    parts: list[str] = []

    for entry in manifest.entries:
        try:
            if entry.file_type in ("csv",):
                section = _csv_value_distributions(entry.file_path, pd)
            elif entry.file_type in ("sqlite",):
                section = _sqlite_value_distributions(entry.file_path, pd)
            elif entry.file_type == "excel":
                section = _excel_value_distributions(entry.file_path, pd)
            elif entry.file_type == "parquet":
                section = _parquet_value_distributions(entry.file_path, pd)
            else:
                continue

            if section:
                parts.append(section)
        except Exception as exc:
            logger.debug(
                "Deterministic value distribution failed for %s: %s",
                entry.file_path, exc,
            )

    if not parts:
        return ""

    header = "## DETERMINISTIC VALUE DISTRIBUTIONS\n(Exact counts — use these for filter values)\n"
    return header + "\n\n".join(parts)


def _format_value_counts(col_name: str, series: "pd.Series") -> str:  # type: ignore[name-defined]
    """Format value_counts for a single column as readable text."""
    non_null = series.dropna()
    n_total = len(series)
    nunique = non_null.nunique()

    if nunique == 0:
        return ""

    # Decide how many values to show
    if nunique <= _MAX_FULL_CARDINALITY:
        vc = non_null.value_counts()
    else:
        vc = non_null.value_counts().head(_TOP_N_HIGH_CARDINALITY)

    lines: list[str] = [f"  {col_name} ({nunique} unique values):"]
    for val, count in vc.items():
        pct = round(count / n_total * 100, 1)
        lines.append(f"    {val!r}: {count} ({pct}%)")

    if nunique > _MAX_FULL_CARDINALITY:
        lines.append(f"    ... (top {_TOP_N_HIGH_CARDINALITY} of {nunique} shown)")

    return "\n".join(lines)


def _should_profile_column(series: "pd.Series") -> bool:  # type: ignore[name-defined]
    """Return True if this column's value distribution is useful to profile."""
    import pandas as pd
    non_null = series.dropna()
    if len(non_null) == 0:
        return False
    if pd.api.types.is_object_dtype(series) or pd.api.types.is_bool_dtype(series):
        return True
    if pd.api.types.is_numeric_dtype(series):
        return non_null.nunique() <= _MAX_NUMERIC_CARDINALITY
    return False


def _csv_value_distributions(file_path: str, pd: object) -> str:
    """Value distributions for a CSV file."""
    import pandas as real_pd
    try:
        df = real_pd.read_csv(file_path, encoding="utf-8", low_memory=False)
    except UnicodeDecodeError:
        df = real_pd.read_csv(file_path, encoding="latin-1", low_memory=False)

    name = Path(file_path).name
    col_sections: list[str] = []
    for col in df.columns:
        if _should_profile_column(df[col]):
            fmt = _format_value_counts(col, df[col])
            if fmt:
                col_sections.append(fmt)

    if not col_sections:
        return ""
    return f"### {name}\n" + "\n".join(col_sections)


def _sqlite_value_distributions(file_path: str, pd: object) -> str:
    """Value distributions for all tables in a SQLite database."""
    import pandas as real_pd
    conn = sqlite3.connect(file_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        name = Path(file_path).name
        table_sections: list[str] = []

        for table in tables:
            try:
                df = real_pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
                col_sections: list[str] = []
                for col in df.columns:
                    if _should_profile_column(df[col]):
                        fmt = _format_value_counts(col, df[col])
                        if fmt:
                            col_sections.append(fmt)
                if col_sections:
                    table_sections.append(f"  Table: {table}\n" + "\n".join(col_sections))
            except Exception as exc:
                logger.debug("sqlite table %s distribution failed: %s", table, exc)

        if not table_sections:
            return ""
        return f"### {name}\n" + "\n".join(table_sections)
    finally:
        conn.close()


def _excel_value_distributions(file_path: str, pd: object) -> str:
    """Value distributions for an Excel file."""
    import pandas as real_pd
    xl = real_pd.ExcelFile(file_path)
    name = Path(file_path).name
    sheet_sections: list[str] = []

    for sheet in xl.sheet_names:
        try:
            df = xl.parse(sheet)
            col_sections: list[str] = []
            for col in df.columns:
                if _should_profile_column(df[col]):
                    fmt = _format_value_counts(str(col), df[col])
                    if fmt:
                        col_sections.append(fmt)
            if col_sections:
                sheet_sections.append(f"  Sheet: {sheet}\n" + "\n".join(col_sections))
        except Exception as exc:
            logger.debug("excel sheet %s distribution failed: %s", sheet, exc)

    if not sheet_sections:
        return ""
    return f"### {name}\n" + "\n".join(sheet_sections)


def _parquet_value_distributions(file_path: str, pd: object) -> str:
    """Value distributions for a Parquet file."""
    import pandas as real_pd
    df = real_pd.read_parquet(file_path)
    name = Path(file_path).name
    col_sections: list[str] = []
    for col in df.columns:
        if _should_profile_column(df[col]):
            fmt = _format_value_counts(col, df[col])
            if fmt:
                col_sections.append(fmt)
    if not col_sections:
        return ""
    return f"### {name}\n" + "\n".join(col_sections)


# --- Original functions (unchanged) ---


def _extract_domain_rules(manifest: Manifest) -> str:
    """Extract full content from documentation files as domain rules.

    Documentation files (manual.md, README, knowledge.md, etc.) contain
    formulas, definitions, and business rules that agents need to follow.
    These are preserved in full — they are HIGH PRIORITY information.

    This is deterministic (no LLM cost) and creates an independent channel
    so domain knowledge is never squeezed out by statistical profiles.
    """
    doc_parts: list[str] = []

    for entry in manifest.entries:
        if entry.file_type not in ("markdown", "pdf", "docx"):
            continue

        # Get text content from profiler's text_preview
        text = entry.summary.get("text_preview", "")
        if not text:
            continue

        # Read full file if text_preview was truncated
        file_path = entry.file_path
        if entry.summary.get("char_count", 0) > len(text):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                pass  # fall back to text_preview

        doc_parts.append(f"=== {entry.file_path} ===\n{text}")

    return "\n\n".join(doc_parts)


def _attempt_llm_profile(manifest_json: str, llm: LLMClient, sandbox: Sandbox) -> str:
    """Full LLM-generated profiling code."""
    prompt_path = Path(__file__).parent.parent / "prompts" / "analyzer.md"
    system_prompt = prompt_path.read_text(encoding="utf-8")
    system_prompt = system_prompt.replace("{manifest_json}", manifest_json)

    response = llm.chat(system_prompt, "Generate the profiling code now.")

    code = _extract_code(response)
    if not code:
        code = response

    result = sandbox.execute(code, step_id="analyzer")

    if result.return_code == 0 and len(result.stdout.strip()) > 50:
        return result.stdout

    return ""


def _attempt_simple_profile(manifest_json: str, llm: LLMClient, sandbox: Sandbox) -> str:
    """Retry with a simpler prompt focused on robustness."""
    simple_prompt = f"""Write Python code to profile these data files. Keep it simple and robust.

## Files
{manifest_json}

## Rules
1. TASK_DIR environment variable has the data path.
2. For CSV files: use pandas with encoding='utf-8' first, then 'latin-1' as fallback. Print: shape, column names with dtypes, null counts, and for each column: unique count, min, max, and top 5 most frequent values.
3. For JSON files: load with json module. Print: type (list/dict), length, and if list of dicts, print all keys and for each key show unique value count and top 5 values.
4. For SQLite: print table names, schemas, row counts, and 3 sample rows per table.
5. For text files (md/txt): skip — already handled separately.
6. Wrap each file in try/except to ensure one file failure doesn't stop the whole script.
7. Print "=== <filename> ===" header before each file summary.
"""

    response = llm.chat(simple_prompt, "Generate the profiling code now. Keep it simple.")

    code = _extract_code(response)
    if not code:
        code = response

    result = sandbox.execute(code, step_id="analyzer_retry")

    if result.return_code == 0 and len(result.stdout.strip()) > 50:
        return result.stdout

    return ""


def _deterministic_fallback(manifest_json: str) -> str:
    """Always-works fallback using manifest metadata. No LLM needed."""
    return (
        "Data profile (from manifest metadata — profiling code failed, "
        "so this is schema-level only. You MUST explore the actual data "
        "in your first step before making assumptions about values):\n\n"
        f"{manifest_json}"
    )


def _extract_code(response: str) -> str:
    """Extract Python code from markdown code blocks."""
    match = re.search(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""
