"""KDD Cup 2026 submission entry point.

Container behavior:
- Iterates every directory under /input that contains task.json.
- Runs the agent on each task using config.yaml + env-injected
  MODEL_API_URL / MODEL_API_KEY / MODEL_NAME.
- Writes /output/<task_id>/prediction.csv per task.
- All stdout/stderr is tee'd to /logs/runtime.log by the ENTRYPOINT.

This file is the ONLY entry point the evaluator runs. Keep it
self-contained: no argparse, hardcoded mount paths, fail-soft on
per-task errors so one bad task never kills the whole batch.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml

INPUT_ROOT = Path("/input")
OUTPUT_ROOT = Path("/output")
LOGS_ROOT = Path("/logs")


def run_one(task_dir_str: str, config: dict, session_id: str) -> tuple[str, str, float]:
    """Run a single task. Returns (task_id, status, elapsed_seconds).

    Always writes a prediction.csv (even on error) so the evaluator
    sees a file for every task — fall-back is an empty CSV.
    """
    task_dir = Path(task_dir_str)
    task_id = task_dir.name
    out_dir = OUTPUT_ROOT / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pred_path = out_dir / "prediction.csv"

    start = time.time()
    try:
        meta_path = task_dir / "task.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"missing task.json in {task_dir}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        question = meta.get("question", "").strip()
        if not question:
            raise ValueError(f"empty question in {meta_path}")

        from dataline.core.llm_client import create_client_from_config
        # Use the HeavySkill wrapper so heavy_mode.enabled=true in config.yaml
        # activates K-trajectory + deliberation on uncertain tasks. When the
        # flag is false, run_task_heavy degenerates to plain run_task with
        # zero overhead. This is the production code path for v77+.
        from dataline.agents.heavy_runner import run_task_heavy as run_task
        from dataline.synthesizer.base import save_prediction

        llm = create_client_from_config(config)
        result = run_task(
            task_dir=str(task_dir),
            question=question,
            llm=llm,
            config=config,
            task_id=task_id,
            output_dir=str(out_dir),
            benchmark="kdd",
            session_id=session_id,
        )
        save_prediction(result.answer, str(pred_path))
        return task_id, "ok", time.time() - start
    except Exception as exc:
        # Fail-soft: never let one task crash the whole batch.
        # Write a minimal valid CSV so the evaluator doesn't 404.
        if not pred_path.exists():
            try:
                pred_path.write_text("answer\n", encoding="utf-8")
            except OSError:
                pass
        # Log full traceback for /logs review.
        print(f"[task {task_id}] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return task_id, f"error:{type(exc).__name__}", time.time() - start


def main() -> int:
    LOGS_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"[submit] start {datetime.now().isoformat()}")
    print(f"[submit] MODEL_API_URL = {os.environ.get('MODEL_API_URL', '<unset>')}")
    print(f"[submit] MODEL_NAME    = {os.environ.get('MODEL_NAME', '<unset>')}")
    print(f"[submit] MODEL_API_KEY = {'<set>' if os.environ.get('MODEL_API_KEY') else '<unset>'}")
    print(f"[submit] CWD           = {os.getcwd()}")

    if not INPUT_ROOT.exists():
        print(f"[submit] FATAL: {INPUT_ROOT} not mounted", file=sys.stderr)
        return 1

    config_path = Path(__file__).parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    # LPT (Longest Processing Time first) — sort by difficulty so the heaviest
    # tasks start while every worker is still idle. With parallel=8 this saves
    # ~20-30% wall time vs lexicographic order because the long-tail hard tasks
    # no longer cluster at the end of the batch. task.json carries the
    # difficulty label per KDD spec; unknown / missing → medium fallback.
    _DIFF_RANK = {"extreme": 0, "hard": 1, "medium": 2, "easy": 3}

    def _task_priority(task_dir: Path) -> tuple[int, str]:
        try:
            meta = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            diff = meta.get("difficulty", "medium")
        except Exception:
            diff = "medium"
        return (_DIFF_RANK.get(diff, 2), task_dir.name)

    tasks = sorted(
        (p for p in INPUT_ROOT.iterdir()
         if p.is_dir() and (p / "task.json").exists()),
        key=_task_priority,
    )
    print(f"[submit] {len(tasks)} tasks discovered in {INPUT_ROOT}")
    if tasks:
        # Show first/last 3 task IDs so the ordering is auditable in logs.
        first_3 = [t.name for t in tasks[:3]]
        last_3 = [t.name for t in tasks[-3:]]
        print(f"[submit] LPT order — first 3: {first_3}  ...  last 3: {last_3}")
    if not tasks:
        print(f"[submit] WARNING: zero tasks found", file=sys.stderr)
        return 0

    parallel = int(config.get("batch", {}).get("parallel", 1))
    parallel = max(1, min(parallel, 16))  # clamp to physical CPU cap
    print(f"[submit] parallel = {parallel}")

    session_id = f"submit__{datetime.now():%Y%m%d_%H%M%S}"
    start = time.time()
    completed = 0
    errors = 0
    total = len(tasks)

    if parallel > 1:
        with ProcessPoolExecutor(max_workers=parallel) as exe:
            futures = {exe.submit(run_one, str(t), config, session_id): t.name
                       for t in tasks}
            for fut in as_completed(futures):
                try:
                    tid, status, dur = fut.result()
                except Exception as exc:
                    tid = futures[fut]
                    status = f"worker_crash:{type(exc).__name__}"
                    dur = 0.0
                completed += 1
                if status.startswith("error") or status.startswith("worker"):
                    errors += 1
                print(f"[{completed}/{total}] {tid:14} {status:25} {dur:6.1f}s",
                      flush=True)
    else:
        for t in tasks:
            tid, status, dur = run_one(str(t), config, session_id)
            completed += 1
            if status.startswith("error"):
                errors += 1
            print(f"[{completed}/{total}] {tid:14} {status:25} {dur:6.1f}s",
                  flush=True)

    elapsed = time.time() - start
    print(f"[submit] DONE in {elapsed:.0f}s — {completed} tasks, "
          f"{errors} errors, {completed - errors} ok")
    print(f"[submit] end {datetime.now().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
