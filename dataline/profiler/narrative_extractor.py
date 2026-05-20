"""Narrative-document → structured records extractor (Block 4 adapter).

Profiler-stage component that turns prose markdown describing repeating
entity records (patient narratives, molecule case studies, transaction
logs in story form) into a structured CSV the Planner can query as a
DuckDB view — bypassing the fragile-regex authorship the Planner used
to do per-task.

Why this lives in the Profiler (not in Planner prompt):
- One-shot extraction at task start, not in the planning iter loop.
- Deterministic-cached by content hash — same LLM behaviour across
  identical inputs.
- Failure path falls back gracefully to the existing text-only manifest
  entry; never blocks the task.
- Same caching/hygiene discipline as B2 doc_glossary (lessons learned
  in 2026-05-19 audit: cache writes go to repo root, never task_dir).

Design reference: docs/BLOCK4_NARRATIVE_EXTRACTION_DESIGN.md
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Cache schema version — bump if the extraction-prompt or output JSON
# shape changes incompatibly, so stale caches are invalidated.
_SCHEMA_VERSION = "narrative-v1"


# Repo-relative cache for extracted CSVs and metadata. Mirrors the B2
# pattern: NEVER write inside task_dir (Profiler would re-scan on next
# run, creating self-injected info pollution — see 2026-05-19 audit).
_REPO_ROOT_CACHE = Path(__file__).resolve().parents[2] / ".dataline_cache" / "narrative_extracts"


@dataclass(frozen=True)
class ExtractedSchema:
    """One narrative document → one virtual table."""
    source_path: str        # original .md path (informational)
    table_name: str         # DuckDB view name (sanitized)
    entity_type: str        # e.g. "patient", "molecule", "transaction"
    columns: tuple[str, ...]
    row_count: int
    csv_path: str           # absolute path to extracted CSV (under cache)


# ── Narrative-shape detection ─────────────────────────────────────────

_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_REPEATING_ENTITY_RE = re.compile(
    # "Patient 43003" / "molecule 5" / "transaction #12" — repeated entity tokens
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:No\.?\s*)?#?(\d{1,6})\b",
)


def detect_narrative_shape(md_text: str) -> tuple[bool, str]:
    """Return (is_narrative, reason).

    Conservative heuristic — false positives are wasteful (cost an LLM
    call), false negatives just mean the existing text-only manifest entry
    is used. Bias toward false negatives.

    Conditions (all must hold):
      1. Doc is large enough to plausibly hold structured records
         (≥ 500 chars) but not absurdly huge (≤ 50K chars truncated).
      2. Markdown-table density is low: # table rows / # lines < 5%.
      3. ≥ 2 distinct repeating-entity references (e.g. "Patient 43003",
         "Patient 133382") — same capitalized noun appears with ≥ 2
         distinct numeric IDs.
    """
    n_chars = len(md_text)
    if n_chars < 500:
        return False, "too short"
    if n_chars > 50_000:
        # Long docs ARE candidates but we truncate downstream; still allow.
        pass

    lines = md_text.splitlines()
    n_lines = max(len(lines), 1)
    n_table_rows = len(_TABLE_ROW_RE.findall(md_text))
    if n_table_rows / n_lines >= 0.05:
        return False, f"table-heavy ({n_table_rows}/{n_lines} table-rows)"

    # Repeating-entity count: group by entity word, count distinct numeric IDs.
    by_entity: dict[str, set[str]] = {}
    for m in _REPEATING_ENTITY_RE.finditer(md_text):
        ent = m.group(1).lower().strip()
        nid = m.group(2)
        by_entity.setdefault(ent, set()).add(nid)
    repeating = [e for e, ids in by_entity.items() if len(ids) >= 2]
    if not repeating:
        return False, "no repeating entity"

    return True, f"narrative (entity={repeating[0]}, n_instances≥{len(by_entity[repeating[0]])})"


# ── Sanitization helpers ──────────────────────────────────────────────

_SAFE_NAME_RE = re.compile(r"[^a-z0-9_]")


def _sanitize_view_name(base: str) -> str:
    """Turn a filename into a DuckDB-safe view name.

    "Patient.md" → "patient", "Lab-Records.md" → "lab_records".
    """
    name = base.lower().strip()
    name = re.sub(r"\.\w+$", "", name)  # strip extension
    name = _SAFE_NAME_RE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "narrative"


# ── Cache helpers ─────────────────────────────────────────────────────

def _content_hash(md_text: str, model_name: str) -> str:
    """Content-only hash (NOT path). Identical doc bytes → same cache entry,
    safe to share across tasks (same B2 pattern after 2026-05-19 audit).
    """
    h = hashlib.sha256()
    h.update(_SCHEMA_VERSION.encode())
    h.update(b"|")
    h.update(model_name.encode())
    h.update(b"|")
    h.update(md_text.encode("utf-8", errors="replace"))
    return h.hexdigest()[:32]


def _cache_dir() -> Path:
    return _REPO_ROOT_CACHE


def _load_cache(key: str) -> ExtractedSchema | None:
    """Return ExtractedSchema if cache hit + CSV file still exists, else None."""
    meta_path = _cache_dir() / f"{key}.json"
    if not meta_path.is_file():
        return None
    try:
        obj = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    csv_path = obj.get("csv_path", "")
    if not csv_path or not Path(csv_path).is_file():
        return None
    try:
        return ExtractedSchema(
            source_path=obj.get("source_path", ""),
            table_name=obj["table_name"],
            entity_type=obj.get("entity_type", "record"),
            columns=tuple(obj["columns"]),
            row_count=int(obj["row_count"]),
            csv_path=csv_path,
        )
    except (KeyError, TypeError):
        return None


def _save_cache(key: str, schema: ExtractedSchema) -> None:
    """Atomic write of metadata (CSV already written separately)."""
    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    meta = {
        "source_path": schema.source_path,
        "table_name": schema.table_name,
        "entity_type": schema.entity_type,
        "columns": list(schema.columns),
        "row_count": schema.row_count,
        "csv_path": schema.csv_path,
    }
    target = cache_dir / f"{key}.json"
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=str(cache_dir),
        )
        json.dump(meta, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
        tmp.close()
        os.replace(tmp_name, target)
    except OSError:
        pass


# ── Extraction (LLM-backed) ──────────────────────────────────────────

_EXTRACTION_PROMPT_VERSION = "v1"

_EXTRACTION_PROMPT = """You are extracting structured records from a narrative document.

The document describes multiple instances of a single entity type
(e.g., patients, products, transactions, molecules). Your task:

1. Identify the entity type (one word, e.g., "patient", "molecule").
2. List the fields documented for each instance. Include ONLY fields that
   appear with concrete values across multiple instances. Use lowercase
   snake_case for column names (e.g., "patient_id", "creatinine_mg_dl").
3. For each instance, emit one JSON object with the fields you identified.
   Use null for missing values. Numeric fields should be numbers (not
   strings). Dates as YYYY-MM-DD strings.

Output ONLY valid JSON with this exact shape:
{
  "entity_type": "patient",
  "schema": [{"name": "patient_id", "type": "integer"}, ...],
  "records": [{"patient_id": 43003, "sex": "male", ...}, ...]
}

If the document does NOT contain repeating structured records (e.g., it's
a glossary, a single article, or pure prose), output: {"entity_type": null,
"schema": [], "records": []} — do not invent records.

Document:
---
"""


def _build_prompt(md_text: str, max_chars: int = 30_000) -> str:
    """Return prompt with doc truncated if very large.

    Truncation is conservative — 30K char limit lets us handle docs up
    to ~7K tokens of content while leaving room for the model's response.
    Truncated docs are noted explicitly so the model knows it doesn't
    have the full picture.
    """
    if len(md_text) <= max_chars:
        return _EXTRACTION_PROMPT + md_text
    head = md_text[: max_chars - 200]
    tail_marker = (
        f"\n\n[... truncated; document continues for "
        f"{len(md_text) - len(head)} more characters ...]\n"
    )
    return _EXTRACTION_PROMPT + head + tail_marker


def _parse_llm_output(raw: str) -> dict[str, Any] | None:
    """Extract JSON object from LLM output. Tolerant of fenced blocks."""
    if not raw:
        return None
    # Strip code-fence wrappers if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    # Find the first top-level JSON object.
    start = candidate.find("{")
    if start == -1:
        return None
    end = candidate.rfind("}")
    if end == -1 or end <= start:
        return None
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None


def _write_csv(records: list[dict], columns: list[str], csv_path: Path) -> int:
    """Write records to CSV (atomic, returns row count). Skips empty values
    as blank cells. Coerces non-string scalars to their string repr.
    """
    import csv
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = {c: (r.get(c) if r.get(c) is not None else "") for c in columns}
            writer.writerow(row)
    os.replace(tmp, csv_path)
    return len(records)


def extract_records(
    source_path: str,
    md_text: str,
    llm_client: Any,
    model_name: str = "narrative-extractor",
) -> ExtractedSchema | None:
    """Extract structured records via one LLM call. Cached by content hash.

    Args:
        source_path: original .md path (informational, NOT used in cache key).
        md_text: full markdown text.
        llm_client: object with .complete(prompt: str) -> str (or compatible).
        model_name: included in cache key so different models don't collide.

    Returns:
        ExtractedSchema on success, None on any failure (LLM error, invalid
        JSON, zero records).
    """
    is_narr, reason = detect_narrative_shape(md_text)
    if not is_narr:
        logger.debug("narrative_extractor: skip (%s)", reason)
        return None

    key = _content_hash(md_text, model_name)
    cached = _load_cache(key)
    if cached is not None:
        logger.info("narrative_extractor: cache hit for %s", source_path)
        return cached

    if llm_client is None:
        logger.debug("narrative_extractor: no LLM client; skip")
        return None

    prompt = _build_prompt(md_text)
    try:
        raw = llm_client.complete(prompt)
    except Exception as e:
        logger.warning("narrative_extractor: LLM error on %s: %s", source_path, e)
        return None

    parsed = _parse_llm_output(raw)
    if parsed is None:
        logger.warning("narrative_extractor: bad JSON output for %s", source_path)
        return None

    entity_type = parsed.get("entity_type")
    schema = parsed.get("schema") or []
    records = parsed.get("records") or []
    if not entity_type or not schema or not records:
        logger.debug("narrative_extractor: empty extraction for %s", source_path)
        return None

    columns = [s["name"] for s in schema if isinstance(s, dict) and s.get("name")]
    if not columns:
        return None

    basename = Path(source_path).name
    table_name = f"narrative_{_sanitize_view_name(basename)}"
    csv_path = _cache_dir() / f"{key}.csv"
    try:
        row_count = _write_csv(records, columns, csv_path)
    except OSError as e:
        logger.warning("narrative_extractor: csv write failed: %s", e)
        return None

    if row_count == 0:
        return None

    schema_obj = ExtractedSchema(
        source_path=source_path,
        table_name=table_name,
        entity_type=str(entity_type),
        columns=tuple(columns),
        row_count=row_count,
        csv_path=str(csv_path.resolve()),
    )
    _save_cache(key, schema_obj)
    return schema_obj
