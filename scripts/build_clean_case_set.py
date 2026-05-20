"""Build a 'Judge-was-right-to-finish' set — inverse of the silent failures.

Picks cases where the production run:
  - score == 1.0 (answer was correct)
  - final judge_action = "finish" (Judge accepted)
  - final iteration has no HarnessGate flags (clean trace)
  - workspace + raw task input present (replay-able)

These are cases where current judge.md is provably correct. We use this set
to measure FALSE POSITIVE rate of any new Judge prompt — how often it
wrongly says continue/backtrack on an answer that was already right.

Outputs:
  replay_set/clean_cases.jsonl
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

REPLAY_DIR = REPO_ROOT / "replay_set"
RESULTS_DIR = REPO_ROOT / "results"
TASK_INPUT_DIR = REPO_ROOT / "public" / "input"

MIN_VERSION = 70
TARGET_SIZE = 40  # matches dev split of silent failures
SEED = 137


def eval_version(name: str) -> int:
    m = re.search(r"eval_v(\d+)", name)
    return int(m.group(1)) if m else 0


def is_clean_success(task_score: dict, trace: dict) -> bool:
    if task_score.get("score", 0.0) < 1.0:
        return False
    iters = (trace.get("observations") or {}).get("iterations") or []
    if not iters:
        return False
    last = iters[-1]
    if not isinstance(last, dict):
        return False
    if not (last.get("judge_action") or "").lower().startswith("finish"):
        return False
    return not (last.get("harness_flags") or [])


def has_artifacts(eval_dir: Path, task_id: str) -> bool:
    workspace_steps = eval_dir / task_id / "workspace" / "steps"
    if not workspace_steps.exists():
        return False
    if not list(workspace_steps.glob("step_*_code.*")):
        return False
    task_input = TASK_INPUT_DIR / task_id
    return task_input.exists() and (task_input / "task.json").exists()


def collect_candidates() -> list[dict]:
    out: list[dict] = []
    for eval_dir in sorted(RESULTS_DIR.glob("eval_v*/")):
        if eval_version(eval_dir.name) < MIN_VERSION:
            continue
        report = eval_dir / "eval_report_kdd.json"
        if not report.exists():
            continue
        try:
            data = json.loads(report.read_text())
        except Exception:
            continue
        for ts in data.get("task_scores", []):
            task_id = ts.get("task_id", "")
            if not task_id or "__" in task_id:
                continue
            trace_path = eval_dir / task_id / "trace.json"
            if not trace_path.exists():
                continue
            try:
                trace = json.loads(trace_path.read_text())
            except Exception:
                continue
            if not is_clean_success(ts, trace):
                continue
            if not has_artifacts(eval_dir, task_id):
                continue
            obs = trace.get("observations") or {}
            qs = obs.get("question_spec") or {}
            out.append({
                "case_id": f"{eval_dir.name}__{task_id}",
                "eval_dir": eval_dir.name,
                "task_id": task_id,
                "score": ts.get("score", 0.0),
                "difficulty": ts.get("difficulty", ""),
                "answer_type": qs.get("answer_type", "unknown"),
                "computation_type": qs.get("computation_type", "unknown"),
                "expected_row_count": qs.get("expected_row_count", "unknown"),
                "tie_possible": qs.get("tie_possible", False),
                "num_iterations": len(obs.get("iterations") or []),
            })
    return out


def stratified_sample(cands: list[dict], n: int) -> list[dict]:
    rng = random.Random(SEED)
    buckets = defaultdict(list)
    for c in cands:
        buckets[(c["answer_type"], c["computation_type"])].append(c)
    per = max(1, n // max(1, len(buckets)))
    picked: list[dict] = []
    for k in sorted(buckets):
        items = sorted(buckets[k], key=lambda c: -eval_version(c["eval_dir"]))
        # Take the top 2*per candidates by recency, then shuffle ON THE COPY
        # and re-bind so we actually pick a random subset of the recent pool
        # rather than the deterministic head. The previous `rng.shuffle(items[:2*per])`
        # was a no-op — shuffle mutates the slice copy, then it's discarded.
        head = items[: 2 * per]
        rng.shuffle(head)
        picked.extend(head[:per])
    rng.shuffle([c for c in cands if c not in picked])
    pool = [c for c in cands if c not in picked]
    rng.shuffle(pool)
    short = n - len(picked)
    if short > 0:
        picked.extend(pool[:short])
    seen, dedup = set(), []
    for c in picked:
        if c["case_id"] not in seen:
            seen.add(c["case_id"])
            dedup.append(c)
    return dedup[:n]


def main():
    REPLAY_DIR.mkdir(exist_ok=True)
    cands = collect_candidates()
    print(f"Found {len(cands)} clean-success candidates from v{MIN_VERSION}+ runs.")
    shape_dist = Counter((c["answer_type"], c["computation_type"]) for c in cands)
    for shape, ct in shape_dist.most_common(8):
        print(f"  {shape}: {ct}")
    sampled = stratified_sample(cands, TARGET_SIZE)
    out_path = REPLAY_DIR / "clean_cases.jsonl"
    with out_path.open("w") as f:
        for c in sampled:
            f.write(json.dumps(c) + "\n")
    print(f"Wrote {out_path}  ({len(sampled)} cases)")


if __name__ == "__main__":
    main()
