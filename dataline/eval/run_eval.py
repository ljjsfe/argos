"""Batch evaluation with full diagnostic output."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

import pandas as pd

from ..core.types import EvalReport, TaskScore
from .scorer import score_task
from .failure_analysis import categorize_failure, identify_failed_agent


def run_eval(
    results_dir: str,
    gold_dir: str,
    task_ids: list[str] | None = None,
) -> EvalReport:
    """Evaluate all tasks and produce a diagnostic report.

    Args:
        results_dir: Directory with task_id/prediction.csv and task_id/trace.json
        gold_dir: Directory with input/task_id/task.json and output/task_id/gold.csv
    """
    if task_ids is None:
        # Discover tasks from gold dir
        output_dir = os.path.join(gold_dir, "output")
        if os.path.exists(output_dir):
            task_ids = sorted([
                d for d in os.listdir(output_dir)
                if os.path.isdir(os.path.join(output_dir, d))
            ])
        else:
            task_ids = []

    scores: list[TaskScore] = []
    failure_counts: Counter = Counter()
    agent_failures: Counter = Counter()
    total_tokens = 0
    total_cost = 0.0

    for task_id in task_ids:
        # Load gold
        gold_path = os.path.join(gold_dir, "output", task_id, "gold.csv")
        if not os.path.exists(gold_path):
            continue
        gold_df = pd.read_csv(gold_path)

        # Load task metadata
        task_json_path = os.path.join(gold_dir, "input", task_id, "task.json")
        difficulty = ""
        if os.path.exists(task_json_path):
            with open(task_json_path) as f:
                task_meta = json.load(f)
                difficulty = task_meta.get("difficulty", "")

        # Load prediction
        pred_path = os.path.join(results_dir, task_id, "prediction.csv")
        trace_path = _find_trace(results_dir, task_id)

        if not os.path.exists(pred_path):
            ts = TaskScore(
                task_id=task_id, score=0, difficulty=difficulty,
                failure_category="empty_answer",
                failed_at_agent="orchestrator",
                error_detail="No prediction.csv found",
                suggestion="Task was not executed or crashed before producing output",
            )
            scores.append(ts)
            failure_counts["empty_answer"] += 1
            agent_failures["orchestrator"] += 1
            continue

        # Handle empty files (finalizer failed to format)
        try:
            pred_df = pd.read_csv(pred_path)
        except pd.errors.EmptyDataError:
            ts = TaskScore(
                task_id=task_id, score=0, difficulty=difficulty,
                failure_category="format_error",
                failed_at_agent="finalizer",
                error_detail="prediction.csv is empty (finalizer failed)",
                suggestion="Finalizer produced empty output — check formatting logic",
            )
            scores.append(ts)
            failure_counts["format_error"] += 1
            agent_failures["finalizer"] += 1
            continue

        # Score
        task_score_val = score_task(pred_df, gold_df)

        # Load trace for diagnostics
        trace = []
        tokens = 0
        cost = 0.0
        time_s = 0.0
        steps = 0
        if os.path.exists(trace_path):
            with open(trace_path) as f:
                trace_data = json.load(f)
                trace = trace_data.get("trace", [])
                tokens = trace_data.get("total_tokens", 0)
                cost = trace_data.get("total_cost_usd", 0.0)
                time_s = trace_data.get("time_seconds", 0.0)
                steps = trace_data.get("steps_executed", 0)

        total_tokens += tokens
        total_cost += cost

        ts = TaskScore(
            task_id=task_id,
            score=task_score_val,
            difficulty=difficulty,
            tokens_used=tokens,
            cost_usd=cost,
            time_seconds=time_s,
            steps_executed=steps,
        )

        if task_score_val < 1.0:
            category = categorize_failure(ts, trace)
            failed_agent = identify_failed_agent(trace)
            ts = TaskScore(
                task_id=task_id, score=task_score_val, difficulty=difficulty,
                failure_category=category,
                failed_at_agent=failed_agent,
                tokens_used=tokens,
                cost_usd=cost,
                time_seconds=time_s,
                steps_executed=steps,
            )
            failure_counts[category] += 1
            agent_failures[failed_agent] += 1

        scores.append(ts)

    # Aggregate — mean score across all tasks (float, 0.0–1.0)
    total = len(scores)
    overall = sum(s.score for s in scores) / total if total > 0 else 0.0

    # Per-difficulty
    per_diff: dict[str, float] = {}
    for diff in ["easy", "medium", "hard", "extreme"]:
        diff_tasks = [s for s in scores if s.difficulty == diff]
        if diff_tasks:
            per_diff[diff] = sum(s.score for s in diff_tasks) / len(diff_tasks)

    # Generate suggestions
    suggestions = _generate_suggestions(failure_counts, agent_failures, scores)

    return EvalReport(
        overall_accuracy=overall,
        per_difficulty=per_diff,
        task_scores=tuple(scores),
        failure_breakdown=dict(failure_counts),
        agent_bottlenecks=dict(agent_failures),
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        suggestions=tuple(suggestions),
    )


def _generate_suggestions(
    failures: Counter,
    agent_fails: Counter,
    scores: list[TaskScore],
) -> list[str]:
    """Generate actionable improvement suggestions from failure patterns."""
    suggestions = []
    total_failed = sum(failures.values())
    if total_failed == 0:
        return ["All tasks passed! Consider running on harder benchmarks."]

    # Sort failures by count
    for category, count in failures.most_common(3):
        pct = count / total_failed * 100
        if category == "code_error":
            suggestions.append(
                f"[HIGH] Fix code generation ({count} tasks, {pct:.0f}% of failures). "
                f"Improve coder prompt with more schema context and error examples."
            )
        elif category == "wrong_direction":
            suggestions.append(
                f"[HIGH] Improve planning strategy ({count} tasks, {pct:.0f}%). "
                f"Planner needs better manifest utilization and question decomposition."
            )
        elif category == "format_error":
            suggestions.append(
                f"[MED] Fix output formatting ({count} tasks, {pct:.0f}%). "
                f"Quick win: improve normalizer and finalizer column naming."
            )
        elif category == "partial_result":
            suggestions.append(
                f"[MED] Address incomplete answers ({count} tasks, {pct:.0f}%). "
                f"Judge may be choosing 'finish' too early."
            )
        elif category == "timeout":
            suggestions.append(
                f"[LOW] Reduce timeouts ({count} tasks, {pct:.0f}%). "
                f"Consider increasing sandbox timeout or limiting data size."
            )
        elif category == "empty_answer":
            suggestions.append(
                f"[HIGH] Fix empty outputs ({count} tasks, {pct:.0f}%). "
                f"Tasks not producing any prediction — check orchestrator flow."
            )

    # Agent bottleneck suggestion
    if agent_fails:
        worst_agent, worst_count = agent_fails.most_common(1)[0]
        suggestions.append(
            f"[INFO] Most failures occur at: {worst_agent} ({worst_count} tasks). "
            f"Prioritize improving this agent's prompt and logic."
        )

    return suggestions


def _find_trace(results_dir: str, task_id: str) -> str:
    """Find trace file, supporting both new and legacy filenames."""
    path = os.path.join(results_dir, task_id, "trace.json")
    if os.path.exists(path):
        return path
    legacy = os.path.join(results_dir, task_id, "trace_detailed.json")
    if os.path.exists(legacy):
        return legacy
    return path  # return default even if missing


def format_report(report: EvalReport) -> str:
    """Format EvalReport as human-readable text dashboard."""
    total = len(report.task_scores)
    correct = sum(1 for s in report.task_scores if s.score == 1)

    lines = [
        f"=== EVAL REPORT: {total} tasks ===",
        f"",
        f"Overall: {correct}/{total} ({report.overall_accuracy:.1%})",
    ]

    # Per-difficulty
    for diff in ["easy", "medium", "hard", "extreme"]:
        if diff in report.per_difficulty:
            diff_tasks = [s for s in report.task_scores if s.difficulty == diff]
            diff_correct = sum(1 for s in diff_tasks if s.score == 1)
            lines.append(f"  {diff:>8}: {diff_correct}/{len(diff_tasks)} ({report.per_difficulty[diff]:.1%})")

    # Failure breakdown
    if report.failure_breakdown:
        lines.append(f"\n=== FAILURE BREAKDOWN ===")
        for cat, count in sorted(report.failure_breakdown.items(), key=lambda x: -x[1]):
            total_failed = sum(report.failure_breakdown.values())
            pct = count / total_failed * 100 if total_failed > 0 else 0
            lines.append(f"  {cat:>20}: {count} tasks ({pct:.0f}%)")

    # Agent bottlenecks
    if report.agent_bottlenecks:
        lines.append(f"\n=== BOTTLENECK BY AGENT ===")
        for agent, count in sorted(report.agent_bottlenecks.items(), key=lambda x: -x[1]):
            lines.append(f"  {agent:>20}: {count} tasks")

    # Economics
    lines.append(f"\n=== TOKEN ECONOMICS ===")
    lines.append(f"  Total: {report.total_tokens:,} tokens / ${report.total_cost_usd:.2f}")
    if total > 0:
        lines.append(f"  Avg per task: {report.total_tokens // max(total, 1):,} tokens / ${report.total_cost_usd / total:.3f}")

    # Most expensive task
    if report.task_scores:
        most_expensive = max(report.task_scores, key=lambda s: s.tokens_used)
        if most_expensive.tokens_used > 0:
            lines.append(f"  Most expensive: {most_expensive.task_id} ({most_expensive.tokens_used:,} tokens, {most_expensive.steps_executed} steps)")

    # Suggestions
    if report.suggestions:
        lines.append(f"\n=== TOP ACTIONABLE IMPROVEMENTS ===")
        for i, suggestion in enumerate(report.suggestions, 1):
            lines.append(f"  {i}. {suggestion}")

    # Per-difficulty failure patterns
    lines.append(f"\n=== PER-DIFFICULTY FAILURE PATTERNS ===")
    for diff in ["easy", "medium", "hard", "extreme"]:
        failed = [s for s in report.task_scores if s.difficulty == diff and s.score == 0]
        if failed:
            cats = Counter(s.failure_category for s in failed)
            top_cat = cats.most_common(1)[0] if cats else ("none", 0)
            lines.append(f"  {diff:>8}: mostly {top_cat[0]} ({top_cat[1]} tasks)")

    return "\n".join(lines)
