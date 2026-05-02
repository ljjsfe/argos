"""Analyzer agent: domain-rules extraction from documentation.

Public API used by orchestrator:
- _extract_domain_rules(manifest) — deterministic, reads doc files
- compile_domain_rules(raw, llm, budget) — optional LLM compaction
  for documents that exceed a token-budget fraction.

Historical note: an LLM-driven `analyze()` profiling path existed but
was superseded by the rich Profiler (csv_reader/sqlite_reader/etc.) which
computes DISTINCT values, cardinality, and samples deterministically.
The LLM profiling code was removed in v60 prompt audit.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..core.llm_client import LLMClient
from ..core.token_estimator import estimate_tokens
from ..core.types import Manifest

logger = logging.getLogger(__name__)

# Compilation threshold: compile when domain rules exceed this fraction
# of the CM budget. At 8% of 262K budget ≈ 21K tokens (~84K chars),
# this fires for any doc over ~84KB to prevent per-iteration token burn.
# Root cause: task_396 (178KB, 44K tok) was included raw in all 8 iterations
# = 352K tokens wasted. Compilation fires once, producing a compact summary.
_COMPILE_BUDGET_FRACTION = 0.08

# Chunk size for extremely large docs (in chars).
# Each chunk must fit within the LLM's context window for compilation.
# ~80K chars ≈ ~20K tokens, leaving room for prompt + output.
_CHUNK_MAX_CHARS = 80_000


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



# --- Original functions (unchanged) ---


def _extract_domain_rules(manifest: Manifest) -> str:
    """Extract full content from documentation files as domain rules.

    Documentation files (manual.md, README, knowledge.md, etc.) contain
    formulas, definitions, and business rules that agents need to follow.
    These are preserved in full — they are HIGH PRIORITY information.

    Markdown: reads file directly from disk (plain text).
    PDF/DOCX: uses profiler-extracted text from summary (requires pdfplumber/python-docx).

    This is deterministic (no LLM cost) and creates an independent channel
    so domain knowledge is never squeezed out by statistical profiles.
    """
    doc_parts: list[str] = []

    for entry in manifest.entries:
        if entry.file_type not in ("markdown", "pdf", "docx"):
            continue

        if entry.file_type == "markdown":
            # Markdown is plain text — read directly from disk for full content
            text = _read_text_file(entry.file_path)
        else:
            # PDF/DOCX are binary — use profiler-extracted text from summary
            text = entry.summary.get("text_preview", "")

        if text and text.strip():
            doc_parts.append(f"=== {entry.file_path} ===\n{text}")

    return "\n\n".join(doc_parts)


def _read_text_file(file_path: str) -> str:
    """Read a plain text file with encoding fallback."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        try:
            with open(file_path, "r", encoding="latin-1") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return ""

