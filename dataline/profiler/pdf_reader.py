"""PDF file profiling via pdfplumber."""

from __future__ import annotations

import os
import re

from ..core.types import ManifestEntry


def read_pdf(file_path: str) -> ManifestEntry:
    """Profile a PDF file into a ManifestEntry."""
    size = os.path.getsize(file_path)

    try:
        import pdfplumber
    except ImportError:
        return ManifestEntry(
            file_path=file_path, file_type="pdf", size_bytes=size,
            summary={"error": "pdfplumber not installed"},
        )

    summary: dict = {}
    try:
        with pdfplumber.open(file_path) as pdf:
            summary["page_count"] = len(pdf.pages)

            # Extract text from first few pages
            text_parts = []
            tables_found = 0
            for page in pdf.pages[:10]:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                tables = page.extract_tables()
                tables_found += len(tables)

            full_text = "\n".join(text_parts)

            # Sections from heading-like patterns
            headings = re.findall(r"^[A-Z][A-Za-z\s]{3,50}$", full_text, re.MULTILINE)
            summary["sections"] = headings[:20]

            # Key numbers
            numbers = re.findall(r"[\$€£]?\d[\d,]*\.?\d*%?", full_text)
            summary["key_numbers"] = list(set(numbers))[:20]

            summary["tables_found"] = tables_found
            summary["text_preview"] = full_text
            summary["char_count"] = len(full_text)
    except Exception as e:
        summary["error"] = str(e)

    return ManifestEntry(
        file_path=file_path, file_type="pdf", size_bytes=size, summary=summary,
    )
