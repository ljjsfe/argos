"""Audit v80 50-task baseline for evidence per candidate improvement direction.

Each audit is a v80-only pass — never reuses old `mine_failure_patterns.py`
(which mixed 70 evals together). The goal: tell ourselves which directions
have ≥5 affected tasks (worth investing) vs which have <5 (overfitting risk).

Phase 0.4 — Seven direction audits (deterministic patterns):
  D1 filter-no-effect       — Task #4 (Phase 1.1)
  D2 wrong-column-name      — Task #13 (Phase 1.2)
  D3 limit1-multirow        — Task #23 (Phase 0.2-gated)
  D4 where-no-null-guard    — Task #22 (Phase 0.2-gated)
  D5 count-without-distinct — Task #24 (Phase 0.2-gated)
  D6 mode-b-iter-exhaust    — Tasks #5/#15 (Planner direction signal)
  D7 stuck-loop             — Task #15 / new (Planner repeats code)

Phase 0.6 — Planner-Judge coupling:
  C1 same-code-across-iters
  C2 same-harness-flags-across-iters
  C3 retry-recovery rate (when judge=continue, does next iter's result
     differ from last iter's)

Output: docs/AUDIT_DIRECTIONS.md
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = REPO_ROOT / "results" / "eval_v80_fallback_20260516_0039"
TASK_INPUT_DIR = REPO_ROOT / "public" / "input"
OUTPUT_PATH = REPO_ROOT / "docs" / "AUDIT_DIRECTIONS.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_task(task_id: str) -> dict | None:
    """Load trace + workspace state for one v80 task."""
    p = EVAL_DIR / task_id
    if not p.exists():
        return None
    trace_path = p / "trace.json"
    if not trace_path.exists():
        return None
    try:
        trace = json.loads(trace_path.read_text())
    except Exception:
        return None
    steps_dir = p / "workspace" / "steps"
    codes: list[tuple[int, str]] = []
    if steps_dir.exists():
        for cf in sorted(steps_dir.glob("step_*_code.*")):
            try:
                idx = int(cf.stem.split("_")[1])
                codes.append((idx, cf.read_text()))
            except Exception:
                continue
    return {"trace": trace, "codes": codes, "task_id": task_id}


def task_failed(score: float) -> bool:
    return score < 1.0


# ---------------------------------------------------------------------------
# D1 — filter no effect
# ---------------------------------------------------------------------------

def audit_d1_filter_no_effect(task: dict, score: float) -> bool:
    """Final code has a SQL/pandas filter but result row count == manifest table size."""
    if not task["codes"]:
        return False
    final_code = task["codes"][-1][1]
    # Heuristic: code has WHERE or [df.something>...] AND structured json
    has_filter = bool(re.search(r"\bWHERE\b|\.loc\[|df\[df", final_code, re.IGNORECASE))
    if not has_filter:
        return False
    # Look for "Loaded X rows" or "After filter: Y rows" patterns
    iters = task["trace"].get("observations", {}).get("iterations", []) or []
    if not iters:
        return False
    stdout = iters[-1].get("stdout_preview") or ""
    # Detect: result row count == an earlier loaded count
    rows = [int(m) for m in re.findall(r"(\d+)\s*rows?", stdout, re.IGNORECASE)]
    return len(set(rows)) == 1 and len(rows) >= 2  # same number twice


# ---------------------------------------------------------------------------
# D2 — wrong column name (already known: 1 task in v80)
# ---------------------------------------------------------------------------

WARNING_BLOCK_RE = re.compile(
    r"# === CODE VALIDATOR WARNINGS ===\s*(.*?)(?=\n\n|\n[a-zA-Z])", re.DOTALL
)


def audit_d2_wrong_column(task: dict, score: float) -> bool:
    if not task["codes"]:
        return False
    final_code = task["codes"][-1][1]
    return bool(WARNING_BLOCK_RE.search(final_code))


# ---------------------------------------------------------------------------
# D3 — LIMIT 1 + multi-row expected
# ---------------------------------------------------------------------------

LIMIT1_RE = re.compile(r"\bLIMIT\s+1\b|\.head\s*\(\s*1\s*\)|\.iloc\[\s*0\s*\]", re.IGNORECASE)


def audit_d3_limit1_multirow(task: dict, score: float) -> bool:
    if not task["codes"]:
        return False
    final_code = task["codes"][-1][1]
    if not LIMIT1_RE.search(final_code):
        return False
    qs = task["trace"].get("observations", {}).get("question_spec", {})
    expected = qs.get("expected_row_count", "")
    return expected == "multiple" or qs.get("answer_type") == "list"


# ---------------------------------------------------------------------------
# D4 — WHERE = literal on potentially-NULL column without IS NOT NULL
# ---------------------------------------------------------------------------

WHERE_EQ_RE = re.compile(r"WHERE\b[^;]*?\b(\w+)\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE)
IS_NOT_NULL_RE = re.compile(r"IS\s+NOT\s+NULL", re.IGNORECASE)


def audit_d4_where_no_null_guard(task: dict, score: float) -> bool:
    """SQL has WHERE col = 'literal' on a column the profiler flagged with NULLs,
    and no IS NOT NULL guard in the same WHERE clause."""
    if not task["codes"]:
        return False
    final_code = task["codes"][-1][1]
    where_match = WHERE_EQ_RE.search(final_code)
    if not where_match:
        return False
    if IS_NOT_NULL_RE.search(final_code):
        return False  # guard exists
    # Conservative — without manifest cross-check, count any WHERE = literal as
    # potential candidate. Real implementation would gate on null% > 0 column.
    return True


# ---------------------------------------------------------------------------
# D5 — COUNT(*) when question implies COUNT(DISTINCT ...)
# ---------------------------------------------------------------------------

COUNT_STAR_RE = re.compile(r"COUNT\s*\(\s*\*\s*\)", re.IGNORECASE)
COUNT_DISTINCT_RE = re.compile(r"COUNT\s*\(\s*DISTINCT", re.IGNORECASE)


def audit_d5_count_no_distinct(task: dict, score: float) -> bool:
    if not task["codes"]:
        return False
    final_code = task["codes"][-1][1]
    if not COUNT_STAR_RE.search(final_code):
        return False
    if COUNT_DISTINCT_RE.search(final_code):
        return False
    question = task["trace"].get("question", "").lower()
    return any(kw in question for kw in ["unique", "distinct", "different"])


# ---------------------------------------------------------------------------
# D6 — Mode B: 8 iter used + still partial / failed
# ---------------------------------------------------------------------------

def audit_d6_mode_b(task: dict, score: float) -> bool:
    iters = task["trace"].get("observations", {}).get("iterations", []) or []
    return len(iters) >= 8 and score < 1.0


# ---------------------------------------------------------------------------
# D7 — Stuck loop: two consecutive iters with ≥90% identical code
# ---------------------------------------------------------------------------

def audit_d7_stuck_loop(task: dict, score: float) -> tuple[bool, float]:
    codes = task["codes"]
    if len(codes) < 2:
        return False, 0.0
    max_sim = 0.0
    for i in range(1, len(codes)):
        sim = difflib.SequenceMatcher(None, codes[i - 1][1], codes[i][1]).ratio()
        max_sim = max(max_sim, sim)
    return max_sim >= 0.9, max_sim


# ---------------------------------------------------------------------------
# C1/C2/C3 — Planner-Judge coupling
# ---------------------------------------------------------------------------

def audit_coupling(task: dict) -> dict:
    """For each (judge=continue → next iter) pair, classify Planner response."""
    iters = task["trace"].get("observations", {}).get("iterations", []) or []
    codes = {idx: code for idx, code in task["codes"]}

    same_code = 0
    same_flags = 0
    pairs = 0
    for i in range(len(iters) - 1):
        cur = iters[i]
        nxt = iters[i + 1]
        if not (cur.get("judge_action") or "").lower().startswith("continue"):
            continue
        pairs += 1
        # Same code?
        cur_idx = cur.get("iteration", i)
        nxt_idx = nxt.get("iteration", i + 1)
        cur_code = codes.get(cur_idx, "")
        nxt_code = codes.get(nxt_idx, "")
        if cur_code and nxt_code:
            sim = difflib.SequenceMatcher(None, cur_code, nxt_code).ratio()
            if sim >= 0.9:
                same_code += 1
        # Same harness flag set?
        cur_flags = frozenset(f.get("rule", "") for f in (cur.get("harness_flags") or []))
        nxt_flags = frozenset(f.get("rule", "") for f in (nxt.get("harness_flags") or []))
        if cur_flags and cur_flags == nxt_flags:
            same_flags += 1
    return {"pairs": pairs, "same_code": same_code, "same_flags": same_flags}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    report_path = EVAL_DIR / "eval_report_kdd.json"
    if not report_path.exists():
        raise SystemExit(f"Missing {report_path}")
    report = json.loads(report_path.read_text())

    tasks_data: dict[str, dict] = {}
    task_meta: dict[str, dict] = {}
    for ts in report.get("task_scores", []):
        tid = ts.get("task_id", "")
        if not tid or "__" in tid:
            continue
        task = load_task(tid)
        if task is None:
            continue
        tasks_data[tid] = task
        task_meta[tid] = ts

    print(f"Loaded {len(tasks_data)} v80 tasks.")

    direction_hits: dict[str, list[tuple[str, float]]] = defaultdict(list)
    coupling_agg = {"pairs": 0, "same_code": 0, "same_flags": 0}
    stuck_sims = []

    for tid, task in tasks_data.items():
        score = task_meta[tid].get("score", 0.0)

        if audit_d1_filter_no_effect(task, score):
            direction_hits["D1_filter_no_effect"].append((tid, score))
        if audit_d2_wrong_column(task, score):
            direction_hits["D2_wrong_column"].append((tid, score))
        if audit_d3_limit1_multirow(task, score):
            direction_hits["D3_limit1_multirow"].append((tid, score))
        if audit_d4_where_no_null_guard(task, score):
            direction_hits["D4_where_no_null_guard"].append((tid, score))
        if audit_d5_count_no_distinct(task, score):
            direction_hits["D5_count_no_distinct"].append((tid, score))
        if audit_d6_mode_b(task, score):
            direction_hits["D6_mode_b"].append((tid, score))
        stuck, sim = audit_d7_stuck_loop(task, score)
        if stuck:
            direction_hits["D7_stuck_loop"].append((tid, score))
            stuck_sims.append((tid, sim, score))

        c = audit_coupling(task)
        coupling_agg["pairs"] += c["pairs"]
        coupling_agg["same_code"] += c["same_code"]
        coupling_agg["same_flags"] += c["same_flags"]

    # ---------- render report ----------
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Direction Audit — v80 Baseline (50 tasks)",
        "",
        "_Generated by `scripts/audit_directions.py`. Re-run to refresh._",
        "",
        "**Rule**: a direction with ≥5 affected tasks is worth investing. "
        "<5 affected = overfitting risk if we tune for it.",
        "",
        "## Phase 0.4 — Direction signal counts",
        "",
        "| ID | Direction | Tasks affected | Failed (score<1) | Justified (≥5)? |",
        "|----|-----------|----------------|-------------------|-----------------|",
    ]
    labels = {
        "D1_filter_no_effect": "Filter no effect (Task #4 Phase 1.1)",
        "D2_wrong_column": "Wrong column name (Task #13 Phase 1.2)",
        "D3_limit1_multirow": "LIMIT 1 on multi-row Q (Task #23)",
        "D4_where_no_null_guard": "WHERE = literal w/o IS NOT NULL (Task #22)",
        "D5_count_no_distinct": "COUNT(*) when distinct implied (Task #24)",
        "D6_mode_b": "Mode B: 8 iter exhausted + partial (Tasks #5/#15)",
        "D7_stuck_loop": "Planner stuck (≥90% same code across iter) (#15/new)",
    }
    for did in ["D1_filter_no_effect", "D2_wrong_column", "D3_limit1_multirow",
                "D4_where_no_null_guard", "D5_count_no_distinct", "D6_mode_b",
                "D7_stuck_loop"]:
        hits = direction_hits.get(did, [])
        failed = sum(1 for _, s in hits if s < 1.0)
        ok = "✅ yes" if len(hits) >= 5 else "❌ no"
        lines.append(f"| {did[:2]} | {labels[did]} | **{len(hits)}** | {failed} | {ok} |")

    lines.extend(["",
                  "## Affected task lists (audit transparency)",
                  ""])
    for did in sorted(direction_hits):
        if direction_hits[did]:
            tids = ", ".join(f"{t}({s:.2f})" for t, s in sorted(direction_hits[did]))
            lines.append(f"- **{did}**: {tids}")
    lines.append("")

    # ---------- Phase 0.6 coupling ----------
    p = coupling_agg["pairs"]
    same_code_rate = (coupling_agg["same_code"] / p * 100) if p else 0
    same_flag_rate = (coupling_agg["same_flags"] / p * 100) if p else 0
    lines.extend([
        "## Phase 0.6 — Planner-Judge coupling",
        "",
        f"Across v80 50 tasks, total `(judge=continue → next iter)` pairs: **{p}**",
        "",
        f"- **C1 same-code rate**: {coupling_agg['same_code']}/{p} = "
        f"**{same_code_rate:.0f}%** — Planner produced nearly identical code "
        "in next iter (≥90% sequence match). High = Planner stuck despite "
        "Judge guidance.",
        f"- **C2 same-harness-flag rate**: {coupling_agg['same_flags']}/{p} = "
        f"**{same_flag_rate:.0f}%** — same HarnessGate flag pattern fired in "
        "consecutive iters. High = Planner's response did not resolve the "
        "specific issue Judge/Harness pointed at.",
        "",
        "### Stuck-loop detail (D7 + C1 cross-ref)",
        "",
    ])
    if stuck_sims:
        lines.append("| task | max code-similarity across iters | score |")
        lines.append("|------|------|------|")
        for tid, sim, score in sorted(stuck_sims, key=lambda x: -x[1]):
            lines.append(f"| {tid} | {sim:.2%} | {score:.2f} |")
    lines.append("")

    # Quick interpretation hooks
    lines.extend([
        "## How to read",
        "",
        "Per direction:",
        "- Tasks affected ≥5 → real signal, investigate further",
        "- 1-4 affected → marginal, overfitting risk if we tune for it",
        "- 0 affected → drop direction entirely",
        "",
        "For coupling:",
        "- C1 same-code-rate > 40% → Planner-side response discipline IS the "
        "right next investment (Reflexion / N-temp / stuck-detection)",
        "- C1 < 20% → Planner generally adapts; coupling is fine; bottleneck "
        "is elsewhere",
        "- C2 high while C1 low → Planner changes code but doesn't fix root "
        "cause; suggests guidance quality is the bottleneck (back to Judge)",
    ])

    OUTPUT_PATH.write_text("\n".join(lines))
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
