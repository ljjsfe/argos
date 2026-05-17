"""Phase 0.2 — Qwen capability probe (Layer 4 = Judge).

Independently measures the production Judge agent's reliability on
synthetic, non-KDD scenarios. Uses the real `judge.evaluate()` function
with the real `dataline/prompts/judge.md` prompt. Only the scenario
context (question + step result + harness flags + question_spec) is
constructed for the probe.

Generality discipline (north star):
  - Schemas: abstract symbolic (T1(a, b, c)) or mixed-neutral
    (orders/inventory/measurements — common to many benchmarks, not
    KDD-specific Czech banking / F1 / California schools).
  - Capabilities tested: universal data-reasoning skills (magnitude
    plausibility, completeness, signal respect, false-positive avoidance,
    epistemic humility) — NOT KDD task patterns.
  - Output: capability-axis report, not a single composite score.

5 P-tests (~50 LLM calls, ~$5, ~10 min):
  P1  Magnitude implausibility — wrong-order-of-magnitude answer
  P2  Missing-entities-in-list — list answer with rows dropped
  P3  Respect HarnessGate WARN — should not finish when flag fires
  P4  False-positive control — clean answer must finish
  P5  No-context humility — without evidence, do not over-confidently judge

Run:
    python3 scripts/probe_qwen_capability.py

Output: docs/QWEN_CAPABILITY_PROFILE.md
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv

load_dotenv(override=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dataline.agents import judge as judge_agent  # noqa: E402
from dataline.core.context_manager import ContextManager  # noqa: E402
from dataline.core.llm_client import create_client_from_config  # noqa: E402
from dataline.core.types import (  # noqa: E402
    AnalysisState,
    HarnessFlag,
    PlanStep,
    QuestionSpec,
    SandboxResult,
    StepRecord,
)

OUTPUT_PATH = REPO_ROOT / "docs" / "QWEN_CAPABILITY_PROFILE.md"
TOKEN_LIMIT = 262_144


# ---------------------------------------------------------------------------
# Scenario type
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    """One probe scenario — feed into judge.evaluate() and check action."""
    probe_id: str
    case_idx: int
    question: str
    manifest_summary: str
    code: str
    stdout: str
    structured_json: str
    return_code: int = 0
    harness_warnings: tuple[HarnessFlag, ...] = ()
    question_spec: QuestionSpec = field(default_factory=QuestionSpec)
    iteration: int = 0
    # expected_action_passes(decision_action) -> bool
    expected_pass_fn: Callable[[str], bool] = field(default=lambda a: True)
    intent_note: str = ""


@dataclass
class ProbeResult:
    scenario: Scenario
    action: str
    reasoning: str
    passed: bool
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _structured(rows: list[dict]) -> str:
    """Wrap rows in the structured_json envelope save_result() produces."""
    if not rows:
        return json.dumps({"answer": {}})
    # save_result wraps a dict-of-lists; mimic that shape.
    cols = list(rows[0].keys())
    answer = {c: [r[c] for r in rows] for c in cols}
    return json.dumps({"answer": answer})


def _scalar_structured(value, col_name: str = "result") -> str:
    return json.dumps({"answer": {col_name: [value]}})


def _build_state(s: Scenario) -> AnalysisState:
    plan = PlanStep(step_description="probe step", expected_output="any")
    result = SandboxResult(
        stdout=s.stdout,
        stderr="",
        return_code=s.return_code,
        execution_time_ms=10,
        step_id="probe_step",
        structured_json=s.structured_json,
    )
    step = StepRecord(plan=plan, code=s.code, result=result, step_index=0)
    return AnalysisState(
        task_id=f"probe_{s.probe_id}_{s.case_idx}",
        question=s.question,
        manifest_summary=s.manifest_summary,
        data_profile_summary="",
        full_step_details=(step,),
        completed_steps=(f"Step 0: {plan.step_description}",),
    )


def run_scenario(s: Scenario, llm) -> ProbeResult:
    cm = ContextManager(token_limit=TOKEN_LIMIT)
    state = _build_state(s)
    try:
        decision = judge_agent.evaluate(
            question=s.question,
            steps_done=[],
            llm=llm,
            state=state,
            cm=cm,
            iteration=s.iteration,
            max_iterations=8,
            harness_warnings=list(s.harness_warnings),
            question_spec=s.question_spec,
        )
        action = decision.action or "continue"
        return ProbeResult(
            scenario=s,
            action=action,
            reasoning=(decision.reasoning or "")[:240],
            passed=s.expected_pass_fn(action),
        )
    except Exception as e:
        return ProbeResult(
            scenario=s, action="error", reasoning="", passed=False, error=str(e),
        )


# ---------------------------------------------------------------------------
# P1 — Magnitude implausibility (10 cases)
# ---------------------------------------------------------------------------

P1_TEMPLATES = [
    # (question, true_magnitude_hint, absurd_answer)
    ("What is the average monthly value of column a per group b over a 12-month period?",
     "expect a few hundred to a few thousand", 82_000_000.50),
    ("What is the average yearly count of distinct entities in T1?",
     "expect a small integer dozens to hundreds", 9_999_999),
    ("What is the average daily price per unit?",
     "expect a single-digit to two-digit float", 1_234_567.89),
    ("What fraction of records in T1 have positive value? Return as a percentage.",
     "expect 0-100 range", 5_872.4),
    ("How many distinct values of column c are there?",
     "expect a count, typically small integer", 1.5),
    ("What is the median latency in milliseconds?",
     "expect a small positive number", -42),
    ("What is the average score across all entries?",
     "expect a value within plausible score scale", 1e15),
    ("What is the total revenue in dollars across all transactions?",
     "expect plausible sum, NOT astronomical", 9.87e20),
    ("What percentage of orders are completed?",
     "expect 0-100 range", 150.0),
    ("Compute the standard deviation of column a.",
     "expect a positive value comparable to data range", -3.14),
]


def build_p1_cases() -> list[Scenario]:
    cases: list[Scenario] = []
    manifest = (
        "## Data Sources\n"
        "- T1 (csv): columns a (float), b (text), c (int). 10000 rows.\n"
        "- Stats from profile: a ranges 0-1000, c distinct values 50."
    )
    for i, (q, _hint, absurd) in enumerate(P1_TEMPLATES):
        code = (
            "import pandas as pd\n"
            "df = pd.read_csv('T1.csv')\n"
            "# (computation omitted)\n"
            f"save_result({{'result': {absurd!r}}})"
        )
        stdout = f"Computed result: {absurd}\n"
        sj = _scalar_structured(absurd)
        cases.append(Scenario(
            probe_id="P1", case_idx=i,
            question=q,
            manifest_summary=manifest,
            code=code, stdout=stdout, structured_json=sj,
            expected_pass_fn=lambda a: a in ("continue", "backtrack"),
            intent_note="answer magnitude is absurd — Judge should not finish",
        ))
    return cases


# ---------------------------------------------------------------------------
# P2 — Missing entities in list (10 cases)
# ---------------------------------------------------------------------------

def build_p2_cases() -> list[Scenario]:
    cases: list[Scenario] = []
    rng = random.Random(42)
    for i in range(10):
        total = rng.choice([8, 12, 15, 20, 25])
        missing = rng.choice([2, 3, 5])
        kept = max(2, total - missing)
        # Use abstract / mixed-neutral entity names
        labels = [f"entity_{n:02d}" for n in range(total)]
        answered = labels[:kept]
        # Manifest tells Judge ground-truth row count
        manifest = (
            "## Data Sources\n"
            f"- T1 (csv): columns name (text), a (int). {total} rows.\n"
            f"- Profile: column a all positive (min 1, max 999). All {total} rows have a > 0."
        )
        question = (
            "List the names of every entity in T1 where column a is strictly "
            "positive. Return one column with all matching names."
        )
        sj = _structured([{"name": n} for n in answered])
        code = (
            "import pandas as pd\n"
            "df = pd.read_csv('T1.csv')\n"
            "# Limit accidentally drops some matching rows\n"
            f"result = df[df['a'] > 0]['name'].head({kept}).tolist()\n"
            "save_result({'name': result})"
        )
        stdout = f"Returned {kept} names\n"
        spec = QuestionSpec(
            answer_type="list", computation_type="lookup",
            expected_row_count="multiple", value_style="name",
        )
        cases.append(Scenario(
            probe_id="P2", case_idx=i,
            question=question,
            manifest_summary=manifest,
            code=code, stdout=stdout, structured_json=sj,
            question_spec=spec,
            expected_pass_fn=lambda a: a in ("continue", "backtrack"),
            intent_note=f"true total {total}, returned only {kept}",
        ))
    return cases


# ---------------------------------------------------------------------------
# P3 — Respect HarnessGate WARN (10 cases)
# ---------------------------------------------------------------------------

P3_WARN_TYPES = [
    ("agg_type",
     "Code uses AVG but question asks for total — verify aggregation."),
    ("phantom_filter",
     "WHERE clause filters on a value that does not exist in column distinct set."),
    ("nan_answer",
     "Answer column contains NaN values — verify computation handled nulls."),
    ("empty_output",
     "Answer dict is empty — code may have filtered too aggressively."),
    ("qa_column_count",
     "Answer has 2 columns but question expects 1 — extra columns dilute the score."),
    ("output_shape",
     "Answer shape does not match expected scalar response."),
    ("agg_type",
     "Code uses SUM where question phrasing suggests AVG over groups."),
    ("phantom_filter",
     "Filter value not found in profile DISTINCT values for that column."),
    ("nan_answer",
     "Result column has 30% NaN — likely an unhandled null."),
    ("empty_output",
     "Result set is empty — question expects at least one row."),
]


def build_p3_cases() -> list[Scenario]:
    cases: list[Scenario] = []
    manifest = (
        "## Data Sources\n"
        "- T2 (csv): columns x (int), y (text), z (float). 500 rows.\n"
        "- Profile: x ranges 0-100, y has 20 distinct values, z 0-50."
    )
    question = (
        "Return the value of column z aggregated by group y for rows where x > 10."
    )
    sj = _structured([{"y": "group_a", "z": 12.3}, {"y": "group_b", "z": 4.5}])
    code = (
        "import pandas as pd\n"
        "df = pd.read_csv('T2.csv')\n"
        "result = df[df['x'] > 10].groupby('y')['z'].sum().reset_index()\n"
        "save_result({'y': result['y'].tolist(), 'z': result['z'].tolist()})"
    )
    stdout = "Returned 2 group rows\n"
    spec = QuestionSpec(
        answer_type="table", computation_type="aggregate",
        expected_row_count="multiple",
    )
    for i, (rule, msg) in enumerate(P3_WARN_TYPES):
        warn = HarnessFlag(rule=rule, severity="warn", message=msg)
        cases.append(Scenario(
            probe_id="P3", case_idx=i,
            question=question,
            manifest_summary=manifest,
            code=code, stdout=stdout, structured_json=sj,
            harness_warnings=(warn,),
            question_spec=spec,
            expected_pass_fn=lambda a: a in ("continue", "backtrack"),
            intent_note=f"WARN flag fired: {rule} — Judge should not finish",
        ))
    return cases


# ---------------------------------------------------------------------------
# P4 — False-positive control (10 cases)
# ---------------------------------------------------------------------------

P4_CLEAN_CASES = [
    # (question, code, structured, stdout, qspec)
    ("How many rows are in T1?",
     "df = pd.read_csv('T1.csv')\nprint(len(df))\nsave_result({'count': [42]})",
     _scalar_structured(42, "count"),
     "42\n",
     QuestionSpec(answer_type="scalar", computation_type="count")),
    ("What is the average of column a in T1?",
     "df = pd.read_csv('T1.csv')\nv = df['a'].mean()\nsave_result({'avg': [v]})",
     _scalar_structured(123.45, "avg"),
     "123.45\n",
     QuestionSpec(answer_type="scalar", computation_type="aggregate")),
    ("What is the sum of column z in T2 for rows where y='group_a'?",
     "df = pd.read_csv('T2.csv')\nv = df[df['y']=='group_a']['z'].sum()\nsave_result({'total': [v]})",
     _scalar_structured(78.9, "total"),
     "78.9\n",
     QuestionSpec(answer_type="scalar", computation_type="aggregate")),
    ("What fraction of rows in T1 have a > 50? Return as a percentage.",
     "df = pd.read_csv('T1.csv')\nv = (df['a'] > 50).mean() * 100\nsave_result({'pct': [v]})",
     _scalar_structured(37.2, "pct"),
     "37.2\n",
     QuestionSpec(answer_type="scalar", computation_type="ratio")),
    ("How many distinct values of column b are in T1?",
     "df = pd.read_csv('T1.csv')\nv = df['b'].nunique()\nsave_result({'distinct_b': [v]})",
     _scalar_structured(17, "distinct_b"),
     "17\n",
     QuestionSpec(answer_type="scalar", computation_type="count")),
    ("Return the maximum value of column z in T2.",
     "df = pd.read_csv('T2.csv')\nv = df['z'].max()\nsave_result({'max_z': [v]})",
     _scalar_structured(49.8, "max_z"),
     "49.8\n",
     QuestionSpec(answer_type="scalar", computation_type="aggregate")),
    ("What is the median of column a in T1 where b='group_x'?",
     "df = pd.read_csv('T1.csv')\nv = df[df['b']=='group_x']['a'].median()\nsave_result({'median': [v]})",
     _scalar_structured(50.0, "median"),
     "50.0\n",
     QuestionSpec(answer_type="scalar", computation_type="aggregate")),
    ("List the distinct values of column b in T1.",
     "df = pd.read_csv('T1.csv')\nvals = sorted(df['b'].unique().tolist())\nsave_result({'b': vals})",
     _structured([{"b": f"group_{c}"} for c in "abcdefghij"]),
     "10 distinct values\n",
     QuestionSpec(answer_type="list", computation_type="lookup", expected_row_count="multiple")),
    ("What is the standard deviation of column a in T1?",
     "df = pd.read_csv('T1.csv')\nv = df['a'].std()\nsave_result({'std': [v]})",
     _scalar_structured(15.7, "std"),
     "15.7\n",
     QuestionSpec(answer_type="scalar", computation_type="aggregate")),
    ("Count the rows in T2 where z > 20.",
     "df = pd.read_csv('T2.csv')\nv = int((df['z'] > 20).sum())\nsave_result({'count': [v]})",
     _scalar_structured(83, "count"),
     "83\n",
     QuestionSpec(answer_type="scalar", computation_type="count")),
]


def build_p4_cases() -> list[Scenario]:
    cases: list[Scenario] = []
    manifest = (
        "## Data Sources\n"
        "- T1 (csv): columns a (float), b (text). 100 rows. a range 0-100. b 17 distinct.\n"
        "- T2 (csv): columns y (text), z (float). 200 rows. z range 0-50."
    )
    for i, (q, code, sj, stdout, spec) in enumerate(P4_CLEAN_CASES):
        cases.append(Scenario(
            probe_id="P4", case_idx=i,
            question=q,
            manifest_summary=manifest,
            code=code, stdout=stdout, structured_json=sj,
            question_spec=spec,
            expected_pass_fn=lambda a: a == "finish",
            intent_note="clean answer with no harness flags — Judge should finish",
        ))
    return cases


# ---------------------------------------------------------------------------
# P5 — No-context humility (5 cases)
# ---------------------------------------------------------------------------

P5_CASES = [
    ("Compute X.", "result = compute_x()\nsave_result({'X': [1]})", _scalar_structured(1, "X"), ""),
    ("Get Y for group Z.", "print('Y for Z')\nsave_result({'Y': [99]})", _scalar_structured(99, "Y"), ""),
    ("Return the answer.", "save_result({'a': [0]})", _scalar_structured(0, "a"), ""),
    ("How many widgets?", "save_result({'count': [7]})", _scalar_structured(7, "count"), ""),
    ("Find the right value.", "save_result({'value': [42]})", _scalar_structured(42, "value"), ""),
]


def build_p5_cases() -> list[Scenario]:
    cases: list[Scenario] = []
    for i, (q, code, sj, stdout) in enumerate(P5_CASES):
        cases.append(Scenario(
            probe_id="P5", case_idx=i,
            question=q,
            manifest_summary="(no manifest available)",
            code=code, stdout=stdout, structured_json=sj,
            expected_pass_fn=lambda a: a in ("finish", "continue"),
            intent_note=(
                "Minimal context. Either choice is defensible — Judge should "
                "not confidently reject (backtrack) without evidence."
            ),
        ))
    return cases


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_report(results: list[ProbeResult], wall_seconds: float, llm_model: str):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    by_probe: dict[str, list[ProbeResult]] = {}
    for r in results:
        by_probe.setdefault(r.scenario.probe_id, []).append(r)

    lines = [
        "# Qwen Capability Profile — Layer 4 (Judge)",
        "",
        "_Generated by `scripts/probe_qwen_capability.py`. Re-run to refresh._",
        "",
        f"**Model**: `{llm_model}`",
        f"**Total scenarios**: {len(results)} | **Wall time**: {wall_seconds:.0f}s",
        "",
        "**Generality note**: All scenarios use abstract symbolic schemas "
        "(T1(a, b, c), T2(x, y, z)) or mixed-neutral entity language. No "
        "KDD-specific tables / columns / business terms appear. Capabilities "
        "tested are universal data-reasoning skills.",
        "",
        "---",
        "",
        "## Headline accuracy by probe",
        "",
        "| Probe | What it tests | Cases | Pass | Accuracy |",
        "|-------|---------------|-------|------|----------|",
    ]
    probe_titles = {
        "P1": "Magnitude implausibility — wrong-order-of-magnitude answer should NOT finish",
        "P2": "Missing entities in list — short list answer should NOT finish",
        "P3": "Respect HarnessGate WARN — flagged iteration should NOT finish",
        "P4": "False-positive control — clean answer SHOULD finish",
        "P5": "No-context humility — minimal context should not over-confidently reject",
    }
    for pid in sorted(by_probe):
        rs = by_probe[pid]
        passed = sum(1 for r in rs if r.passed)
        acc = passed / len(rs) if rs else 0.0
        lines.append(
            f"| **{pid}** | {probe_titles[pid]} | {len(rs)} | {passed} | "
            f"**{acc * 100:.0f}%** |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## Per-scenario detail",
        "",
    ])
    for pid in sorted(by_probe):
        lines.append(f"### {pid} — {probe_titles[pid]}")
        lines.append("")
        lines.append("| # | action | passed | reasoning (truncated) | intent |")
        lines.append("|---|--------|--------|-----------------------|--------|")
        for r in by_probe[pid]:
            status = "✅" if r.passed else "❌"
            reasoning = r.reasoning.replace("|", "/").replace("\n", " ")[:140]
            intent = r.scenario.intent_note.replace("|", "/")
            lines.append(
                f"| {r.scenario.case_idx} | `{r.action}` | {status} | "
                f"{reasoning} | {intent} |"
            )
        lines.append("")

    lines.extend([
        "---",
        "",
        "## Interpretation",
        "",
        "- **P3 < 80%**: HarnessGate WARN signals are not respected → "
        "  Rubric Judge (Phase 2.1) must force citation of flag IDs.",
        "- **P1 < 70%**: Magnitude reasoning gap → Rubric Judge needs an "
        "  explicit 'magnitude check' rubric line; CoVe verification mode "
        "  (Phase 2.4) is the deeper fix.",
        "- **P2 < 70%**: List completeness gap → Rubric Judge needs an "
        "  explicit row-count check vs QuestionSpec.expected_row_count.",
        "- **P4 < 90%**: False-positive rate too high → critique upgrades "
        "  must NOT add brittle continue triggers; verify Skeptic Judge "
        "  doesn't regress this.",
        "- **P5**: Either action is defensible. Watch for over-confident "
        "  backtrack — that would indicate Judge invents grounds.",
        "",
        "## Cross-check against Phase 0.1",
        "",
        "Trace mining showed 1070 silent failures (Judge=finish + no flag "
        "+ wrong answer = 57.5% of all failures). The P1/P2 accuracy here "
        "are the synthetic-data analogues. Low scores on those two probes "
        "confirm the silent-failure dominance is a Judge capability gap, "
        "not just a data-distribution artifact.",
    ])

    OUTPUT_PATH.write_text("\n".join(lines))
    print(f"Report written: {OUTPUT_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    llm = create_client_from_config(cfg)
    model = os.environ.get("MODEL_NAME", cfg["llm"].get("model", "?"))
    print(f"Probing model: {model}")

    builders = [
        ("P1", build_p1_cases),
        ("P2", build_p2_cases),
        ("P3", build_p3_cases),
        ("P4", build_p4_cases),
        ("P5", build_p5_cases),
    ]

    all_cases: list[Scenario] = []
    for _pid, fn in builders:
        all_cases.extend(fn())

    print(f"Total scenarios: {len(all_cases)}")

    results: list[ProbeResult] = []
    t0 = time.time()
    for s in all_cases:
        r = run_scenario(s, llm)
        results.append(r)
        ok = "PASS" if r.passed else "FAIL"
        err = f" [err: {r.error[:80]}]" if r.error else ""
        print(f"  {s.probe_id}/#{s.case_idx:02d}  {ok}  action={r.action}{err}")

    elapsed = time.time() - t0
    print(f"\nFinished {len(results)} scenarios in {elapsed:.0f}s.")
    write_report(results, elapsed, model)


if __name__ == "__main__":
    main()
