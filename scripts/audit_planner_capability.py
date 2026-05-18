"""Phase 0.7 + 0.8 — classify v80 failures + extract Planner capability gaps.

Phase 0.7: classify each v80 failure (score < 1.0) into:
  A. Silent acceptance — Judge said `finish` early, score = 0.
                          Planner had no chance to adapt.
  B. Persistent struggle — Multiple iter, Judge said `continue` >= 2 times,
                            score = 0. Planner had hints but couldn't fix.
  C. Code-error exhaustion — never produced a valid final code.
  D. Other.

Phase 0.8: for Category B tasks, dump:
  - question
  - sequence of Judge `continue` reasoning across iters
  - final code (what Planner ended up submitting)
  - presence of relevant helpers in dataline/helpers/data_helpers.py that
    might have addressed Judge's pointed concern

Output: docs/AUDIT_PLANNER_CAPABILITY.md — qualitative table + per-task detail
        so we can read and propose what Planner technical capability is missing.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "results" / "eval_v80_fallback_20260516_0039"
HELPERS_FILE = REPO_ROOT / "dataline" / "helpers" / "data_helpers.py"
OUT = REPO_ROOT / "docs" / "AUDIT_PLANNER_CAPABILITY.md"

# Helper name → keyword Judge guidance would mention if this helper applies.
# Heuristic: if Judge says "json nested", helper safe_read_json_df might help.
HELPER_TRIGGERS = {
    "safe_read_json_df":     ["json", "nested", "records key", "table key"],
    "parse_jsonish_column":  ["dict string", "json column", "stringified"],
    "explode_jsonish_column": ["array column", "list column", "explode"],
    "coerce_numeric_id_columns": ["id type mismatch", "type coercion", "join key type"],
    "join_with_type_coercion":   ["join failed", "no matching rows", "type mismatch on join"],
    "safe_read_pdf":         ["pdf", "scanned document"],
    "safe_read_image":       ["image", "ocr"],
    "safe_extract_tables":   ["table extraction", "tables from"],
    "find_join_keys":        ["foreign key", "join key", "relationship"],
    "detect_date_columns":   ["date column", "timestamp"],
    "clean_numeric":         ["numeric format", "embellishment", "$ %"],
}


def load_task(task_id: str) -> dict | None:
    """Best-effort load: trace.json + workspace codes + final stdout."""
    p = EVAL_DIR / task_id
    tp = p / "trace.json"
    if not tp.exists():
        return None
    try:
        trace = json.loads(tp.read_text())
    except Exception:
        return None
    codes = {}
    steps_dir = p / "workspace" / "steps"
    if steps_dir.exists():
        for cf in sorted(steps_dir.glob("step_*_code.*")):
            try:
                idx = int(cf.stem.split("_")[1])
                codes[idx] = cf.read_text()
            except Exception:
                pass
    return {"trace": trace, "codes": codes}


def judge_messages(trace_top: list[dict]) -> list[str]:
    """Extract per-iter Judge reasoning strings (chronological)."""
    out = []
    for e in trace_top:
        if e.get("agent") != "judge":
            continue
        msg = e.get("message", "")
        if not isinstance(msg, str):
            continue
        m = re.search(r"Reasoning:\s*(.+?)(?:\s*\||\s*$)", msg, re.DOTALL)
        if m:
            out.append(m.group(1).strip()[:400])
    return out


def classify(task: dict, score: float) -> str:
    iters = task["trace"].get("observations", {}).get("iterations", []) or []
    if not iters:
        return "D"
    final = iters[-1]
    final_action = (final.get("judge_action") or "").lower()
    final_code_ok = final.get("code_success", True)
    n = len(iters)

    if not final_code_ok and score < 1.0:
        return "C"
    if final_action.startswith("finish") and n <= 2 and score < 1.0:
        return "A"  # Silent acceptance
    if n >= 2 and score < 1.0:
        # Multi-iter and final says finish → also struggle
        return "B"
    return "D"


def helper_opportunities(judge_texts: list[str]) -> list[str]:
    """For each Judge text, list helpers whose triggers match."""
    hits = set()
    for txt in judge_texts:
        lower = txt.lower()
        for helper, triggers in HELPER_TRIGGERS.items():
            if any(trig in lower for trig in triggers):
                hits.add(helper)
    return sorted(hits)


def helper_referenced_in_code(code: str) -> list[str]:
    """Helpers actually used in the final code."""
    if not code:
        return []
    refs = set()
    for h in HELPER_TRIGGERS.keys():
        if h in code:
            refs.add(h)
    return sorted(refs)


def main():
    report_path = EVAL_DIR / "eval_report_kdd.json"
    rpt = json.loads(report_path.read_text())

    rows_a: list[dict] = []
    rows_b: list[dict] = []
    rows_c: list[dict] = []
    rows_d: list[dict] = []
    counts = Counter()

    for ts in rpt.get("task_scores", []):
        tid = ts.get("task_id", "")
        if not tid or "__" in tid:
            continue
        score = ts.get("score", 0.0)
        if score >= 1.0:
            continue
        task = load_task(tid)
        if not task:
            continue
        cat = classify(task, score)
        counts[cat] += 1
        iters = task["trace"].get("observations", {}).get("iterations", []) or []
        judge_texts = judge_messages(task["trace"].get("trace", []))
        final_code = task["codes"].get(max(task["codes"]), "") if task["codes"] else ""
        opportunities = helper_opportunities(judge_texts)
        used = helper_referenced_in_code(final_code)
        rec = {
            "task_id": tid,
            "difficulty": ts.get("difficulty", ""),
            "n_iters": len(iters),
            "question": task["trace"].get("question", "")[:200],
            "last_judge_msg": (judge_texts[-1] if judge_texts else "")[:300],
            "final_code_head": final_code[:240] if final_code else "",
            "helper_opportunities": opportunities,
            "helpers_used": used,
        }
        {"A": rows_a, "B": rows_b, "C": rows_c, "D": rows_d}[cat].append(rec)

    # Render
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Planner Capability Audit — v80 failures only",
        "",
        "_Generated by `scripts/audit_planner_capability.py`._",
        "",
        "## Headline counts",
        "",
        f"- A (silent acceptance, ≤2 iter + judge=finish): **{counts['A']}**",
        f"- B (persistent struggle, multi-iter, judge guidance ignored): **{counts['B']}**",
        f"- C (code-error exhaustion): **{counts['C']}**",
        f"- D (other/edge): **{counts['D']}**",
        "",
        "Each category needs a different intervention:",
        "- A → stronger pre-finish gate (Skeptic-veto style; HarnessGate coverage)",
        "- B → Planner technical capability (helper signposting; concrete recipes)",
        "- C → Debugger upgrades or Planner robustness",
        "",
        "---",
        "",
    ]

    def render_section(title: str, recs: list[dict]):
        lines.append(f"## {title} — {len(recs)} tasks")
        if not recs:
            lines.append("(none)\n")
            return
        for r in recs:
            lines.extend([
                "",
                f"### {r['task_id']} ({r['difficulty']}, {r['n_iters']} iter)",
                f"**Q**: {r['question']}",
                "",
                f"**Last Judge reasoning** (truncated): {r['last_judge_msg']}",
                "",
                f"**Final code (first 240 chars)**:",
                "```",
                r["final_code_head"],
                "```",
                f"**Helper opportunities** (by keyword match): "
                f"`{', '.join(r['helper_opportunities']) or '(none detected)'}`",
                f"**Helpers actually used in final code**: "
                f"`{', '.join(r['helpers_used']) or '(none)'}`",
                "",
            ])

    render_section("Category A — Silent Acceptance", rows_a)
    lines.append("\n---\n")
    render_section("Category B — Persistent Struggle (KEY TO DIG)", rows_b)
    lines.append("\n---\n")
    render_section("Category C — Code-Error Exhaustion", rows_c)
    lines.append("\n---\n")
    render_section("Category D — Other", rows_d)

    OUT.write_text("\n".join(lines))
    print(f"Wrote {OUT}")
    print()
    print(f"A={counts['A']}  B={counts['B']}  C={counts['C']}  D={counts['D']}")
    print(f"Total failures classified: {sum(counts.values())}")


if __name__ == "__main__":
    main()
