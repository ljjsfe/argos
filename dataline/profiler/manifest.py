"""Scan task directory and build Manifest."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

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


# Output-convention blacklist (universal input hygiene).
# A data agent should never consume artifacts that look like its own output.
# These patterns are conservative — they match output-naming conventions across
# data-agent projects (not KDD-specific): result.json, prediction.csv, etc.
RESERVED_FILENAMES = frozenset({
    # Agent outputs (this project)
    "result.json",
    "step_result.json",
    "prediction.csv",
    "trace.json",
    "trace_agent.json",
    "status.json",
    "final_answer.txt",
    # Benchmark gold/ground-truth files (universal hygiene).
    # If any of these end up inside task_dir (misconfigured layout, manual
    # copy during testing, third-party benchmark with non-standard layout)
    # the agent could otherwise read the answer directly, producing fake-
    # high scores. List is conservative — only strongly benchmark-flavored
    # names. `answers.csv` / `reference.csv` are intentionally NOT here
    # because they're legitimate data file names in many real datasets.
    "gold.csv",
    "gold.json",
    "ground_truth.csv",
    "ground_truth.json",
    "solution.csv",
    "solution.json",
})
RESERVED_SUFFIXES = ("_result.json", "_prediction.csv", "_results.pkl")
RESERVED_PREFIXES = ("intermediate_", "step_")
# Directories that conventionally contain agent output, regardless of where
# they sit in the input tree. Matching is by directory basename, anywhere in
# the path.
RESERVED_DIR_BASENAMES = frozenset({"output", "workspace", "temp", "_pred_task_"})


def _reserved_artifact_reason(rel_path: Path) -> str | None:
    """Return a short reason string if `rel_path` is an agent-output artifact.

    `rel_path` is relative to the task root. Returns None for legitimate input.
    """
    for part in rel_path.parts[:-1]:  # exclude the filename
        if part in RESERVED_DIR_BASENAMES:
            return f"reserved dir '{part}'"
    name = rel_path.name
    if name in RESERVED_FILENAMES:
        return "reserved filename"
    if name.endswith(RESERVED_SUFFIXES):
        return "reserved suffix"
    if name.startswith(RESERVED_PREFIXES):
        return "reserved prefix"
    return None


def scan(task_dir: str) -> Manifest:
    """Scan a task directory recursively and build a Manifest."""
    task_dir_abs = os.path.abspath(task_dir)
    task_root = Path(task_dir_abs)
    entries: list[ManifestEntry] = []
    skipped: list[tuple[str, str]] = []

    for root, dirs, files in os.walk(task_dir_abs):
        # Skip dot-directories (e.g., .dataline_cache, .git, .ipynb_checkpoints)
        # in-place modify dirs so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            # Skip task metadata — not a data source
            if fname == "task.json":
                continue
            fpath = os.path.join(root, fname)
            rel = Path(fpath).relative_to(task_root)

            # Output-convention hygiene: never consume agent-output artifacts.
            reason = _reserved_artifact_reason(rel)
            if reason is not None:
                skipped.append((str(rel), reason))
                logger.info("Profiler skip: %s (%s)", rel, reason)
                continue

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
        task_root=task_dir_abs,
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


def safe_relative_path(abs_path: str, task_root: str) -> str:
    """Return a path that is safe to emit into LLM prompts.

    Converts absolute paths under task_root to relative form. Anything
    outside task_root (e.g. helper / cache / extracted CSV) falls back
    to basename — never expose absolute paths to the LLM.

    Reason: agent code can otherwise do `open('/abs/task_dir/gold.csv')`
    and bypass the scratch sandbox entirely. (2026-05-20 audit follow-up.)
    """
    if not abs_path:
        return abs_path
    if not task_root:
        return os.path.basename(abs_path)
    try:
        rel = os.path.relpath(abs_path, task_root)
    except ValueError:
        return os.path.basename(abs_path)
    # If relpath produces ".." it means path is outside task_root — fall
    # back to basename to avoid leaking sibling structure.
    if rel.startswith(".."):
        return os.path.basename(abs_path)
    return rel


def manifest_to_json(manifest: Manifest) -> str:
    """Serialize manifest to JSON string for prompts.

    File paths are RELATIVIZED against manifest.task_root before
    emission — never expose absolute paths to the LLM (would let
    agent code construct `open('/abs/task_dir/gold.csv')`).
    """
    import json

    root = manifest.task_root
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
            "path": safe_relative_path(entry.file_path, root),
            "type": entry.file_type,
            "size_bytes": entry.size_bytes,
            "summary": summary,
        })
    for rel in manifest.cross_source_relations:
        data["cross_source_relations"].append({
            "source_a": safe_relative_path(rel.source_a, root),
            "source_b": safe_relative_path(rel.source_b, root),
            "relation": rel.relation,
            "confidence": rel.confidence,
        })

    return json.dumps(data, indent=2, default=str, ensure_ascii=False)
