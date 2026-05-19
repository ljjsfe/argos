"""Run SC1-C1 or SC1-C2 Qwen experiment.

For each task and each arm, sends the skeleton .py + question to Qwen, asks it
to fill all TODOs, saves the completion as _C<arm>_output.py, executes it, and
scores the resulting prediction.csv.

Usage:
    python3 scripts/run_sc1_qwen.py --arm C1 --tasks 86,163,180,344,352,418
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dataline.core.llm_client import create_client_from_config  # noqa: E402
from dataline.eval.scorer import score_task  # noqa: E402


SYSTEM_PROMPT = textwrap.dedent(
    """\
    You are a senior data engineer completing a Python script.

    The user will give you:
    - A natural-language question.
    - A Python SKELETON containing imports, data loads, probe prints, and
      `# TODO:` comments next to placeholder variables (assigned to None).

    Your job: fill in the TODO sections so the script computes the correct
    answer to the question and writes prediction.csv. Do NOT change imports,
    paths, or the PROBE block. Do NOT add new imports.

    Output ONLY a complete Python script (no markdown, no commentary), ready
    to run as-is."""
)


def build_user(question: str, skeleton: str, probe_output: str = "") -> str:
    probe_section = ""
    if probe_output:
        probe_section = (
            f"\nPROBE OUTPUT (what running the skeleton's probe block prints "
            f"— this is the actual data shape you must use):\n```\n{probe_output}\n```\n"
        )
    return (
        f"QUESTION:\n{question}\n"
        f"{probe_section}\n"
        f"SKELETON (fill the TODOs):\n```python\n{skeleton}\n```\n\n"
        f"Output the complete Python script."
    )


def run_probe(skeleton_path: Path) -> str:
    """Run the skeleton up to the TODO section, capture probe stdout."""
    text = skeleton_path.read_text()
    # Truncate at first TODO line so we only execute imports + load + probe.
    cut_at = text.find("# TODO:")
    if cut_at == -1:
        cut_at = len(text)
    probe_only = text[:cut_at] + "\nimport sys; sys.exit(0)\n"
    tmp = skeleton_path.with_suffix(".probe.py")
    tmp.write_text(probe_only)
    try:
        proc = subprocess.run(
            [sys.executable, str(tmp)],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (proc.stdout or "")[-3000:]  # cap to last 3KB
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


CODE_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)\n```", re.DOTALL)


def extract_code(text: str) -> str:
    m = CODE_FENCE.search(text)
    if m:
        return m.group(1)
    return text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["C1", "C2"], required=True)
    ap.add_argument("--tasks", default="86,163,180,344,352,418")
    args = ap.parse_args()

    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    client = create_client_from_config(cfg)
    arm = args.arm
    tasks = args.tasks.split(",")

    print(f"Arm {arm}, tasks: {tasks}, model: {client._config.model}")

    rows = []
    for tid in tasks:
        skeleton_path = REPO / f"eval_split/skeletons/task_{tid}_{arm}.py"
        if not skeleton_path.exists():
            print(f"  task_{tid}: SKIP — no skeleton")
            rows.append((tid, "SKIP", None))
            continue

        question_path = REPO / f"public/input/task_{tid}/task.json"
        question = json.loads(question_path.read_text())["question"]
        skeleton = skeleton_path.read_text()

        print(f"  task_{tid}: running probe ...", flush=True)
        try:
            probe_out = run_probe(skeleton_path)
        except Exception as e:
            print(f"    probe error: {e}")
            probe_out = ""
        print(f"  task_{tid}: calling Qwen (probe={len(probe_out)} chars) ...", flush=True)
        try:
            response = client.chat(SYSTEM_PROMPT, build_user(question, skeleton, probe_out))
        except Exception as e:
            print(f"    LLM error: {e}")
            rows.append((tid, "LLM_ERROR", None))
            continue

        code = extract_code(response)
        out_path = REPO / f"eval_split/skeletons/task_{tid}_{arm}_output.py"
        out_path.write_text(code)

        # Run the generated code
        try:
            proc = subprocess.run(
                [sys.executable, str(out_path)],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print("    TIMEOUT")
            rows.append((tid, "TIMEOUT", None))
            continue
        if proc.returncode != 0:
            print(f"    EXEC FAIL rc={proc.returncode}")
            tail = (proc.stderr or "")[-300:]
            print(f"    stderr tail: {tail}")
            rows.append((tid, "EXEC_FAIL", None))
            continue

        # Score
        pred_path = REPO / f"eval_split/skeletons/_pred_task_{tid}_{arm}/prediction.csv"
        gold_path = REPO / f"public/output/task_{tid}/gold.csv"
        if not pred_path.exists():
            print("    NO_PREDICTION")
            rows.append((tid, "NO_PRED", None))
            continue
        try:
            pred = pd.read_csv(pred_path)
            gold = pd.read_csv(gold_path)
            score = score_task(pred, gold)
        except Exception as e:
            print(f"    SCORE_ERR: {e}")
            rows.append((tid, "SCORE_ERR", None))
            continue

        print(f"    score = {score}")
        rows.append((tid, "OK", score))

    # Summary
    print()
    print(f"=== SC1-{arm} summary ===")
    print(f"{'task':<10}  {'status':<12}  {'score'}")
    ok_pass = 0
    for tid, status, score in rows:
        print(f"task_{tid:<5}  {status:<12}  {score}")
        if status == "OK" and score is not None and score >= 0.9:
            ok_pass += 1
    print(f"\npass (Score>=0.9): {ok_pass}/{len(rows)}")


if __name__ == "__main__":
    main()
