"""B2 — LLM-extracted doc glossary, format-agnostic.

Replaces the markdown-bold-prefix parser in `term_binding.py`. Mechanism:

  1. Concatenate all task docs (knowledge.md + context/doc/*.md), header-
     prefixed and length-capped.
  2. ONE LLM call with strict JSON schema to extract terms / formulas /
     rules / synonyms. Style-agnostic because the LLM is the parser.
  3. Cache result by sha256(schema + sorted file content). KDD submission
     can use `DATALINE_GLOBAL_CACHE_DIR` for warm starts.
  4. Deterministic question-glossary matching → ≤ 12-line hint block for
     PlannerCoder context.

Designed to be universal across benchmarks (no markdown-style assumption,
no domain-specific filter lists). See docs/B2_DOC_GLOSSARY_DESIGN.md.
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

from ..core.types import Manifest

logger = logging.getLogger(__name__)

# Bumped 2026-05-20 from b2.v1: path sanitization for source_files /
# source_section to close raw-task-dir leak via warm cache. Old caches
# remain on disk but won't be reused because cache_key hashes this version.
SCHEMA_VERSION = "b2.v2"
_MAX_DOC_BYTES_DEFAULT = 20_000


# ── Data classes ───────────────────────────────────────────────────

@dataclass(frozen=True)
class TermDefV2:
    name: str
    aliases: tuple[str, ...] = ()
    definition: str = ""
    data_field_table: str | None = None
    data_field_column: str | None = None
    value_enum: tuple[tuple[str, str], ...] = ()
    value_range_min: float | None = None
    value_range_max: float | None = None
    value_range_unit: str | None = None
    source_section: str = ""


@dataclass(frozen=True)
class FormulaDef:
    name: str = ""
    expression: str = ""
    inputs: tuple[str, ...] = ()
    source_section: str = ""


@dataclass(frozen=True)
class RuleDef:
    statement: str = ""
    applies_to: tuple[str, ...] = ()
    source_section: str = ""


@dataclass(frozen=True)
class DocGlossary:
    schema_version: str = SCHEMA_VERSION
    source_files: tuple[str, ...] = ()
    terms: tuple[TermDefV2, ...] = ()
    formulas: tuple[FormulaDef, ...] = ()
    rules: tuple[RuleDef, ...] = ()
    synonyms: tuple[tuple[str, str, str], ...] = ()  # (a, b, note)


EMPTY_GLOSSARY = DocGlossary()


# ── Doc collection ────────────────────────────────────────────────

# Universal convention: domain documentation lives under either the task
# root or a `context/` subdirectory. We treat ANY *.md file directly under
# these roots (or under a `doc/` subdirectory of either) as a candidate.
# This is convention-based — not benchmark-specific. KDD's `knowledge.md`
# and DABstep's `context/manual.md` + `context/payments-readme.md` are
# both captured by this rule without naming any benchmark explicitly.
def _collect_doc_files(task_dir: Path) -> list[Path]:
    """Return ordered, deduplicated doc files for a task.

    Scan strategy (universal — no benchmark-specific filename hardcoded):
      1. `<task_dir>/*.md` (top-level)
      2. `<task_dir>/context/*.md`
      3. `<task_dir>/doc/*.md`
      4. `<task_dir>/context/doc/*.md`

    Style-agnostic at the LLM-extraction layer too: B2's prompt parses any
    prose / heading / bullet content, not just markdown-bold-prefix terms.
    """
    seen: set[str] = set()
    out: list[Path] = []
    for sub in (task_dir, task_dir / "context", task_dir / "doc", task_dir / "context" / "doc"):
        if not sub.is_dir():
            continue
        for p in sorted(sub.glob("*.md")):
            rp = str(p.resolve())
            if rp in seen:
                continue
            # Skip Profiler-blacklisted output-convention names (defence in
            # depth — Profiler already filters these from the manifest).
            if p.name in {"task.json", "result.json", "step_result.json",
                          "prediction.csv", "trace.json"}:
                continue
            seen.add(rp)
            out.append(p)
    return out


def _read_and_concat(
    files: list[Path], max_bytes: int, task_root: str = "",
) -> tuple[str, list[str]]:
    """Return (concatenated_text_with_headers, list_of_relative_paths).

    Truncates total payload to `max_bytes` to keep the LLM prompt small.
    """
    # Sanitize paths for prompt + downstream hints. Same defense as
    # manifest_to_json: never expose absolute task_dir to the LLM —
    # agent code could derive it and `open('/abs/task_dir/gold.csv')`,
    # bypassing the scratch sandbox. (2026-05-20 audit follow-up.)
    from ..profiler.manifest import safe_relative_path

    chunks: list[str] = []
    paths_used: list[str] = []
    total = 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = safe_relative_path(str(p), task_root) if task_root else p.name
        header = f"\n===== {rel} =====\n"
        block = header + text + "\n"
        if total + len(block) > max_bytes:
            # Take what fits.
            remaining = max_bytes - total - len(header)
            if remaining > 200:
                chunks.append(header + text[:remaining] + "\n[truncated]\n")
                paths_used.append(rel)
            break
        chunks.append(block)
        paths_used.append(rel)
        total += len(block)
    return "".join(chunks), paths_used


# ── Cache helpers ─────────────────────────────────────────────────

def _cache_key(file_paths: list[Path], model_name: str) -> str:
    """Content-derived cache key — does NOT include filesystem path.

    Two tasks with identical doc bytes (e.g. same knowledge.md) share a
    single cache entry. This is safe because B2 only extracts terms/
    formulas from the doc bytes themselves — the result is independent of
    where the file lives. Cross-task sharing saves one LLM call per task
    when docs repeat (common across same-domain benchmarks).
    """
    h = hashlib.sha256()
    h.update(SCHEMA_VERSION.encode())
    h.update(b"|")
    h.update(model_name.encode())
    # Read all bytes first, then sort by content hash so order is stable
    # regardless of path or input order.
    blobs: list[bytes] = []
    for p in file_paths:
        try:
            blobs.append(Path(p).read_bytes())
        except OSError:
            continue
    for b in sorted(blobs):
        h.update(b"|")
        h.update(hashlib.sha256(b).digest())
    return h.hexdigest()[:32]


# Repo-root-relative cache. CRITICAL: do NOT write inside task_dir — Profiler
# would re-scan the cache file as an input on the next run, creating
# self-injected info pollution (audit 2026-05-19, V90 lessons). The cache
# key is content-derived so cross-task sharing happens naturally when two
# tasks reuse identical doc bytes.
_REPO_ROOT_CACHE = Path(__file__).resolve().parents[2] / ".dataline_cache" / "doc_glossary"


def _cache_paths(task_dir: Path, key: str) -> list[Path]:
    """Lookup order: repo-local cache, then global env-var cache if set.
    Task_dir is intentionally NOT a cache location (would pollute next run)."""
    candidates = [_REPO_ROOT_CACHE / f"{key}.json"]
    global_root = os.environ.get("DATALINE_GLOBAL_CACHE_DIR")
    if global_root:
        candidates.append(Path(global_root) / "doc_glossary" / f"{key}.json")
    return candidates


def _sanitize_cache_paths(glossary: DocGlossary, task_dir: Path) -> DocGlossary:
    """Defensive sanitization for cache hits — strip any absolute task_dir
    that leaked through from a prior code version (or a corrupt cache).

    The schema-version bump (b2.v1 → b2.v2) already invalidates stale
    caches via the content hash, but this is a belt-and-suspenders pass:
    even a v2 cache containing an unexpected absolute path is scrubbed
    here before reaching the planner-hint surface. 2026-05-20 audit fix.
    """
    from ..profiler.manifest import safe_relative_path
    root = str(task_dir)

    def _safe(s: str) -> str:
        if not s:
            return s
        # Only relativize if it looks like a filesystem path under task_dir.
        if root in s:
            return safe_relative_path(s, root)
        return s

    # source_files
    new_files = tuple(_safe(s) for s in glossary.source_files)
    # source_section on terms / formulas / rules
    new_terms = tuple(
        TermDefV2(
            name=t.name, aliases=t.aliases, definition=t.definition,
            data_field_table=_safe(t.data_field_table) if t.data_field_table else t.data_field_table,
            data_field_column=t.data_field_column,
            value_enum=t.value_enum,
            value_range_min=t.value_range_min,
            value_range_max=t.value_range_max,
            value_range_unit=t.value_range_unit,
            source_section=_safe(t.source_section),
        ) for t in glossary.terms
    )
    new_formulas = tuple(
        FormulaDef(
            name=f.name, expression=f.expression, inputs=f.inputs,
            source_section=_safe(f.source_section),
        ) for f in glossary.formulas
    )
    new_rules = tuple(
        RuleDef(
            statement=r.statement, applies_to=r.applies_to,
            source_section=_safe(r.source_section),
        ) for r in glossary.rules
    )
    return DocGlossary(
        schema_version=glossary.schema_version,
        source_files=new_files,
        terms=new_terms,
        formulas=new_formulas,
        rules=new_rules,
        synonyms=glossary.synonyms,
    )


def _load_cache(task_dir: Path, key: str) -> DocGlossary | None:
    for p in _cache_paths(task_dir, key):
        if p.is_file():
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
                return _sanitize_cache_paths(_from_dict(obj), task_dir)
            except (OSError, ValueError):
                continue
    return None


def _save_cache(task_dir: Path, key: str, glossary: DocGlossary) -> None:
    """Atomic write to repo-local cache (NOT task_dir). Best-effort; silent on error."""
    cache_dir = _REPO_ROOT_CACHE
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    target = cache_dir / f"{key}.json"
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=str(cache_dir),
        )
        json.dump(_to_dict(glossary), tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
        tmp.close()
        os.replace(tmp_name, target)
    except OSError:
        return


# ── JSON ↔ dataclass ──────────────────────────────────────────────

def _from_dict(obj: dict) -> DocGlossary:
    """Tolerant: extra keys ignored; missing keys default to empty."""
    if not isinstance(obj, dict):
        return EMPTY_GLOSSARY

    def _terms() -> tuple[TermDefV2, ...]:
        out: list[TermDefV2] = []
        for t in obj.get("terms") or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            data_field = t.get("data_field") or {}
            if not isinstance(data_field, dict):
                data_field = {}
            value_range = t.get("value_range") or {}
            if not isinstance(value_range, dict):
                value_range = {}
            enums: list[tuple[str, str]] = []
            for e in t.get("value_enum") or []:
                if isinstance(e, dict) and "value" in e:
                    enums.append((str(e.get("value", "")), str(e.get("meaning", ""))))
            out.append(TermDefV2(
                name=str(t["name"])[:80],
                aliases=tuple(str(a)[:80] for a in (t.get("aliases") or []) if a),
                definition=str(t.get("definition") or "")[:240],
                data_field_table=(
                    str(data_field.get("table_or_file"))
                    if data_field.get("table_or_file") else None
                ),
                data_field_column=(
                    str(data_field.get("column"))
                    if data_field.get("column") else None
                ),
                value_enum=tuple(enums),
                value_range_min=(
                    float(value_range["min"]) if value_range.get("min") is not None else None
                ),
                value_range_max=(
                    float(value_range["max"]) if value_range.get("max") is not None else None
                ),
                value_range_unit=(
                    str(value_range["unit"]) if value_range.get("unit") else None
                ),
                source_section=str(t.get("source_section") or "")[:120],
            ))
        return tuple(out)

    def _formulas() -> tuple[FormulaDef, ...]:
        out: list[FormulaDef] = []
        for f in obj.get("formulas") or []:
            if not isinstance(f, dict):
                continue
            out.append(FormulaDef(
                name=str(f.get("name") or "")[:120],
                expression=str(f.get("expression") or "")[:200],
                inputs=tuple(str(i)[:80] for i in (f.get("inputs") or []) if i),
                source_section=str(f.get("source_section") or "")[:120],
            ))
        return tuple(out)

    def _rules() -> tuple[RuleDef, ...]:
        out: list[RuleDef] = []
        for r in obj.get("rules") or []:
            if not isinstance(r, dict) or not r.get("statement"):
                continue
            out.append(RuleDef(
                statement=str(r["statement"])[:240],
                applies_to=tuple(str(a)[:80] for a in (r.get("applies_to") or []) if a),
                source_section=str(r.get("source_section") or "")[:120],
            ))
        return tuple(out)

    def _syns() -> tuple[tuple[str, str, str], ...]:
        out: list[tuple[str, str, str]] = []
        for s in obj.get("synonyms") or []:
            if not isinstance(s, dict):
                continue
            a, b = str(s.get("a") or ""), str(s.get("b") or "")
            if a and b:
                out.append((a[:80], b[:80], str(s.get("note") or "")[:120]))
        return tuple(out)

    return DocGlossary(
        schema_version=str(obj.get("schema_version") or SCHEMA_VERSION),
        source_files=tuple(str(s) for s in (obj.get("source_files") or [])),
        terms=_terms(),
        formulas=_formulas(),
        rules=_rules(),
        synonyms=_syns(),
    )


def _to_dict(g: DocGlossary) -> dict:
    return {
        "schema_version": g.schema_version,
        "source_files": list(g.source_files),
        "terms": [
            {
                "name": t.name,
                "aliases": list(t.aliases),
                "definition": t.definition,
                "data_field": {
                    "table_or_file": t.data_field_table,
                    "column": t.data_field_column,
                },
                "value_enum": [{"value": v, "meaning": m} for v, m in t.value_enum],
                "value_range": (
                    None if (t.value_range_min is None and t.value_range_max is None) else {
                        "min": t.value_range_min,
                        "max": t.value_range_max,
                        "unit": t.value_range_unit,
                    }
                ),
                "source_section": t.source_section,
            }
            for t in g.terms
        ],
        "formulas": [
            {
                "name": f.name,
                "expression": f.expression,
                "inputs": list(f.inputs),
                "source_section": f.source_section,
            }
            for f in g.formulas
        ],
        "rules": [
            {
                "statement": r.statement,
                "applies_to": list(r.applies_to),
                "source_section": r.source_section,
            }
            for r in g.rules
        ],
        "synonyms": [{"a": a, "b": b, "note": n} for a, b, n in g.synonyms],
    }


# ── LLM extraction ────────────────────────────────────────────────

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "doc_glossary.md"
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)


def _parse_llm_json(text: str) -> dict | None:
    if not text or not text.strip():
        return None
    for pat in (_JSON_FENCE_RE, _BARE_JSON_RE):
        m = pat.search(text)
        if m:
            candidate = m.group(1) if pat is _JSON_FENCE_RE else m.group(0)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None


def extract_doc_glossary(
    task_dir: str,
    manifest: Manifest | None,
    llm: Any,
    *,
    max_doc_bytes: int = _MAX_DOC_BYTES_DEFAULT,
) -> DocGlossary:
    """LLM-extract a structured glossary from task docs (or cache hit).

    Never raises — failure returns EMPTY_GLOSSARY. Logs nothing on the
    standard logger path; trace logging is caller's job.
    """
    td = Path(task_dir)
    files = _collect_doc_files(td)
    if not files:
        return EMPTY_GLOSSARY

    model_name = getattr(getattr(llm, "_config", None), "model", "unknown")
    key = _cache_key(files, str(model_name))
    cached = _load_cache(td, key)
    if cached is not None:
        return cached

    payload, paths_used = _read_and_concat(files, max_doc_bytes, task_root=task_dir)
    if not payload.strip():
        return EMPTY_GLOSSARY

    # Manifest basenames help the LLM avoid inventing column references.
    basenames: list[str] = []
    if manifest is not None:
        for entry in manifest.entries:
            basenames.append(Path(entry.file_path).name)
    basenames_line = ", ".join(sorted(set(basenames))) or "(no manifest available)"

    try:
        system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("doc_glossary: cannot read prompt template: %s", e)
        return EMPTY_GLOSSARY

    user_prompt = (
        f"Task data files (for reference; do not invent columns outside this list):\n"
        f"{basenames_line}\n\n"
        f"Documentation (concatenated, with file headers):\n"
        f"{payload}"
    )

    try:
        response = llm.chat(system_prompt, user_prompt)
    except Exception as e:
        logger.warning("doc_glossary: LLM call failed: %s", e)
        return EMPTY_GLOSSARY

    parsed = _parse_llm_json(response)
    if parsed is None:
        return EMPTY_GLOSSARY
    if not isinstance(parsed, dict):
        return EMPTY_GLOSSARY
    parsed.setdefault("source_files", paths_used)

    glossary = _from_dict(parsed)
    _save_cache(td, key, glossary)
    return glossary


# ── Question-glossary matching ─────────────────────────────────────

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "have", "has", "had", "their",
    "more", "than", "less", "into", "onto", "over", "many", "much",
    "this", "that", "these", "those", "what", "when", "where",
    "which", "while", "yet", "but", "are", "was", "were", "been", "being",
    "level", "value", "values", "kind", "kinds", "type", "types",
    "give", "show", "find", "tell", "average", "total", "count",
})


def _tokenise(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())) - _STOPWORDS


def _question_phrases(question: str) -> list[str]:
    # Local extraction — independent of focus_hints (avoid coupling).
    quoted = re.findall(r'["“”\'‘’]([^"“”\'‘’\n]{2,80})["“”\'‘’]', question)
    caps = re.findall(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)+)\b", question)
    # short lowercase noun phrases
    lower_phrases: list[str] = []
    tokens = re.findall(r"[a-z]{3,}", question.lower())
    for size in (3, 2):
        for i in range(len(tokens) - size + 1):
            window = tokens[i:i + size]
            if any(w in _STOPWORDS for w in window):
                continue
            lower_phrases.append(" ".join(window))
    # Dedup preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for ph in (quoted + caps + lower_phrases):
        if ph and ph.lower() not in seen:
            seen.add(ph.lower())
            out.append(ph)
    return out


def _format_term_hint(t: TermDefV2) -> str:
    parts = [f"'{t.name}'"]
    if t.data_field_column:
        col = f"{t.data_field_table}:{t.data_field_column}" if t.data_field_table else t.data_field_column
        parts.append(f"→ column `{col}`")
    if t.definition:
        parts.append(f"— {t.definition[:140].rstrip()}.")
    if t.value_enum:
        vals = ", ".join(f"{v}={m}" for v, m in t.value_enum[:4])
        parts.append(f"Values: {vals}.")
    if t.source_section:
        parts.append(f"[{t.source_section}]")
    return "- " + " ".join(parts)


def _format_formula_hint(f: FormulaDef) -> str:
    inputs = f", inputs: {', '.join(f.inputs[:5])}" if f.inputs else ""
    return f"- Formula '{f.name}': {f.expression[:140]}{inputs}"


def _format_rule_hint(r: RuleDef) -> str:
    return f"- Rule: {r.statement[:160]}"


def build_doc_glossary_hints(question: str, glossary: DocGlossary) -> str:
    """Deterministic match → ≤ 12-line hint block. Never raises."""
    if not glossary or not glossary.terms:
        return ""

    q_text = question.lower()
    q_tokens = _tokenise(question)
    q_phrases = _question_phrases(question)

    matched_term_names: set[str] = set()
    scored: list[tuple[float, str, str]] = []  # (score, kind, line) — kind for dedup

    # ── Term matches ───────────────────────────────────────────────
    for t in glossary.terms:
        score = 0.0
        # 1. exact phrase / alias / column → strong
        candidates = [t.name, *t.aliases]
        if t.data_field_column:
            candidates.append(t.data_field_column)
        hit_exact = False
        for cand in candidates:
            if not cand:
                continue
            c = cand.lower().strip()
            if not c:
                continue
            if c in q_text or any(c == ph.lower() for ph in q_phrases):
                score += 3.0
                hit_exact = True
                break
        # 2. token overlap with name
        if not hit_exact:
            name_tokens = set(re.findall(r"[a-z0-9_]{3,}", t.name.lower()))
            if name_tokens & q_tokens:
                score += 1.0
        # 3. definition-body overlap (gated — only when nothing stronger)
        if score == 0.0 and t.definition:
            def_tokens = set(re.findall(r"[a-z0-9_]{4,}", t.definition.lower()))
            overlap = (def_tokens & q_tokens) - _STOPWORDS
            if len(overlap) >= 2:
                score += 0.5
        if score >= 1.0:
            scored.append((score, "term:" + t.name.lower(), _format_term_hint(t)))
            matched_term_names.add(t.name)

    # ── Formula matches ────────────────────────────────────────────
    for f in glossary.formulas:
        name_tokens = set(re.findall(r"[a-z0-9_]{3,}", f.name.lower()))
        inputs_lc = [i.lower() for i in f.inputs]
        if (name_tokens & q_tokens) or any(i and i in q_text for i in inputs_lc):
            scored.append((2.5, "formula:" + f.name.lower(), _format_formula_hint(f)))

    # ── Rule matches (only if mentions any matched term) ──────────
    for r in glossary.rules:
        rs_lower = r.statement.lower()
        if any(tn.lower() in rs_lower for tn in matched_term_names):
            scored.append((1.5, "rule:" + r.statement.lower()[:40], _format_rule_hint(r)))

    if not scored:
        return ""

    # Rank, dedup by kind+line, cap.
    scored.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, kind, line in scored:
        if kind in seen or line in out:
            continue
        seen.add(kind)
        out.append(line)
        if len(out) >= 12:
            break
    return "\n".join(out)
