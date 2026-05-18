"""Run the Judge replay set against the current (or overridden) Judge prompt.

For each case in replay_set/cases.jsonl:
  1. Re-profile public/input/task_<id>/ to rebuild manifest_summary +
     domain_rules (deterministic, reproduces what Judge saw).
  2. Read the actual code Judge saw from
     results/<eval_dir>/<task_id>/workspace/steps/step_<last_iter>_code.*
  3. Read the actual structured_json output from step_<last_iter>_output.txt
     (or step_result.json if present).
  4. Reconstruct AnalysisState, invoke judge.evaluate(), record the action.

Baseline expectation: most cases reproduce the original "finish" verdict —
that's the confirmation that reconstruction is faithful. Catch rate is
the % of cases where replay Judge says "continue" or "backtrack" — i.e.,
would have caught the silent failure.

Run:
    python3 scripts/run_replay.py                   # baseline
    python3 scripts/run_replay.py --split dev       # dev only
    python3 scripts/run_replay.py --prompt-override path/to/new_judge.md
    python3 scripts/run_replay.py --label rubric_v1 # tag output file

Output:
    docs/JUDGE_REPLAY_<label>.md  (default label = "baseline")
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(override=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dataline.agents import analyzer, judge as judge_agent  # noqa: E402
from dataline.core.context_manager import ContextManager  # noqa: E402
from dataline.core.llm_client import create_client_from_config  # noqa: E402
from dataline.core.types import (  # noqa: E402
    AnalysisState,
    PlanStep,
    QuestionSpec,
    SandboxResult,
    StepRecord,
)
from dataline.profiler import manifest as profiler  # noqa: E402
from dataline.profiler.manifest import manifest_to_json  # noqa: E402

REPLAY_DIR = REPO_ROOT / "replay_set"
RESULTS_DIR = REPO_ROOT / "results"
TASK_INPUT_DIR = REPO_ROOT / "public" / "input"
DOCS_DIR = REPO_ROOT / "docs"
TOKEN_LIMIT = 262_144


@dataclass
class ReplayResult:
    case_id: str
    eval_dir: str
    task_id: str
    answer_type: str
    computation_type: str
    original_action: str  # "finish" (silent failures are always finish)
    replay_action: str
    caught: bool       # True if replay action != "finish"
    reasoning: str
    error: str = ""


def load_cases(split: str | None) -> list[dict]:
    cases_path = REPLAY_DIR / "cases.jsonl"
    if not cases_path.exists():
        sys.exit(f"Missing {cases_path}. Run build_replay_set.py first.")
    cases = [json.loads(line) for line in cases_path.read_text().splitlines() if line.strip()]
    if split:
        split_path = REPLAY_DIR / "split.json"
        split_data = json.loads(split_path.read_text())
        ids = set(split_data.get(split, []))
        cases = [c for c in cases if c["case_id"] in ids]
    return cases


def load_step_artifacts(eval_dir: str, task_id: str) -> tuple[str, str, str, str] | None:
    """Return (code, language, stdout, structured_json) for the FINAL iteration.

    None on failure to read.
    """
    workspace_steps = RESULTS_DIR / eval_dir / task_id / "workspace" / "steps"
    if not workspace_steps.exists():
        return None
    # Highest step index = final iteration
    code_files = sorted(workspace_steps.glob("step_*_code.*"))
    if not code_files:
        return None
    last_code = code_files[-1]
    code = last_code.read_text()
    language = "sql" if last_code.suffix == ".sql" else "python"

    # Output file matching the same step idx
    base = last_code.stem  # e.g. "step_3_code"
    idx = base.split("_")[1]
    out_path = workspace_steps / f"step_{idx}_output.txt"
    stdout = out_path.read_text() if out_path.exists() else ""

    # Structured JSON from step_result.json if present (current state — best effort)
    sr_path = RESULTS_DIR / eval_dir / task_id / "step_result.json"
    structured_json = sr_path.read_text() if sr_path.exists() else ""
    if not structured_json:
        # Try workspace-level alternative
        alt = workspace_steps.parent / "step_result.json"
        if alt.exists():
            structured_json = alt.read_text()

    return code, language, stdout, structured_json


def reconstruct_state(case: dict) -> AnalysisState | None:
    """Re-profile + read workspace artifacts to rebuild Judge's view."""
    task_id = case["task_id"]
    task_dir = TASK_INPUT_DIR / task_id
    if not task_dir.exists():
        return None

    try:
        m = profiler.scan(str(task_dir))
    except Exception:
        return None
    manifest_summary = manifest_to_json(m)
    try:
        domain_rules = analyzer._extract_domain_rules(m)
    except Exception:
        domain_rules = ""

    artifacts = load_step_artifacts(case["eval_dir"], task_id)
    if artifacts is None:
        return None
    code, _language, stdout, structured_json = artifacts

    plan = PlanStep(step_description="replay step", expected_output="")
    result = SandboxResult(
        stdout=stdout,
        stderr="",
        return_code=0,
        execution_time_ms=10,
        step_id="replay",
        structured_json=structured_json,
    )
    step = StepRecord(plan=plan, code=code, result=result, step_index=0)

    # Read question from task.json
    try:
        task_meta = json.loads((task_dir / "task.json").read_text())
        question = task_meta.get("question", "")
    except Exception:
        question = ""

    return AnalysisState(
        task_id=task_id,
        question=question,
        manifest_summary=manifest_summary,
        data_profile_summary="",
        domain_rules=domain_rules,
        full_step_details=(step,),
        completed_steps=(f"Step 0: {plan.step_description}",),
    )


def run_one(case: dict, llm, prompt_override: str | None) -> ReplayResult:
    state = reconstruct_state(case)
    if state is None:
        return ReplayResult(
            case_id=case["case_id"], eval_dir=case["eval_dir"], task_id=case["task_id"],
            answer_type=case["answer_type"], computation_type=case["computation_type"],
            original_action="finish", replay_action="error", caught=False,
            reasoning="", error="reconstruction failed",
        )

    spec = QuestionSpec(
        answer_type=case.get("answer_type", "unknown"),
        computation_type=case.get("computation_type", "unknown"),
        expected_row_count=case.get("expected_row_count", "unknown"),
        tie_possible=case.get("tie_possible", False),
    )
    cm = ContextManager(token_limit=TOKEN_LIMIT)

    # Optional prompt override — temporarily monkey-patch the judge module's
    # template path. We do this by writing the override to a temp file and
    # patching read_text behavior. Simpler: write to dataline/prompts/judge.md
    # location is too risky. We instead use a context manager around the call.
    if prompt_override:
        # Patch the path resolution inside judge.evaluate by replacing
        # the file's content for this call.
        prompt_path = Path(judge_agent.__file__).parent.parent / "prompts" / "judge.md"
        original = prompt_path.read_text(encoding="utf-8")
        new_template = Path(prompt_override).read_text(encoding="utf-8")
        prompt_path.write_text(new_template, encoding="utf-8")
        try:
            decision = judge_agent.evaluate(
                question=state.question, steps_done=[], llm=llm,
                state=state, cm=cm,
                iteration=0, max_iterations=8,
                question_spec=spec,
            )
        finally:
            prompt_path.write_text(original, encoding="utf-8")
    else:
        try:
            decision = judge_agent.evaluate(
                question=state.question, steps_done=[], llm=llm,
                state=state, cm=cm,
                iteration=0, max_iterations=8,
                question_spec=spec,
            )
        except Exception as e:
            return ReplayResult(
                case_id=case["case_id"], eval_dir=case["eval_dir"], task_id=case["task_id"],
                answer_type=case["answer_type"], computation_type=case["computation_type"],
                original_action="finish", replay_action="error", caught=False,
                reasoning="", error=str(e)[:200],
            )

    action = (decision.action or "continue").lower()
    return ReplayResult(
        case_id=case["case_id"], eval_dir=case["eval_dir"], task_id=case["task_id"],
        answer_type=case["answer_type"], computation_type=case["computation_type"],
        original_action="finish", replay_action=action,
        caught=(action != "finish"),
        reasoning=(decision.reasoning or "")[:200],
    )


def write_report(results: list[ReplayResult], label: str, wall_s: float,
                 model: str, split_used: str | None, prompt_override: str | None):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DOCS_DIR / f"JUDGE_REPLAY_{label}.md"

    n = len(results)
    n_caught = sum(1 for r in results if r.caught)
    n_finish = sum(1 for r in results if r.replay_action == "finish")
    n_error = sum(1 for r in results if r.replay_action == "error")
    catch_rate = (n_caught / n * 100) if n else 0
    reproduction_rate = (n_finish / (n - n_error) * 100) if (n - n_error) else 0

    by_shape: dict[tuple, list[ReplayResult]] = {}
    for r in results:
        key = (r.answer_type, r.computation_type)
        by_shape.setdefault(key, []).append(r)

    lines = [
        f"# Judge Replay — `{label}`",
        "",
        "_Generated by `scripts/run_replay.py`. Re-run to refresh._",
        "",
        f"**Model**: `{model}`",
        f"**Split**: `{split_used or 'all'}`  |  **Cases**: {n}  "
        f"|  **Wall time**: {wall_s:.0f}s",
        f"**Prompt override**: `{prompt_override or 'baseline (dataline/prompts/judge.md)'}`",
        "",
        "## Headline",
        "",
        f"- **Catch rate**: {n_caught}/{n} = **{catch_rate:.0f}%** "
        "(replay says continue/backtrack — would have caught silent failure)",
        f"- **Reproduction**: {n_finish}/{n - n_error} = "
        f"{reproduction_rate:.0f}% (replay reproduces original `finish` — "
        "reconstruction fidelity)",
        f"- **Errors**: {n_error} (reconstruction or LLM failures)",
        "",
        "## By question shape",
        "",
        "| (answer_type, computation_type) | cases | caught | catch_rate |",
        "|---|---|---|---|",
    ]
    for key in sorted(by_shape):
        rs = by_shape[key]
        c = sum(1 for r in rs if r.caught)
        rate = (c / len(rs) * 100) if rs else 0
        lines.append(f"| `{key[0]} / {key[1]}` | {len(rs)} | {c} | {rate:.0f}% |")

    lines.extend(["", "## Per-case detail (first 50)", "",
                  "| case_id | shape | replay_action | caught | reasoning |",
                  "|---|---|---|---|---|"])
    for r in results[:50]:
        shape = f"{r.answer_type}/{r.computation_type}"
        marker = "✅" if r.caught else "—"
        reasoning = (r.reasoning or r.error or "").replace("|", "/").replace("\n", " ")[:120]
        lines.append(
            f"| `{r.case_id[-40:]}` | {shape} | `{r.replay_action}` | "
            f"{marker} | {reasoning} |"
        )
    if len(results) > 50:
        lines.append(f"| _(... {len(results) - 50} more rows truncated)_ |||||")

    out_path.write_text("\n".join(lines))
    print(f"Report written: {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["train", "dev"], default=None,
                    help="Which split to run on (default: all 80 cases)")
    ap.add_argument("--prompt-override", default=None,
                    help="Path to alternate judge.md prompt to test")
    ap.add_argument("--label", default="baseline",
                    help="Label for output report file")
    args = ap.parse_args()

    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    llm = create_client_from_config(cfg)
    model = os.environ.get("MODEL_NAME", cfg["llm"].get("model", "?"))
    cases = load_cases(args.split)
    print(f"Model: {model}  |  cases: {len(cases)}  |  split: {args.split or 'all'}")
    if args.prompt_override:
        print(f"Prompt override: {args.prompt_override}")

    results: list[ReplayResult] = []
    t0 = time.time()
    for i, case in enumerate(cases):
        r = run_one(case, llm, args.prompt_override)
        results.append(r)
        marker = "✅" if r.caught else "—"
        err = f" ERR:{r.error[:60]}" if r.error else ""
        print(f"  [{i + 1:>2}/{len(cases)}] {marker} {r.case_id[-35:]:<35}  "
              f"action={r.replay_action}{err}")
    elapsed = time.time() - t0
    print(f"\nFinished {len(cases)} cases in {elapsed:.0f}s.")
    write_report(results, args.label, elapsed, model, args.split, args.prompt_override)


if __name__ == "__main__":
    main()
