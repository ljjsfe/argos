"""Deterministic question-entity → manifest binding.

For each natural-language question, extract candidate entities (quoted
phrases, capitalised proper nouns, key numbers) and match them against
columns and DISTINCT VALUES in the profiled manifest. Produce a focused
"FOCUS_HINTS" text block that tells PlannerCoder which columns and values
to anchor on.

This is the honest substitute for the filename-as-hint mechanism that
previously leaked through polluted inputs (see docs/LEAK_TO_HONEST_INFO_MAP.md):

  Question "How many times was Advertisement for 'Yearly Kickoff'..."
    → 'Yearly Kickoff' / 'October Meeting' / 'Advertisement' extracted
    → matched against event.csv::event_name DISTINCT values
    → FOCUS_HINTS:
        - 'Yearly Kickoff' is a value in event.csv:event_name
        - 'October Meeting' is a value in event.csv:event_name
        - 'Advertisement' matches budget.csv:category column

Zero LLM cost. Universal across benchmarks. Works for any data agent
because the mechanism only uses (a) the question text and (b) the
existing Profiler output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from ..core.types import Manifest, ManifestEntry


# Patterns ────────────────────────────────────────────────────────────
QUOTED_RE = re.compile(r'["“”\'‘’]([^"“”\'‘’\n]{2,80})["“”\'‘’]')
CAPITAL_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)+)\b")
SINGLE_CAPITAL_RE = re.compile(r"\b([A-Z][a-z]{3,})\b")
NUMBER_RE = re.compile(r"\b(\d{2,}(?:\.\d+)?|\d\.\d+)\b")

# Common question-stem words that match the capital-single regex but are
# not informative entities.
QUESTION_STOPWORDS = frozenset({
    "Which", "What", "Where", "When", "Who", "Why", "How", "Among", "Above",
    "Below", "Between", "Across", "Against", "After", "Before", "During",
    "From", "For", "Into", "Onto", "Over", "Through", "With", "Without",
    "Identify", "Calculate", "Tally", "Give", "Show", "Find", "List",
    "Please", "The", "Their", "There", "These", "Those", "This", "That",
    "Year", "Month", "Day", "January", "February", "March", "April", "May",
    "June", "July", "August", "September", "October", "November", "December",
})


# Public API ──────────────────────────────────────────────────────────

def extract_entities(question: str) -> dict[str, list[str]]:
    """Pull candidate entities from a natural-language question.

    Three buckets — quoted, capitalised phrases (multi-word), capital
    singles — plus numbers. Quoted and capital_phrases are the most
    valuable; singles get filtered against stopwords.
    """
    quoted = [m.group(1).strip() for m in QUOTED_RE.finditer(question)]
    cap_phrases = [m.group(1).strip() for m in CAPITAL_PHRASE_RE.finditer(question)]
    singles = [
        m.group(1) for m in SINGLE_CAPITAL_RE.finditer(question)
        if m.group(1) not in QUESTION_STOPWORDS
    ]
    numbers = [m.group(1) for m in NUMBER_RE.finditer(question)]

    def _dedup(xs: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in xs:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return {
        "quoted": _dedup(quoted),
        "capital_phrases": _dedup(cap_phrases),
        "capital_singles": _dedup(singles),
        "numbers": _dedup(numbers),
    }


def build_focus_hints(question: str, manifest: Manifest) -> str:
    """Return a FOCUS_HINTS block (string) for the planner_coder context.

    Empty string ⇒ no useful hints; the section should be omitted entirely
    from the prompt.

    Universal: only depends on `question` text + Profiler `manifest` output.
    """
    if not question or manifest is None or not manifest.entries:
        return ""

    entities = extract_entities(question)
    columns = _collect_columns_and_values(manifest)
    if not columns:
        return ""

    # Match each entity bucket against column values / column names.
    matches: list[tuple[str, str]] = []  # (entity, evidence-line)
    for ent in entities["quoted"] + entities["capital_phrases"]:
        hits = _match_entity(ent, columns, prefer_value=True)
        for h in hits[:2]:
            matches.append((ent, h))

    # Singles: only emit when they hit a value EXACTLY (not just column
    # name) — singles are noisier and risk false positives.
    for ent in entities["capital_singles"]:
        hits = _match_entity(ent, columns, prefer_value=True, values_only=True)
        for h in hits[:1]:
            matches.append((ent, h))

    # Numbers: only match if they appear as DISTINCT values (and length≥2),
    # and there are few matching columns — otherwise the hint is spam.
    for ent in entities["numbers"]:
        hits = _match_entity(ent, columns, prefer_value=True, values_only=True)
        if 1 <= len(hits) <= 2:  # noise filter
            for h in hits:
                matches.append((ent, h))

    if not matches:
        return ""

    # De-dup (entity, evidence) pairs preserving order; cap total lines so
    # the section stays compact in the context budget.
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for ent, ev in matches:
        key = (ent, ev)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- '{ent}' → {ev}")
        if len(lines) >= 12:
            break

    return "\n".join(lines)


# Internal ────────────────────────────────────────────────────────────

def _column_value_pool(col: dict) -> list:
    pool: list = []
    for v in col.get("top_values") or []:
        if isinstance(v, dict) and "value" in v:
            pool.append(v["value"])
        else:
            pool.append(v)
    pool.extend(col.get("sample") or [])
    repr_dict = col.get("value_repr") or {}
    pool.extend(repr_dict.get("sample") or [])
    return pool


def _load_full_distinct_if_low_cardinality(
    col_meta: dict, file_path: str, threshold: int = 60,
) -> list:
    """Live-load full distinct values for low-cardinality string columns.
    Profiler only emits top-5 sample values — insufficient for matching
    arbitrary question entities. We cap at `threshold` distinct values to
    avoid loading large free-text columns.
    """
    card = (
        col_meta.get("cardinality")
        or (col_meta.get("value_repr") or {}).get("cardinality")
        or 0
    )
    dtype = col_meta.get("dtype", "")
    if not (1 < card <= threshold and dtype in ("object", "string", "str")):
        return []
    name = col_meta.get("name")
    if not name:
        return []
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix in (".csv", ".tsv"):
            import pandas as pd
            df = pd.read_csv(file_path, usecols=[name])
            return sorted(set(str(v) for v in df[name].dropna()))
        if suffix == ".json":
            import pandas as pd
            obj = json.loads(Path(file_path).read_text())
            if isinstance(obj, dict) and "records" in obj:
                obj = obj["records"]
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                df = pd.DataFrame(obj)
                if name in df.columns:
                    return sorted(set(str(v) for v in df[name].dropna()))
    except Exception:
        # Silent skip — live-load failures should never break the agent.
        return []
    return []


def _collect_columns_and_values(manifest: Manifest) -> dict[str, dict]:
    """Flatten manifest entries into a single column dictionary.

    Key = `<file_basename>::<column_name>` so the same column name from
    different files is disambiguated.
    """
    out: dict[str, dict] = {}
    for entry in manifest.entries:
        s = entry.summary
        file_basename = Path(entry.file_path).name
        # Top-level CSV/JSON columns
        for col in s.get("columns", []) or []:
            name = col.get("name", "")
            if not name:
                continue
            values = _column_value_pool(col)
            extra = _load_full_distinct_if_low_cardinality(col, entry.file_path)
            if extra:
                values = list({*values, *extra})
            out[f"{file_basename}::{name}"] = {
                "table": file_basename,
                "column": name,
                "dtype": col.get("dtype") or "?",
                "distinct_values": values,
            }
        # SQLite tables (no live-load — schema is in manifest already)
        for table in s.get("tables", []) or []:
            tname = table.get("name", "?")
            for col in table.get("columns", []) or []:
                name = col.get("name", "")
                if not name:
                    continue
                out[f"{tname}::{name}"] = {
                    "table": tname,
                    "column": name,
                    "dtype": col.get("dtype") or "?",
                    "distinct_values": _column_value_pool(col),
                }
        # Excel sheets
        for sheet in s.get("sheets", []) or []:
            sname = sheet.get("name", "?")
            for col in sheet.get("columns", []) or []:
                name = col.get("name", "")
                if not name:
                    continue
                out[f"{sname}::{name}"] = {
                    "table": sname,
                    "column": name,
                    "dtype": col.get("dtype") or "?",
                    "distinct_values": _column_value_pool(col),
                }
    return out


def _match_entity(
    entity: str,
    columns: dict[str, dict],
    *,
    prefer_value: bool = True,
    values_only: bool = False,
) -> list[str]:
    """Return evidence lines (short strings) describing column matches."""
    e_lower = entity.lower().strip()
    if not e_lower:
        return []

    results: list[str] = []
    for key, col in columns.items():
        # 1. EXACT value match (strongest evidence)
        matched_value = None
        for v in col.get("distinct_values") or []:
            try:
                if str(v).strip().lower() == e_lower:
                    matched_value = v
                    break
            except Exception:
                continue
        if matched_value is not None:
            results.append(
                f"value '{matched_value}' in {col['table']}:{col['column']}"
            )
            continue

        # 2. Column name overlap (weaker; gated to avoid trivial matches)
        if values_only:
            continue
        cname = col.get("column", "")
        cname_lower = cname.lower()
        if not cname_lower:
            continue
        # require ≥3 shared characters AND substring relationship in either
        # direction to avoid spurious matches like 'a' vs 'apple'.
        if (e_lower in cname_lower or cname_lower in e_lower) and (
            len(set(e_lower) & set(cname_lower)) >= 3
        ):
            results.append(
                f"column name {col['table']}:{col['column']} overlaps"
            )
    return results
