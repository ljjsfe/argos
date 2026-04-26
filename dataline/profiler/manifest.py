"""Scan task directory and build Manifest."""

from __future__ import annotations

import os
from pathlib import Path

from ..core.types import Manifest, ManifestEntry
from .csv_reader import read_csv
from .json_reader import read_json
from .sqlite_reader import read_sqlite
from .markdown_reader import read_markdown
from .pdf_reader import read_pdf
from .docx_reader import read_docx
from .excel_reader import read_excel
from .image_reader import read_image
from .parquet_reader import read_parquet
from .cross_source import discover_relations

# Extension -> reader mapping
READERS = {
    ".csv": read_csv,
    ".tsv": read_csv,
    ".json": read_json,
    ".sqlite": read_sqlite,
    ".db": read_sqlite,
    ".sqlite3": read_sqlite,
    ".md": read_markdown,
    ".markdown": read_markdown,
    ".pdf": read_pdf,
    ".docx": read_docx,
    ".xlsx": read_excel,
    ".xls": read_excel,
    ".png": read_image,
    ".jpg": read_image,
    ".jpeg": read_image,
    ".parquet": read_parquet,
}


def scan(task_dir: str) -> Manifest:
    """Scan a task directory recursively and build a Manifest."""
    task_dir = os.path.abspath(task_dir)
    entries: list[ManifestEntry] = []

    for root, _dirs, files in os.walk(task_dir):
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            # Skip task metadata — not a data source
            if fname == "task.json":
                continue
            fpath = os.path.join(root, fname)
            ext = Path(fname).suffix.lower()

            reader = READERS.get(ext)
            if reader is not None:
                try:
                    entry = reader(fpath)
                    entries.append(entry)
                except Exception as e:
                    entries.append(ManifestEntry(
                        file_path=fpath,
                        file_type=ext.lstrip("."),
                        size_bytes=os.path.getsize(fpath),
                        summary={"error": str(e)},
                    ))

    # Discover cross-source relations
    relations = discover_relations(entries)

    # Extract keyword tags from all entries
    tags = _extract_tags(entries)

    return Manifest(
        entries=tuple(entries),
        cross_source_relations=tuple(relations),
        keyword_tags=tuple(tags),
    )


def _extract_tags(entries: list[ManifestEntry]) -> list[str]:
    """Extract keyword tags from all entries for knowledge retrieval."""
    tags: set[str] = set()
    for entry in entries:
        s = entry.summary
        # From structured data columns
        if "columns" in s:
            for col in s["columns"]:
                tags.add(col.get("name", ""))
        # From tables
        if "tables" in s:
            for table in s["tables"]:
                tags.add(table.get("name", ""))
                for col in table.get("columns", []):
                    tags.add(col.get("name", ""))
        # From markdown key terms
        if "key_terms" in s:
            tags.update(s["key_terms"])
        # From headings
        if "headings" in s:
            tags.update(s["headings"])

    tags.discard("")
    return sorted(tags)


def manifest_to_json(manifest: Manifest) -> str:
    """Serialize manifest to JSON string for prompts."""
    import json

    data = {
        "files": [],
        "cross_source_relations": [],
    }
    for entry in manifest.entries:
        # Exclude text_preview from serialization — domain text flows
        # through _extract_domain_rules(), not through manifest_json.
        summary = {
            k: v for k, v in entry.summary.items()
            if k != "text_preview"
        }
        data["files"].append({
            "path": entry.file_path,
            "type": entry.file_type,
            "size_bytes": entry.size_bytes,
            "summary": summary,
        })
    for rel in manifest.cross_source_relations:
        data["cross_source_relations"].append({
            "source_a": rel.source_a,
            "source_b": rel.source_b,
            "relation": rel.relation,
            "confidence": rel.confidence,
        })

    return json.dumps(data, indent=2, default=str, ensure_ascii=False)
