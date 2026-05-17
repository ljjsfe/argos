"""Mine failure patterns across all historical eval runs.

Reads results/eval_v*/ directories — each contains:
  - eval_report_kdd.json (task scores + failure categories)
  - task_<id>/trace.json (per-iteration plan, judge action, harness flags)
  - task_<id>/workspace/steps/step_<i>_code.py (executed SQL/Python)

Produces docs/FAILURE_PATTERN_LIBRARY.md with three tables:
  A. Cross-run stable failures — anonymized failure classes (not task_ids)
  B. Judge over-accept patterns — flag/spec combinations Judge wrongly finished
  C. SQL AST features in failing queries — structural patterns, no literal names

Generality rule: outputs describe properties (computation_type, AST nodes,
HarnessGate rule names) — NEVER specific column names, table names, or
business terms from KDD demo data.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import expressions as exp

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results"
OUTPUT_PATH = REPO_ROOT / "docs" / "FAILURE_PATTERN_LIBRARY.md"

# Tasks failing in this fraction of runs they appeared in → considered systemic
SYSTEMIC_FAILURE_THRESHOLD = 0.5
MIN_RUNS_FOR_SYSTEMIC = 5

VERSION_RE = re.compile(r"eval_v(\d+)")


def eval_version_key(eval_id: str) -> tuple[int, str]:
    """Sort key that orders eval_v9 before eval_v80 (numeric version, not lex)."""
    m = VERSION_RE.search(eval_id)
    num = int(m.group(1)) if m else 0
    return (num, eval_id)


@dataclass
class TaskRecord:
    eval_id: str
    task_id: str
    score: float
    failure_category: str  # "" if success
    difficulty: str
    iterations: list[dict] = field(default_factory=list)
    final_code: str | None = None
    final_language: str | None = None
    question_spec: dict = field(default_factory=dict)
    task_mode: str | None = None


def collect_eval_runs() -> list[Path]:
    """Return all results/eval_v*/ directories with a parseable eval_report."""
    runs = []
    for d in sorted(RESULTS_DIR.glob("eval_v*/")):
        if (d / "eval_report_kdd.json").exists():
            runs.append(d)
    return runs


def load_task_record(eval_dir: Path, task_score: dict) -> TaskRecord | None:
    """Build TaskRecord from a task's directory; returns None if unreadable."""
    task_id = task_score.get("task_id", "")
    if not task_id:
        return None

    task_dir = eval_dir / task_id
    trace_path = task_dir / "trace.json"
    if not trace_path.exists():
        return None

    try:
        trace = json.loads(trace_path.read_text())
    except Exception:
        return None

    obs = trace.get("observations") or {}
    iters = obs.get("iterations") or []

    rec = TaskRecord(
        eval_id=eval_dir.name,
        task_id=task_id,
        score=float(task_score.get("score", 0.0)),
        failure_category=task_score.get("failure_category", "") or "",
        difficulty=task_score.get("difficulty", "") or "",
        iterations=iters,
        question_spec=obs.get("question_spec") or {},
        task_mode=obs.get("task_mode"),
    )

    # Final iteration's code from workspace/steps/
    if iters:
        last_iter_idx = len(iters) - 1
        steps_dir = task_dir / "workspace" / "steps"
        for ext in ("py", "sql"):
            code_path = steps_dir / f"step_{last_iter_idx}_code.{ext}"
            if code_path.exists():
                try:
                    rec.final_code = code_path.read_text()
                    rec.final_language = "python" if ext == "py" else "sql"
                    break
                except Exception:
                    pass

    return rec


def collect_all_records() -> list[TaskRecord]:
    records = []
    for eval_dir in collect_eval_runs():
        try:
            report = json.loads((eval_dir / "eval_report_kdd.json").read_text())
        except Exception:
            continue
        for ts in report.get("task_scores", []):
            rec = load_task_record(eval_dir, ts)
            if rec is not None:
                records.append(rec)
    return records


# ─────────────────────────────────────────────────────────────────────────
# Table A: Cross-run stable failures (anonymized)
# ─────────────────────────────────────────────────────────────────────────

def mine_table_a(records: list[TaskRecord]) -> list[dict]:
    """Find tasks that fail across many runs with same failure_category.

    Output is anonymized: task_id is replaced by a stable hash.
    Reports failure class + computation_type + answer_type from question_spec.
    """
    by_task: dict[str, list[TaskRecord]] = defaultdict(list)
    for r in records:
        by_task[r.task_id].append(r)

    rows = []
    for task_id, runs in by_task.items():
        if len(runs) < MIN_RUNS_FOR_SYSTEMIC:
            continue
        failures = [r for r in runs if r.score < 1.0]
        fail_rate = len(failures) / len(runs)
        if fail_rate < SYSTEMIC_FAILURE_THRESHOLD:
            continue

        # Dominant failure category
        cat_counts = Counter(r.failure_category for r in failures if r.failure_category)
        top_cat = cat_counts.most_common(1)[0][0] if cat_counts else "unknown"

        # Most-recent question spec — prefer runs that actually have it
        with_spec = [r for r in runs if r.question_spec]
        latest = max(with_spec or runs, key=lambda r: eval_version_key(r.eval_id))
        qs = latest.question_spec
        difficulty = latest.difficulty or next((r.difficulty for r in runs if r.difficulty), "")

        rows.append({
            "task_hash": abs(hash(task_id)) % 100000,
            "runs_seen": len(runs),
            "fail_rate": round(fail_rate, 2),
            "dominant_failure": top_cat,
            "difficulty": difficulty,
            "answer_type": qs.get("answer_type", ""),
            "computation_type": qs.get("computation_type", ""),
            "tie_possible": qs.get("tie_possible", ""),
            "expected_row_count": qs.get("expected_row_count", ""),
        })

    rows.sort(key=lambda r: (-r["fail_rate"], -r["runs_seen"]))
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Table B: Judge over-accept patterns
# ─────────────────────────────────────────────────────────────────────────

def normalize_flag_signature(flags) -> str:
    """Anonymized signature for a harness_flags list — rule names only."""
    if not flags or not isinstance(flags, list):
        return "no_flags"
    parts = []
    for f in flags:
        if not isinstance(f, dict):
            continue
        rule = f.get("rule", "?")
        sev = (f.get("severity") or "").lower()
        if sev in ("block", "warn"):
            parts.append(f"{rule}:{sev[0]}")
    return ",".join(sorted(set(parts))) if parts else "no_flags"


def mine_table_b(records: list[TaskRecord]) -> list[dict]:
    """Patterns where Judge said 'finish' but final score was 0.

    Groups by (flag_signature, qa_shape, code_success) — counts only,
    no question text or task_id.
    """
    bucket: Counter = Counter()
    detail: dict[tuple, list[float]] = defaultdict(list)

    for r in records:
        if r.score >= 1.0:
            continue
        if not r.iterations:
            continue
        last = r.iterations[-1]
        if not isinstance(last, dict):
            continue
        judge = (last.get("judge_action") or "").lower()
        if not judge.startswith("finish"):
            continue

        flag_sig = normalize_flag_signature(last.get("harness_flags"))
        qs = r.question_spec
        qa_shape = f"{qs.get('answer_type','?')}/{qs.get('computation_type','?')}/tie={qs.get('tie_possible','?')}"
        code_ok = last.get("code_success", True)

        key = (flag_sig, qa_shape, bool(code_ok))
        bucket[key] += 1
        detail[key].append(r.score)

    rows = []
    for (flag_sig, qa_shape, code_ok), count in bucket.most_common():
        if count < 3:  # require at least 3 occurrences to be a "pattern"
            continue
        rows.append({
            "flag_signature": flag_sig,
            "qa_shape": qa_shape,
            "code_success": code_ok,
            "occurrences": count,
            "avg_score_when_fired": round(sum(detail[(flag_sig, qa_shape, code_ok)]) / count, 2),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Table C: SQL AST features in failing queries
# ─────────────────────────────────────────────────────────────────────────

PYTHON_SQL_RE = re.compile(
    r"""(?:duckdb\.sql|con\.execute|conn\.execute|cursor\.execute|"""
    r"""pd\.read_sql(?:_query)?|sqlite3\.\w+\.execute)"""
    r"""\s*\(\s*[fr]?(['"]{1,3})(.+?)\1""",
    re.DOTALL,
)

SQL_LEADING_KEYWORDS = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "CREATE", "ATTACH")


def looks_like_pure_sql(code: str) -> bool:
    """Detect when a .py file actually contains raw SQL only."""
    stripped = code.strip()
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].upper().lstrip("(")
    return first_word in SQL_LEADING_KEYWORDS


def extract_sql_from_python(code: str) -> list[str]:
    """Best-effort: pull SQL strings out of Python source."""
    matches = PYTHON_SQL_RE.findall(code)
    return [m[1].strip() for m in matches if "SELECT" in m[1].upper() and len(m[1].strip()) > 30]


def get_sql_candidates(rec: TaskRecord) -> list[str]:
    if not rec.final_code:
        return []
    if rec.final_language == "sql" or looks_like_pure_sql(rec.final_code):
        return [rec.final_code]
    # Python — try to extract embedded SQL
    return extract_sql_from_python(rec.final_code)


def extract_ast_features(sql: str) -> dict:
    """Return a dict of bool features describing the SQL structure.

    All features are STRUCTURAL — no literal names or values.
    """
    feats = {
        "has_limit_1": False,
        "has_limit_n": False,
        "has_order_by": False,
        "has_group_by": False,
        "has_distinct": False,
        "has_count_distinct": False,
        "has_count_star": False,
        "has_sum": False,
        "has_avg": False,
        "has_min_max": False,
        "has_where_literal_eq": False,
        "has_where_in_literal": False,
        "has_is_null_check": False,
        "has_join": False,
        "num_joins": 0,
        "has_subquery": False,
        "has_having": False,
        "has_case_when": False,
        "has_window_func": False,
    }
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except Exception:
        return feats
    if tree is None:
        return feats

    # LIMIT
    limit = tree.find(exp.Limit)
    if limit is not None:
        try:
            n = int(str(limit.expression))
            feats["has_limit_1"] = (n == 1)
            feats["has_limit_n"] = (n != 1)
        except Exception:
            feats["has_limit_n"] = True

    feats["has_order_by"] = tree.find(exp.Order) is not None
    feats["has_group_by"] = tree.find(exp.Group) is not None
    feats["has_having"] = tree.find(exp.Having) is not None

    # DISTINCT
    for node in tree.find_all(exp.Distinct):
        feats["has_distinct"] = True
        break

    # Aggregation functions
    for node in tree.find_all(exp.Count):
        feats["has_count_star"] = True
        if isinstance(node.this, exp.Distinct) or (
            hasattr(node, "args") and node.args.get("distinct")
        ):
            feats["has_count_distinct"] = True
    for node in tree.find_all(exp.Sum):
        feats["has_sum"] = True
    for node in tree.find_all(exp.Avg):
        feats["has_avg"] = True
    for node in tree.find_all((exp.Min, exp.Max)):
        feats["has_min_max"] = True

    # WHERE literal equality / IN
    where = tree.find(exp.Where)
    if where is not None:
        for eq in where.find_all(exp.EQ):
            if isinstance(eq.expression, exp.Literal):
                feats["has_where_literal_eq"] = True
        for in_node in where.find_all(exp.In):
            feats["has_where_in_literal"] = True
        for is_node in where.find_all(exp.Is):
            if isinstance(is_node.expression, exp.Null):
                feats["has_is_null_check"] = True

    # JOIN
    joins = list(tree.find_all(exp.Join))
    feats["num_joins"] = len(joins)
    feats["has_join"] = len(joins) > 0

    # Subqueries
    subselects = [n for n in tree.find_all(exp.Select) if n is not tree]
    feats["has_subquery"] = len(subselects) > 0

    # CASE WHEN
    feats["has_case_when"] = tree.find(exp.Case) is not None

    # Window functions
    feats["has_window_func"] = tree.find(exp.Window) is not None

    return feats


def mine_table_c(records: list[TaskRecord]) -> dict:
    """Compute per-feature failure rate vs success rate.

    For each feature, report:
      - count in failing tasks
      - count in succeeding tasks
      - failure rate of tasks where feature is present
      - "suspicion" = failure_rate_with_feature - baseline_failure_rate
    """
    feat_in_fail: Counter = Counter()
    feat_in_pass: Counter = Counter()
    fail_total = 0
    pass_total = 0
    fail_with_spec: dict[str, Counter] = defaultdict(Counter)  # for crosses

    for r in records:
        sqls = get_sql_candidates(r)
        if not sqls:
            continue
        # Use the longest SQL as the representative for this record
        sql = max(sqls, key=len)
        feats = extract_ast_features(sql)
        is_fail = r.score < 1.0

        if is_fail:
            fail_total += 1
        else:
            pass_total += 1

        for k, v in feats.items():
            if isinstance(v, bool) and v:
                if is_fail:
                    feat_in_fail[k] += 1
                else:
                    feat_in_pass[k] += 1

        # Crosses with question_spec
        qs = r.question_spec
        if is_fail and feats.get("has_limit_1") and qs.get("tie_possible") is True:
            fail_with_spec["limit1_tie_possible"]["count"] += 1
        if is_fail and not feats.get("has_is_null_check") and feats.get("has_where_literal_eq"):
            fail_with_spec["where_eq_no_null_guard"]["count"] += 1
        if is_fail and feats.get("has_sum") and qs.get("computation_type") == "average":
            fail_with_spec["sum_for_average_question"]["count"] += 1
        if is_fail and feats.get("has_count_star") and not feats.get("has_count_distinct") \
                and qs.get("computation_type") == "count" \
                and qs.get("answer_type") == "scalar":
            fail_with_spec["count_no_distinct_when_distinct_needed"]["count"] += 1

    total = fail_total + pass_total
    baseline_fail = fail_total / total if total else 0.0

    rows = []
    for feat in feat_in_fail.keys() | feat_in_pass.keys():
        f = feat_in_fail[feat]
        p = feat_in_pass[feat]
        n = f + p
        if n < 10:  # require enough samples
            continue
        fr = f / n
        rows.append({
            "feature": feat,
            "in_fail": f,
            "in_pass": p,
            "fail_rate_with_feat": round(fr, 2),
            "delta_vs_baseline": round(fr - baseline_fail, 2),
        })
    rows.sort(key=lambda r: -r["delta_vs_baseline"])
    return {
        "rows": rows,
        "baseline_fail_rate": round(baseline_fail, 2),
        "fail_total": fail_total,
        "pass_total": pass_total,
        "crosses": dict(fail_with_spec),
    }


# ─────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────

def render_table_a(rows: list[dict]) -> str:
    if not rows:
        return "(no systemic failures detected)\n"
    out = ["| hash | runs | fail_rate | dominant_failure | difficulty | answer_type | computation_type | tie_possible | expected_rows |",
           "|------|------|-----------|------------------|------------|-------------|------------------|--------------|---------------|"]
    for r in rows[:40]:
        out.append(
            f"| {r['task_hash']:>5} | {r['runs_seen']} | {r['fail_rate']} | "
            f"{r['dominant_failure']} | {r['difficulty']} | {r['answer_type']} | "
            f"{r['computation_type']} | {r['tie_possible']} | {r['expected_row_count']} |"
        )
    return "\n".join(out) + "\n"


def render_table_b(rows: list[dict]) -> str:
    if not rows:
        return "(no over-accept patterns detected)\n"
    out = ["| flag_signature | qa_shape | code_ok | occurrences | avg_score |",
           "|----------------|----------|---------|-------------|-----------|"]
    for r in rows[:25]:
        out.append(
            f"| `{r['flag_signature']}` | `{r['qa_shape']}` | {r['code_success']} | "
            f"{r['occurrences']} | {r['avg_score_when_fired']} |"
        )
    return "\n".join(out) + "\n"


def render_table_c(table_c: dict) -> str:
    rows = table_c["rows"]
    crosses = table_c["crosses"]
    out = [
        f"Baseline failure rate (any SQL run): **{table_c['baseline_fail_rate']}** "
        f"(fail={table_c['fail_total']}, pass={table_c['pass_total']})\n",
        "### Per-feature failure rate",
        "| feature | in_fail | in_pass | fail_rate | delta_vs_baseline |",
        "|---------|---------|---------|-----------|-------------------|",
    ]
    for r in rows:
        out.append(
            f"| `{r['feature']}` | {r['in_fail']} | {r['in_pass']} | "
            f"{r['fail_rate_with_feat']} | {r['delta_vs_baseline']:+.2f} |"
        )

    out.append("\n### Cross-feature patterns (anti-patterns)")
    if not crosses:
        out.append("(none)")
    else:
        out.append("| pattern | failures |")
        out.append("|---------|----------|")
        for name, ctr in crosses.items():
            out.append(f"| `{name}` | {ctr.get('count', 0)} |")
    return "\n".join(out) + "\n"


def compute_headline_stats(records: list[TaskRecord]) -> dict:
    """High-level numbers: how often does Judge silently approve wrong answers
    with no harness signal?"""
    judge_finish_failures_no_flags = 0
    judge_finish_failures_with_flags = 0
    total_failures = 0
    total_records = 0
    for r in records:
        if not r.iterations:
            continue
        total_records += 1
        if r.score >= 1.0:
            continue
        total_failures += 1
        last = r.iterations[-1]
        if not isinstance(last, dict):
            continue
        if (last.get("judge_action") or "").lower().startswith("finish"):
            sig = normalize_flag_signature(last.get("harness_flags"))
            if sig == "no_flags":
                judge_finish_failures_no_flags += 1
            else:
                judge_finish_failures_with_flags += 1
    return {
        "total_records": total_records,
        "total_failures": total_failures,
        "judge_finish_failures_no_flags": judge_finish_failures_no_flags,
        "judge_finish_failures_with_flags": judge_finish_failures_with_flags,
        "pct_silent_failures": round(
            100 * judge_finish_failures_no_flags / total_failures, 1
        ) if total_failures else 0,
    }


def write_report(table_a, table_b, table_c, total_records: int, headline: dict):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Failure Pattern Library",
        "",
        "_Mined from `results/eval_v*/` historical traces. Generated by "
        "`scripts/mine_failure_patterns.py`. Do not edit by hand — re-run script._",
        "",
        f"**Source**: {total_records} (eval, task) records across all historical eval runs.",
        "",
        "**Generality rule**: tables below describe structural / behavioral "
        "properties — no specific column names, table names, or business "
        "terms from KDD demo data. Patterns must be valid for any "
        "general-purpose data agent.",
        "",
        "---",
        "",
        "## Headline finding",
        "",
        f"- **{headline['judge_finish_failures_no_flags']}** cases where Judge said `finish` AND HarnessGate produced **no flags** AND the answer was wrong.",
        f"- That is **{headline['pct_silent_failures']}%** of all {headline['total_failures']} failures.",
        f"- Compare: only {headline['judge_finish_failures_with_flags']} failures had Judge=finish WITH at least one Harness WARN/BLOCK signal.",
        "",
        "**Implication**: the dominant failure mode is **silent** — no deterministic signal "
        "fires, Judge alone accepts. This is exactly the gap that Phase 2 (Rubric Judge / "
        "Skeptic Judge / CoVe) is designed to close. Adding more HarnessGate rules helps "
        "only with the minority of failures that have *some* signal already.",
        "",
        "---",
        "",
        "## Table A — Cross-Run Stable Failures (anonymized)",
        "",
        f"Tasks failing in ≥{SYSTEMIC_FAILURE_THRESHOLD * 100:.0f}% of "
        f"runs they appeared in (min {MIN_RUNS_FOR_SYSTEMIC} runs). "
        "`task_hash` replaces task_id to avoid demo-specific bias.",
        "",
        render_table_a(table_a),
        "",
        "**Generality reading**: scan dominant_failure × question_shape — "
        "tells us which *kinds* of questions our agent systemically fails on, "
        "transferable to any benchmark with similar shapes.",
        "",
        "---",
        "",
        "## Table B — Judge Over-Accept Patterns",
        "",
        "Final iteration where Judge said `finish` but task scored 0. "
        "Grouped by HarnessGate flag signature + QuestionSpec shape.",
        "",
        render_table_b(table_b),
        "",
        "**Generality reading**: tells us which flag combinations Judge "
        "wrongly trusted. Direct input to Phase 2.1 (rubric judge) — these "
        "are the checks that must be enforced, not optional.",
        "",
        "---",
        "",
        "## Table C — SQL AST Features in Failing Queries",
        "",
        "Structural features extracted via sqlglot AST. Positive "
        "`delta_vs_baseline` means the feature is over-represented in failing "
        "queries — a smell, not a cause.",
        "",
        render_table_c(table_c),
        "",
        "**Generality reading**: AST-level patterns (LIMIT 1, no NULL guard, "
        "SUM for average questions) are universal SQL anti-patterns. Safe "
        "to encode as HarnessGate rules or planner_coder constraints.",
        "",
        "---",
        "",
        "## How to use this report",
        "",
        "1. **Pick patterns with the strongest evidence** (Table A high "
        "fail_rate, Table B high occurrences, Table C high delta).",
        "2. **For each pattern, write down its generality assertion** before "
        "implementing a fix — if the fix only makes sense given KDD-specific "
        "knowledge, reject.",
        "3. **Cross-check with Phase 0.2 capability probe**: a pattern is "
        "robust if both historical traces AND independent Qwen probes show "
        "the weakness.",
    ]
    OUTPUT_PATH.write_text("\n".join(lines))


def main():
    print(f"Reading from {RESULTS_DIR} ...")
    records = collect_all_records()
    print(f"Loaded {len(records)} (eval, task) records.")

    print("Mining Table A: cross-run stable failures ...")
    table_a = mine_table_a(records)
    print(f"  → {len(table_a)} systemic-failure task classes.")

    print("Mining Table B: judge over-accept patterns ...")
    table_b = mine_table_b(records)
    print(f"  → {len(table_b)} over-accept signatures.")

    print("Mining Table C: SQL AST features ...")
    table_c = mine_table_c(records)
    print(f"  → {len(table_c['rows'])} features computed "
          f"(baseline fail_rate={table_c['baseline_fail_rate']}).")

    headline = compute_headline_stats(records)
    print(f"  → silent failures (no flags + judge=finish + wrong): "
          f"{headline['judge_finish_failures_no_flags']} "
          f"({headline['pct_silent_failures']}% of all failures)")

    print(f"Writing report to {OUTPUT_PATH} ...")
    write_report(table_a, table_b, table_c, total_records=len(records), headline=headline)
    print("Done.")


if __name__ == "__main__":
    main()
