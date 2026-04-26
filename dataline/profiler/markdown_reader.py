"""Markdown file profiling."""

from __future__ import annotations

import os
import re

from ..core.types import ManifestEntry


def read_markdown(file_path: str) -> ManifestEntry:
    """Profile a Markdown file into a ManifestEntry."""
    size = os.path.getsize(file_path)

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    headings = re.findall(r"^#{1,4}\s+(.+)$", text, re.MULTILINE)

    # Extract key terms (capitalized multi-word phrases, quoted terms)
    key_terms = set()
    for match in re.findall(r"\*\*([^*]+)\*\*", text):
        key_terms.add(match.strip())
    for match in re.findall(r"`([^`]+)`", text):
        key_terms.add(match.strip())

    # Detect tables
    table_count = len(re.findall(r"^\|.+\|$", text, re.MULTILINE)) // 3  # rough estimate

    return ManifestEntry(
        file_path=file_path,
        file_type="markdown",
        size_bytes=size,
        summary={
            "headings": headings[:30],
            "key_terms": sorted(key_terms)[:30],
            "table_count": max(table_count, 0),
            "char_count": len(text),
        },
    )
