"""Categorize task failures for actionable diagnostics."""

from __future__ import annotations

from ..core.types import TaskScore


# Failure categories
CATEGORIES = {
    "code_error": "Generated code crashed and couldn't be fixed",
    "wrong_direction": "Agent pursued wrong analysis strategy",
    "format_error": "Answer correct but formatting didn't match gold",
    "partial_result": "Some sub-answers correct but not all",
    "hallucination": "Agent fabricated data not in any source",
    "timeout": "Exceeded step or time limit",
    "empty_answer": "Agent produced no answer",
    "profiler_miss": "Profiler failed to detect a data source or column",
}


def categorize_failure(task_score: TaskScore, trace: list[dict]) -> str:
    """Categorize a failed task based on its trace."""
    if not trace:
        return "empty_answer"

    # Check for timeout
    sandbox_steps = [t for t in trace if t.get("agent") == "sandbox"]
    if any("Timeout" in t.get("message", "") for t in trace):
        return "timeout"

    # Check for empty answer
    finalizer_entries = [t for t in trace if t.get("agent") == "finalizer"]
    if not finalizer_entries:
        return "empty_answer"

    # Check for code errors (debugger was heavily used)
    debugger_entries = [t for t in trace if t.get("agent") == "debugger"]
    error_count = len(debugger_entries)

    # Check judge — did it finish too early?
    judge_entries = [t for t in trace if t.get("agent") == "judge"]

    # Heuristic categorization
    if error_count >= 3:
        return "code_error"

    # Check if planner went wrong direction
    planner_entries = [t for t in trace if t.get("agent") == "planner"]
    if len(planner_entries) >= 8:
        return "wrong_direction"  # Too many steps usually means lost

    # If we got an answer but it's wrong, likely format or partial
    if task_score.error_detail:
        if "column" in task_score.error_detail.lower():
            return "format_error"

    return "partial_result"


def identify_failed_agent(trace: list[dict]) -> str:
    """Identify which agent was the primary failure point."""
    # Find the last error in trace
    for entry in reversed(trace):
        msg = entry.get("message", "")
        agent = entry.get("agent", "")
        if "fail" in msg.lower() or "error" in msg.lower() or "rc=" in msg and "rc=0" not in msg:
            return agent
    return "unknown"
