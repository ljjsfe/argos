"""H1 prototype — deterministic question entity → manifest hint.

Given a task directory and the manifest, extract:
  - quoted entities from question ("October Meeting")
  - capitalized phrases ("Yearly Kickoff", "Alex Yoong")
  - numeric thresholds (29.00, 70, 2008)

Then match against manifest:
  - distinct values of columns
  - column names
  - knowledge.md / domain-rule terms

Emit a FOCUS_HINTS section that maps question concepts to data anchors.

0 LLM cost. Pure regex + manifest lookup. Universal across benchmarks.

Run:
  python scripts/h1_focus_hints_prototype.py --task public/input/task_352
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dataline.profiler.manifest import scan
from dataline.core.types import Manifest, ManifestEntry


# Entity extraction patterns (universal, language-aware-but-simple).
QUOTED_RE = re.compile(r'["“”\'‘’]([^"“”\'‘’\n]{2,80})["“”\'‘’]')
CAPITAL_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)+)\b")
NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")
# Standalone capitalised single word (e.g. "Advertisement", "Phosphorus"). Skip
# common English question words.
QUESTION_STOPWORDS = {
    "Which", "What", "Where", "When", "Who", "How", "Among", "For", "From",
    "Identify", "Calculate", "Tally", "Give", "Show", "Find", "List",
    "Please", "The", "Yet", "Yet?",
}
SINGLE_CAPITAL_RE = re.compile(r"\b([A-Z][a-z]{3,})\b")


def extract_entities(question: str) -> dict[str, list]:
    """Pull candidate entities from a natural-language question."""
    quoted = [m.group(1).strip() for m in QUOTED_RE.finditer(question)]
    cap_phrases = [m.group(1).strip() for m in CAPITAL_PHRASE_RE.finditer(question)]
    singles = [
        m.group(1)
        for m in SINGLE_CAPITAL_RE.finditer(question)
        if m.group(1) not in QUESTION_STOPWORDS
    ]
    numbers = [m.group(1) for m in NUMBER_RE.finditer(question)]
    # De-dup, preserve order.
    def dedup(xs):
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    return {
        "quoted": dedup(quoted),
        "capital_phrases": dedup(cap_phrases),
        "capital_singles": dedup(singles),
        "numbers": dedup(numbers),
    }


def _column_value_pool(col: dict) -> list:
    """Pull values from whichever keys the reader populated."""
    pool: list = []
    for v in col.get("top_values", []) or []:
        if isinstance(v, dict) and "value" in v:
            pool.append(v["value"])
        else:
            pool.append(v)
    pool.extend(col.get("sample", []) or [])
    repr_dict = col.get("value_repr") or {}
    pool.extend(repr_dict.get("sample", []) or [])
    return pool


def _load_full_distinct_if_low_cardinality(col_meta: dict, file_path: str, threshold: int = 60) -> list:
    """For low-cardinality string columns we live-load full distinct values
    from disk. Profiler's top_values only covers top-5 — insufficient for
    matching arbitrary question entities."""
    card = col_meta.get("cardinality") or (col_meta.get("value_repr") or {}).get("cardinality") or 0
    dtype = col_meta.get("dtype", "")
    if not (1 < card <= threshold and dtype in ("object", "string", "str")):
        return []
    name = col_meta.get("name")
    if not name:
        return []
    try:
        # CSV / JSON live-load. SQLite is left to manifest sample for now.
        suffix = Path(file_path).suffix.lower()
        if suffix in (".csv", ".tsv"):
            import pandas as pd
            df = pd.read_csv(file_path, usecols=[name])
            return sorted(set(str(v) for v in df[name].dropna()))
        if suffix == ".json":
            import json, pandas as pd
            obj = json.loads(Path(file_path).read_text())
            if isinstance(obj, dict) and "records" in obj:
                obj = obj["records"]
            if isinstance(obj, list):
                df = pd.DataFrame(obj)
                if name in df.columns:
                    return sorted(set(str(v) for v in df[name].dropna()))
    except Exception:
        pass
    return []


def _collect_columns_and_values(manifest: Manifest) -> dict:
    """Walk manifest, flatten column metadata into:
    {column_full_name -> {table, dtype, values, file}}
    """
    out: dict = {}
    for entry in manifest.entries:
        s = entry.summary
        file_basename = Path(entry.file_path).name
        # Top-level columns (csv, json)
        for col in s.get("columns", []):
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
                "dtype": col.get("dtype") or col.get("type", "?"),
                "distinct_values": values,
                "file": entry.file_path,
            }
        # SQLite tables
        for table in s.get("tables", []):
            tname = table.get("name", "?")
            for col in table.get("columns", []):
                name = col.get("name", "")
                if not name:
                    continue
                values = _column_value_pool(col)
                out[f"{tname}::{name}"] = {
                    "table": tname,
                    "column": name,
                    "dtype": col.get("dtype") or col.get("type", "?"),
                    "distinct_values": values,
                    "file": entry.file_path,
                }
        # Excel sheets
        for sheet in s.get("sheets", []):
            sname = sheet.get("name", "?")
            for col in sheet.get("columns", []):
                name = col.get("name", "")
                if not name:
                    continue
                values = _column_value_pool(col)
                out[f"{sname}::{name}"] = {
                    "table": sname,
                    "column": name,
                    "dtype": col.get("dtype") or col.get("type", "?"),
                    "distinct_values": values,
                    "file": entry.file_path,
                }
    return out


def match_entity_to_columns(entity: str, columns: dict) -> list[dict]:
    """Return matches where entity appears in column NAME or DISTINCT VALUES."""
    e_lower = entity.lower()
    matches = []
    for key, col in columns.items():
        # 1. Exact value match in distinct_values
        for v in col["distinct_values"] or []:
            if isinstance(v, (str, int, float)) and str(v).lower() == e_lower:
                matches.append({
                    "match_type": "value",
                    "column": key,
                    "value": v,
                    "evidence": f"{key} contains value '{v}'",
                })
                break
        else:
            # 2. Column-name contains entity word
            col_name_lower = col["column"].lower()
            if e_lower in col_name_lower or col_name_lower in e_lower:
                # avoid trivial 1-2 char overlap
                if len(set(e_lower) & set(col_name_lower)) >= 3:
                    matches.append({
                        "match_type": "column_name",
                        "column": key,
                        "evidence": f"column name '{col['column']}' overlaps entity '{entity}'",
                    })
    return matches


def build_focus_hints(task_dir: str) -> str:
    """Run extraction + matching, return a printable FOCUS_HINTS block."""
    task_path = Path(task_dir)
    task_json = task_path / "task.json"
    question = ""
    if task_json.exists():
        import json
        try:
            question = json.loads(task_json.read_text()).get("question", "")
        except Exception:
            pass

    if not question:
        return "(no question found)"

    manifest = scan(str(task_path))
    columns = _collect_columns_and_values(manifest)

    entities = extract_entities(question)
    all_entities = (
        entities["quoted"]
        + entities["capital_phrases"]
        + entities["capital_singles"]
        + entities["numbers"]
    )

    lines = [
        "# FOCUS_HINTS — auto-derived from question + manifest (deterministic)",
        f"Question: {question}",
        f"Entities: quoted={entities['quoted']} caps={entities['capital_phrases']} singles={entities['capital_singles']} nums={entities['numbers']}",
        "",
    ]

    hits_found = False
    for entity in all_entities:
        ms = match_entity_to_columns(entity, columns)
        if ms:
            hits_found = True
            lines.append(f"  '{entity}':")
            for m in ms[:3]:  # cap matches per entity
                lines.append(f"    - {m['evidence']}")
    if not hits_found:
        lines.append("  (no entity↔column matches found)")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="path to task directory")
    args = ap.parse_args()
    print(build_focus_hints(args.task))


if __name__ == "__main__":
    main()
