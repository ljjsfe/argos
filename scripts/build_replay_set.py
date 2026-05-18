"""Build the Judge replay set from historical silent failures.

Walks recent eval runs (v70+) and picks tasks where:
  - score < 1.0 (wrong answer)
  - final iteration's judge_action = "finish" (Judge accepted)
  - final iteration has no HarnessGate flags (truly silent — Judge alone failed)
  - workspace/steps/* present (we can read the actual code Judge saw)
  - public/input/task_<id>/ present (we can re-profile to rebuild manifest)

Outputs:
  replay_set/cases.jsonl  — one JSON object per case (metadata + code refs)
  replay_set/split.json   — train/dev split (deterministic, seed=42)

Each case is a reference (eval_dir + task_id) — run_replay.py reconstructs
the full Judge inputs on demand. This keeps the file small and lets us
re-profile if the profiler changes.

Generality note: replay cases include the question text and code, which
DO reference KDD demo schemas. That is acceptable here because the replay
set is a DIAGNOSTIC tool, not a training source — we never tune prompts
against the dev split, and the train split is only used as a sanity check
for prompt iteration. The capability being measured (does Judge catch
silent failures given concrete context) is universal.

Run:
    python3 scripts/build_replay_set.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

REPLAY_DIR = REPO_ROOT / "replay_set"
RESULTS_DIR = REPO_ROOT / "results"
TASK_INPUT_DIR = REPO_ROOT / "public" / "input"

# Only consider eval runs from v70+ (modern architecture: heavy mode era)
MIN_VERSION = 70

# Target replay-set size and train/dev split
TARGET_SIZE = 80
TRAIN_FRACTION = 0.5
SEED = 42


def eval_version(eval_dir_name: str) -> int:
    import re
    m = re.search(r"eval_v(\d+)", eval_dir_name)
    return int(m.group(1)) if m else 0


def is_silent_failure(task_score: dict, trace: dict) -> bool:
    """Did Judge silently approve a wrong answer with no harness signal?"""
    if task_score.get("score", 0.0) >= 1.0:
        return False
    iters = (trace.get("observations") or {}).get("iterations") or []
    if not iters:
        return False
    last = iters[-1]
    if not isinstance(last, dict):
        return False
    judge_action = (last.get("judge_action") or "").lower()
    if not judge_action.startswith("finish"):
        return False
    flags = last.get("harness_flags") or []
    # "Silent" = no flag at all (not even WARN that Judge ignored)
    return not flags


def has_reconstructable_artifacts(eval_dir: Path, task_id: str) -> bool:
    """Both workspace code and raw task input must be readable."""
    workspace_steps = eval_dir / task_id / "workspace" / "steps"
    if not workspace_steps.exists():
        return False
    # At least one step_*_code file
    code_files = list(workspace_steps.glob("step_*_code.*"))
    if not code_files:
        return False
    task_input = TASK_INPUT_DIR / task_id
    return task_input.exists() and (task_input / "task.json").exists()


def collect_candidates() -> list[dict]:
    """Walk eval dirs, return list of candidate-case dicts."""
    candidates: list[dict] = []
    for eval_dir in sorted(RESULTS_DIR.glob("eval_v*/")):
        if eval_version(eval_dir.name) < MIN_VERSION:
            continue
        report_path = eval_dir / "eval_report_kdd.json"
        if not report_path.exists():
            continue
        try:
            report = json.loads(report_path.read_text())
        except Exception:
            continue

        for ts in report.get("task_scores", []):
            task_id = ts.get("task_id", "")
            if not task_id or "__" in task_id:
                continue  # skip heavy trajectory siblings

            trace_path = eval_dir / task_id / "trace.json"
            if not trace_path.exists():
                continue
            try:
                trace = json.loads(trace_path.read_text())
            except Exception:
                continue

            if not is_silent_failure(ts, trace):
                continue
            if not has_reconstructable_artifacts(eval_dir, task_id):
                continue

            obs = trace.get("observations") or {}
            qs = obs.get("question_spec") or {}
            candidates.append({
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
    return candidates


def stratified_sample(candidates: list[dict], target: int) -> list[dict]:
    """Sample by (answer_type, computation_type) shape so the replay set
    isn't dominated by one question kind."""
    rng = random.Random(SEED)

    # Bucket
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for c in candidates:
        key = (c["answer_type"], c["computation_type"])
        buckets[key].append(c)

    # First pass: keep up to ceil(target / num_buckets) per bucket
    per_bucket = max(1, target // max(1, len(buckets)))
    picked: list[dict] = []
    for key, items in sorted(buckets.items()):
        # Within bucket, prefer more recent eval (closer to current architecture)
        items_sorted = sorted(items, key=lambda c: -eval_version(c["eval_dir"]))
        rng.shuffle(items_sorted[: 2 * per_bucket])  # mild randomness within recent
        picked.extend(items_sorted[:per_bucket])

    # Fill remainder up to target by sampling from remaining pool
    remaining = [c for c in candidates if c not in picked]
    rng.shuffle(remaining)
    short = target - len(picked)
    if short > 0:
        picked.extend(remaining[:short])

    # De-dup by case_id (paranoia)
    seen = set()
    deduped = []
    for c in picked:
        if c["case_id"] in seen:
            continue
        seen.add(c["case_id"])
        deduped.append(c)
    return deduped[:target]


def split_train_dev(cases: list[dict]) -> dict:
    rng = random.Random(SEED + 1)
    shuffled = cases[:]
    rng.shuffle(shuffled)
    n_train = int(len(shuffled) * TRAIN_FRACTION)
    train_ids = [c["case_id"] for c in shuffled[:n_train]]
    dev_ids = [c["case_id"] for c in shuffled[n_train:]]
    return {"train": train_ids, "dev": dev_ids, "seed": SEED + 1}


def main():
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Walking {RESULTS_DIR} for eval runs v{MIN_VERSION}+ ...")
    candidates = collect_candidates()
    print(f"  Found {len(candidates)} silent-failure candidates.")

    # Distribution snapshot
    shape_counts = Counter(
        (c["answer_type"], c["computation_type"]) for c in candidates
    )
    print(f"  Shape distribution (top 8):")
    for shape, n in shape_counts.most_common(8):
        print(f"    {shape}: {n}")

    sampled = stratified_sample(candidates, TARGET_SIZE)
    print(f"  Stratified-sampled {len(sampled)} cases.")

    # Write cases.jsonl
    cases_path = REPLAY_DIR / "cases.jsonl"
    with cases_path.open("w") as f:
        for c in sampled:
            f.write(json.dumps(c) + "\n")
    print(f"  Wrote {cases_path}")

    # Train/dev split
    split = split_train_dev(sampled)
    split_path = REPLAY_DIR / "split.json"
    split_path.write_text(json.dumps(split, indent=2))
    print(f"  Wrote {split_path}: train={len(split['train'])}, "
          f"dev={len(split['dev'])}")


if __name__ == "__main__":
    main()
