"""Dispatcher for document → structured-table extractors.

Reads text-bearing docs from the manifest and runs the appropriate
extractor (currently: narrative_extractor only). Extensible by adding
more extractors to `_EXTRACTORS` — each is a callable that takes
`(source_path, text, llm_client)` and returns `ExtractedSchema | None`.

Future extensions (NOT IMPLEMENTED, this is the integration point):
- `table_extractor`: PDF-tables, OCR'd image-tables → CSV
- `image_extractor`: vision-LLM extraction from non-tabular images
- `chart_extractor`: time series / bar / pie from image

Currently only handles markdown / pdf / docx text-bearing docs.
Returns the list of successfully extracted schemas (may be empty).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.types import Manifest
from . import narrative_extractor

logger = logging.getLogger(__name__)


# Text-bearing doc types we know how to read. Note: we deliberately
# include `pdf`, `docx` here even though the current narrative_extractor
# was written against markdown — the extractor itself is text-shape
# agnostic, only `detect_narrative_shape` cares about content.
_TEXT_DOC_TYPES = {"markdown", "pdf", "docx"}


# Ordered list of extractors to try per doc. First successful wins.
# Future: add table_extractor, image_extractor here. Each must accept
# (source_path: str, text: str, llm_client: Any) -> ExtractedSchema | None.
_EXTRACTORS: list = [
    narrative_extractor.extract_records,
]


def _read_text_for_entry(file_path: str, file_type: str) -> str | None:
    """Read text content from a doc by extension. Returns None on failure.

    Reuses the data_helpers safe_read_* functions for PDF/DOCX so the
    binary→text logic is in one place. Markdown is a direct read.
    """
    p = Path(file_path)
    if not p.is_file():
        return None
    try:
        if file_type == "markdown":
            return p.read_text(encoding="utf-8", errors="replace")
        if file_type == "pdf":
            from ..helpers.data_helpers import safe_read_pdf  # lazy import
            return safe_read_pdf(file_path)
        if file_type == "docx":
            from ..helpers.data_helpers import safe_read_docx  # lazy import
            return safe_read_docx(file_path)
    except Exception as e:
        logger.debug("doc_extractor: text read failed for %s: %s", file_path, e)
        return None
    return None


def extract_docs(
    manifest: Manifest,
    llm_client: Any,
) -> list:
    """Run all extractors on text-bearing manifest entries.

    Returns: list of ExtractedSchema (one per successful extraction).
    The caller is responsible for surfacing these to Sandbox (so the
    extracted CSVs get symlinked into scratch dir) and to the manifest
    text (so Planner sees the virtual tables).

    Fail-open: any single failure → skip that entry, continue with rest.
    Never raises.
    """
    extracted = []
    if llm_client is None:
        return extracted

    for entry in manifest.entries:
        if entry.file_type not in _TEXT_DOC_TYPES:
            continue
        text = _read_text_for_entry(entry.file_path, entry.file_type)
        if not text:
            continue
        # Try each extractor in order; first non-None wins.
        for extractor in _EXTRACTORS:
            try:
                schema = extractor(entry.file_path, text, llm_client)
            except Exception as e:
                logger.warning(
                    "doc_extractor: %s raised on %s: %s",
                    extractor.__name__, entry.file_path, e,
                )
                schema = None
            if schema is not None:
                extracted.append(schema)
                break  # one extractor per doc
    return extracted


def schemas_to_summary_lines(schemas: list) -> list[str]:
    """Format extracted schemas as one-line summaries for the manifest text.

    These lines are appended to the manifest_json so the Planner can see
    the virtual tables in its context.
    """
    lines = []
    for s in schemas:
        cols = ", ".join(s.columns[:8]) + ("…" if len(s.columns) > 8 else "")
        src = Path(s.source_path).name
        lines.append(
            f"- Virtual table `{s.table_name}` from {src}: "
            f"{s.row_count} rows, columns ({cols}). "
            f"Use SELECT * FROM {s.table_name} like any other table."
        )
    return lines
