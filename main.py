"""dataline CLI — data analysis agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(override=True)  # override=True so .env wins over pre-set empty env vars


def main():
    parser = argparse.ArgumentParser(description="dataline: data analysis agent")
    subparsers = parser.add_subparsers(dest="command")

    # --- run: analyze a single task ---
    run_parser = subparsers.add_parser("run", help="Run analysis on a task")
    run_parser.add_argument("--task", required=True, help="Path to task directory")
    run_parser.add_argument("--question", help="Question (default: from task.json)")
    run_parser.add_argument("--output", default="./results", help="Output directory")
    run_parser.add_argument("--config", default="config.yaml", help="Config file")
    run_parser.add_argument("--benchmark", default="kdd", help="Benchmark: kdd | dabstep")
    run_parser.add_argument(
        "--heavy-mode",
        choices=["auto", "always", "off"],
        default=None,
        help="HeavySkill heavy mode: auto (confidence-triggered) | always | off (default: config)",
    )

    # --- eval: batch evaluation ---
    eval_parser = subparsers.add_parser("eval", help="Evaluate results against gold")
    eval_parser.add_argument("--results", required=True, help="Results directory")
    eval_parser.add_argument("--benchmark", default="kdd", help="Benchmark: kdd | dabstep")
    eval_parser.add_argument("--gold", default="public", help="Gold dir (KDD) or DABstep root")
    eval_parser.add_argument("--label", default="", help="Run label for the log")
    eval_parser.add_argument("--model", default="", help="Model used (for log)")
    eval_parser.add_argument("--provider", default="", help="Provider used (for log)")
    eval_parser.add_argument("--no-log", action="store_true", help="Skip logging this run")

    # --- history: show run history dashboard ---
    history_parser = subparsers.add_parser("history", help="Show eval run history")
    history_parser.add_argument("--benchmark", default=None, help="Filter by benchmark")
    history_parser.add_argument("--last", type=int, default=20, help="Show last N runs")
    history_parser.add_argument("--best", action="store_true", help="Show best runs only")

    # --- batch: run on all tasks ---
    batch_parser = subparsers.add_parser("batch", help="Run on all tasks in a dataset")
    batch_parser.add_argument("--benchmark", default="kdd", help="Benchmark: kdd | dabstep")
    batch_parser.add_argument("--data", default="public", help="Dataset root directory")
    batch_parser.add_argument("--output", default="./results", help="Output directory")
    batch_parser.add_argument("--config", default="config.yaml", help="Config file")
    batch_parser.add_argument("--tasks", nargs="*", help="Specific task IDs (default: all)")
    batch_parser.add_argument(
        "--sample",
        default="full",
        help="Sampling mode: 'dev' (fixed harder-biased 10-task set), 'full' (all), or integer N (random N tasks)",
    )
    batch_parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="Number of tasks to run in parallel (default: read from config.yaml batch.parallel)",
    )
    batch_parser.add_argument(
        "--heavy-mode",
        choices=["auto", "always", "off"],
        default=None,
        help="HeavySkill heavy mode: auto (confidence-triggered) | always | off (default: config)",
    )

    args = parser.parse_args()

    if args.command == "run":
        _cmd_run(args)
    elif args.command == "eval":
        _cmd_eval(args)
    elif args.command == "batch":
        _cmd_batch(args)
    elif args.command == "history":
        _cmd_history(args)
    else:
        parser.print_help()


def _cmd_run(args):
    """Run analysis on a single task."""
    from datetime import datetime
    config = _load_config(args.config)
    question = args.question

    # Load question from task.json if not provided
    if not question:
        task_json = os.path.join(args.task, "task.json")
        if os.path.exists(task_json):
            with open(task_json) as f:
                task_data = json.load(f)
                question = task_data.get("question", "")
        if not question:
            print("Error: no question provided and no task.json found", file=sys.stderr)
            sys.exit(1)

    task_id = os.path.basename(os.path.normpath(args.task))
    session_id = f"single__{task_id}__{datetime.now():%Y%m%d_%H%M%S}"

    from dataline.core.llm_client import create_client_from_config
    from dataline.agents.heavy_runner import run_task_heavy
    from dataline.synthesizer.base import save_prediction

    # CLI --heavy-mode overrides config
    _apply_heavy_mode_cli(config, getattr(args, "heavy_mode", None))

    llm = create_client_from_config(config)

    heavy_active = bool((config.get("heavy_mode") or {}).get("enabled")) and (
        (config.get("heavy_mode") or {}).get("trigger", "auto") != "off"
    )

    print(f"Running task: {task_id}")
    print(f"Question: {question}")
    print(f"Model: {config['llm']['provider']}/{os.environ.get('MODEL_NAME', '') or config['llm'].get('model', '<unset>')}")
    print(f"Heavy mode: {'ON' if heavy_active else 'OFF'}")
    print(f"Session: {session_id}")
    print()

    out_dir = os.path.join(args.output, task_id)
    os.makedirs(out_dir, exist_ok=True)

    result = run_task_heavy(
        task_dir=args.task,
        question=question,
        llm=llm,
        config=config,
        task_id=task_id,
        output_dir=out_dir,
        benchmark=args.benchmark,
        session_id=session_id,
    )

    pred_path = os.path.join(out_dir, "prediction.csv")
    save_prediction(result.answer, pred_path)
    print(f"Prediction saved: {pred_path}")

    trace_path = os.path.join(out_dir, "trace.json")
    _save_trace(result, trace_path)
    print(f"Trace saved: {trace_path}")
    print(f"\nTokens: {result.total_tokens:,} | Cost: ${result.total_cost_usd:.3f} | Time: {result.time_seconds:.1f}s")


def _cmd_eval(args):
    """Run evaluation for KDD or DABstep."""
    from dataline.eval.run_logger import log_run

    benchmark = args.benchmark.lower()

    if benchmark == "dabstep":
        from dataline.eval.dabstep_eval import run_dabstep_eval, format_dabstep_report
        report = run_dabstep_eval(args.results, args.gold)
        print(format_dabstep_report(report))
    else:
        from dataline.eval.run_eval import run_eval, format_report
        report = run_eval(args.results, args.gold)
        print(format_report(report))

    # Save report
    report_path = os.path.join(args.results, f"eval_report_{benchmark}.json")
    with open(report_path, "w") as f:
        json.dump({
            "benchmark": benchmark,
            "overall_accuracy": report.overall_accuracy,
            "per_difficulty": report.per_difficulty,
            "failure_breakdown": report.failure_breakdown,
            "agent_bottlenecks": report.agent_bottlenecks,
            "total_tokens": report.total_tokens,
            "total_cost_usd": report.total_cost_usd,
            "suggestions": list(report.suggestions),
            "task_scores": [
                {
                    "task_id": s.task_id,
                    "score": s.score,
                    "difficulty": s.difficulty,
                    "failure_category": s.failure_category,
                    "failed_at_agent": s.failed_at_agent,
                    "tokens_used": s.tokens_used,
                    "cost_usd": s.cost_usd,
                    "time_seconds": s.time_seconds,
                    "steps_executed": s.steps_executed,
                    "suggestion": s.suggestion,
                }
                for s in report.task_scores
            ],
        }, f, indent=2, ensure_ascii=False)
    print(f"\nReport saved: {report_path}")

    if not getattr(args, "no_log", False):
        model = args.model
        provider = args.provider
        if not model or not provider:
            try:
                config = _load_config("config.yaml")
                model = model or config["llm"]["model"]
                provider = provider or config["llm"]["provider"]
            except Exception:
                model = model or "unknown"
                provider = provider or "unknown"

        run_id = log_run(
            report=report,
            benchmark=benchmark,
            run_label=args.label or "unlabeled",
            model=model,
            provider=provider,
            sample_size=len(report.task_scores),
        )
        print(f"Run logged: {run_id}")


def _cmd_history(args):
    """Show run history dashboard."""
    from dataline.eval.run_logger import format_history, format_best_runs

    if args.best:
        print(format_best_runs())
    else:
        print(format_history(benchmark_filter=args.benchmark, last_n=args.last))


def _cmd_batch(args):
    """Run batch on KDD or DABstep tasks."""
    from datetime import datetime
    config = _load_config(args.config)
    benchmark = args.benchmark.lower()

    # CLI --heavy-mode overrides config
    _apply_heavy_mode_cli(config, getattr(args, "heavy_mode", None))

    from dataline.core.llm_client import create_client_from_config
    from dataline.agents.heavy_runner import run_task_heavy as run_task
    from dataline.synthesizer.base import save_prediction
    from dataline.eval.dev_sets import get_dev_set, describe_dev_set

    # Resolve --sample into args.tasks
    sample = getattr(args, "sample", "full")
    if sample == "dev" and not args.tasks:
        args.tasks = list(get_dev_set(benchmark))
        print(f"Sample: {describe_dev_set(benchmark)}")
        print()
    elif sample not in ("dev", "full") and not args.tasks:
        # Treat as integer N — random sample
        try:
            n = int(sample)
            args.tasks = _random_sample(benchmark, args.data, n)
            print(f"Sample: random {n} tasks from {benchmark}")
            print()
        except ValueError:
            print(f"Warning: unknown --sample value '{sample}', running full set", file=sys.stderr)

    # One session_id shared across all tasks in this batch run
    sample = getattr(args, "sample", "full")
    session_id = f"{benchmark}__{sample}__{datetime.now():%Y%m%d_%H%M%S}"
    print(f"Session: {session_id}")

    # Resolve parallelism: CLI flag > config.yaml > default 1
    if args.parallel is None:
        args.parallel = int(config.get("batch", {}).get("parallel", 1))

    if benchmark == "dabstep":
        _batch_dabstep(args, config, run_task, save_prediction, create_client_from_config, session_id)
    else:
        _batch_kdd(args, config, run_task, save_prediction, create_client_from_config, session_id)


def _random_sample(benchmark: str, data_dir: str, n: int) -> list[str]:
    """Return a reproducible random sample of N task IDs."""
    import random
    if benchmark == "dabstep":
        from dataline.eval.dabstep_eval import load_dabstep_tasks
        all_ids = sorted(load_dabstep_tasks(data_dir).keys())
    else:
        input_dir = os.path.join(data_dir, "input")
        all_ids = sorted([
            d for d in os.listdir(input_dir)
            if os.path.isdir(os.path.join(input_dir, d))
        ])
    rng = random.Random(42)  # fixed seed for reproducibility
    return rng.sample(all_ids, min(n, len(all_ids)))


def _batch_kdd(args, config, run_task, save_prediction, create_client_from_config, session_id: str = ""):
    """KDD Cup batch: task_dir per task, question from task.json.

    Supports parallel execution via args.parallel (resolved from config or CLI).
    """
    # Force absolute paths upfront — defends against chdir-induced races
    # if any worker (e.g. in-process REPL) modifies process cwd.
    input_dir = os.path.abspath(os.path.join(args.data, "input"))
    output_root = os.path.abspath(args.output)

    if args.tasks:
        task_ids = args.tasks
    else:
        # LPT (Longest Processing Time first): order by difficulty so hard/extreme
        # tasks start early and don't strand workers idle in the final stretch.
        # task.json carries the difficulty label for KDD; missing/unknown → medium.
        _DIFF_RANK = {"extreme": 0, "hard": 1, "medium": 2, "easy": 3}
        def _task_priority(tid: str) -> tuple[int, str]:
            tj = os.path.join(input_dir, tid, "task.json")
            try:
                with open(tj) as f:
                    diff = json.load(f).get("difficulty", "medium")
            except Exception:
                diff = "medium"
            return (_DIFF_RANK.get(diff, 2), tid)
        all_tids = [
            d for d in os.listdir(input_dir)
            if os.path.isdir(os.path.join(input_dir, d))
        ]
        task_ids = sorted(all_tids, key=_task_priority)

    parallel = getattr(args, "parallel", 1) or 1
    print(f"[KDD] Running {len(task_ids)} tasks | {config['llm']['provider']}/{os.environ.get('MODEL_NAME', '') or config['llm'].get('model', '<unset>')}")
    print(f"[KDD] Parallelism: {parallel}")
    print()

    os.makedirs(output_root, exist_ok=True)

    def _run_single_kdd(task_id: str) -> tuple[str, object]:
        task_dir = os.path.join(input_dir, task_id)
        task_json = os.path.join(task_dir, "task.json")

        if not os.path.exists(task_json):
            print(f"  [{task_id}] SKIP (no task.json)")
            return task_id, None

        with open(task_json) as f:
            task_data = json.load(f)
            question = task_data.get("question", "")

        out_dir = os.path.join(output_root, task_id)
        os.makedirs(out_dir, exist_ok=True)

        llm = create_client_from_config(config)
        result = run_task(
            task_dir=task_dir, question=question, llm=llm, config=config,
            task_id=task_id, output_dir=out_dir, benchmark="kdd",
            session_id=session_id,
        )

        save_prediction(result.answer, os.path.join(out_dir, "prediction.csv"))
        _save_trace(result, os.path.join(out_dir, "trace.json"))

        status = "OK" if result.success else "FAIL"
        diff = task_data.get("difficulty", "?")
        print(f"  [{task_id}] ({diff}) → {status} | {result.total_tokens:,} tok | ${result.total_cost_usd:.3f} | {result.time_seconds:.1f}s")
        return task_id, result

    if parallel <= 1:
        for i, task_id in enumerate(task_ids):
            task_dir = os.path.join(input_dir, task_id)
            task_json = os.path.join(task_dir, "task.json")
            diff = "?"
            if os.path.exists(task_json):
                with open(task_json) as f:
                    td = json.load(f)
                diff = td.get("difficulty", "?")
                q = td.get("question", "")
                print(f"[{i+1}/{len(task_ids)}] {task_id} ({diff}): {q[:80]}...")
            _run_single_kdd(task_id)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        print(f"Starting {parallel} workers...\n")
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_run_single_kdd, tid): tid for tid in task_ids}
            done = 0
            for future in as_completed(futures):
                done += 1
                try:
                    future.result()
                except Exception as exc:
                    tid = futures[future]
                    print(f"  [{tid}] ERROR: {exc}")
                print(f"  Progress: {done}/{len(task_ids)} tasks done", flush=True)

    print(f"\nDone. Evaluate with:")
    print(f"  python main.py eval --benchmark kdd --results {args.output} --gold {args.data}")


def _batch_dabstep(args, config, run_task, save_prediction, create_client_from_config, session_id: str = ""):
    """DABstep batch: shared context dir for all tasks, questions from dev_tasks.json.

    Supports parallel execution via --parallel N flag.
    Writes dashboard.json for real-time monitoring.
    """
    from dataline.eval.dabstep_eval import load_dabstep_tasks

    all_tasks = load_dabstep_tasks(args.data)

    if args.tasks:
        all_tasks = {k: v for k, v in all_tasks.items() if k in args.tasks}

    # All DABstep tasks share the same context directory
    context_dir = os.path.join(args.data, "context")
    parallel = getattr(args, "parallel", 1)

    task_list = sorted(all_tasks.items(), key=lambda x: x[0])
    print(f"[DABstep] Running {len(task_list)} tasks | {config['llm']['provider']}/{config['llm']['model']}")
    print(f"[DABstep] Shared context dir: {context_dir}")
    print(f"[DABstep] Parallelism: {parallel}")
    print(f"[DABstep] Monitor: watch -n 5 ./monitor.sh {args.output}")
    print()

    os.makedirs(args.output, exist_ok=True)
    _write_dashboard(args.output, task_list, {}, "running")

    def _run_single(task_id_meta: tuple[str, dict]) -> tuple[str, object]:
        task_id, task_meta = task_id_meta
        question = task_meta["question"]
        guidelines = task_meta.get("guidelines", "")
        level = task_meta.get("level", "?")

        full_question = question
        if guidelines:
            full_question = f"{question}\n\nGuidelines: {guidelines}"

        out_dir = os.path.join(args.output, task_id)
        os.makedirs(out_dir, exist_ok=True)

        llm = create_client_from_config(config)
        result = run_task(
            task_dir=context_dir,
            question=full_question,
            llm=llm,
            config=config,
            task_id=task_id,
            output_dir=out_dir,
            benchmark="dabstep",
            guidelines=guidelines,
            session_id=session_id,
        )

        save_prediction(result.answer, os.path.join(out_dir, "prediction.csv"))
        _save_trace(result, os.path.join(out_dir, "trace.json"))

        status = "OK" if result.success else "FAIL"
        print(f"  [{task_id}] → {status} | {result.total_tokens:,} tokens | ${result.total_cost_usd:.3f} | {result.time_seconds:.1f}s")
        return task_id, result

    results_map: dict[str, object] = {}

    if parallel <= 1:
        # Serial execution
        for i, item in enumerate(task_list):
            task_id = item[0]
            level = item[1].get("level", "?")
            question = item[1]["question"]
            print(f"[{i+1}/{len(task_list)}] task {task_id} ({level}): {question[:80]}...")
            tid, result = _run_single(item)
            results_map[tid] = result
            _write_dashboard(args.output, task_list, results_map, "running")
    else:
        # Parallel execution (thread-safe via lock)
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results_lock = threading.Lock()

        print(f"Starting {parallel} workers...\n")
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_run_single, item): item[0] for item in task_list}
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    tid, result = future.result()
                    with results_lock:
                        results_map[tid] = result
                except Exception as exc:
                    print(f"  [{task_id}] → ERROR: {exc}")
                with results_lock:
                    snapshot = dict(results_map)
                _write_dashboard(args.output, task_list, snapshot, "running")

    _write_dashboard(args.output, task_list, results_map, "completed")

    print(f"\nDone ({len(results_map)}/{len(task_list)} tasks). Evaluate with:")
    print(f"  python main.py eval --benchmark dabstep --results {args.output} --gold {args.data}")


def _write_dashboard(
    output_dir: str,
    task_list: list[tuple[str, dict]],
    results_map: dict,
    batch_status: str,
) -> None:
    """Write dashboard.json — global progress across all tasks."""
    import time as _time

    completed = len(results_map)
    total = len(task_list)
    total_tokens = 0
    total_cost = 0.0

    tasks_summary = []
    for task_id, meta in task_list:
        if task_id in results_map:
            r = results_map[task_id]
            tasks_summary.append({
                "task_id": task_id,
                "level": meta.get("level", "?"),
                "status": "ok" if getattr(r, "success", False) else "failed",
                "tokens": getattr(r, "total_tokens", 0),
                "cost_usd": round(getattr(r, "total_cost_usd", 0), 4),
                "time_s": round(getattr(r, "time_seconds", 0), 1),
            })
            total_tokens += getattr(r, "total_tokens", 0)
            total_cost += getattr(r, "total_cost_usd", 0)
        else:
            tasks_summary.append({
                "task_id": task_id,
                "level": meta.get("level", "?"),
                "status": "pending",
            })

    dashboard = {
        "batch_status": batch_status,
        "completed": completed,
        "total": total,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "timestamp": _time.time(),
        "tasks": tasks_summary,
    }

    try:
        path = os.path.join(output_dir, "dashboard.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dashboard, f, indent=2)
    except OSError as exc:
        import logging
        logging.debug("Could not write dashboard.json: %s", exc)


def _save_trace(result, trace_path: str) -> None:
    """Persist trace.json including structured observations."""
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump({
            "task_id": result.task_id,
            "question": result.question,
            "answer": result.answer,
            "trace": result.trace,
            "observations": result.observations,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost_usd,
            "time_seconds": result.time_seconds,
            "steps_executed": len(result.steps),
            "success": result.success,
            "error": result.error,
        }, f, indent=2, default=str, ensure_ascii=False)


def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _apply_heavy_mode_cli(config: dict, mode: str | None) -> None:
    """Apply --heavy-mode CLI flag to the loaded config (mutates in place).

    None = leave config alone. "off" sets enabled=False so the wrapper is a
    no-op. "auto"/"always" sets enabled=True with the matching trigger.
    """
    if mode is None:
        return
    hm = config.setdefault("heavy_mode", {})
    if mode == "off":
        hm["enabled"] = False
        hm["trigger"] = "off"
    else:
        hm["enabled"] = True
        hm["trigger"] = mode


if __name__ == "__main__":
    main()
