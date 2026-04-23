"""Fetch failed/low-scoring task traces from Langfuse Cloud for local analysis.

Usage:
    # Fetch all tasks with score < 0.5 from the latest eval session
    python -m dataline.eval.fetch_failures --session eval_v5_20260423

    # Fetch a specific task's full trace
    python -m dataline.eval.fetch_failures --task task_11

    # Fetch top 10 worst scores from any session
    python -m dataline.eval.fetch_failures --worst 10

    # List recent sessions
    python -m dataline.eval.fetch_failures --list-sessions
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=True)


def _get_langfuse():
    from langfuse import Langfuse
    lf = Langfuse()
    lf.auth_check()
    return lf


def list_sessions(limit: int = 10) -> None:
    """List recent sessions."""
    lf = _get_langfuse()
    sessions = lf.api.sessions.list(page=1, limit=limit)
    print(f"\n{'Session ID':<40} {'Created':<25} {'Traces'}")
    print("-" * 80)
    for s in sessions.data:
        print(f"{s.id:<40} {str(s.created_at)[:19]:<25} {getattr(s, 'count_traces', '?')}")


def fetch_session_failures(session_id: str, threshold: float = 0.5) -> list[dict]:
    """Fetch all low-scoring traces from a session.

    Returns list of dicts with task_id, score, decision_path, and failure details.
    """
    lf = _get_langfuse()

    # Get all traces in this session
    traces = lf.api.trace.list(session_id=session_id, limit=100)

    # Get scores for these traces
    scores = lf.api.scores.get_many(limit=200)

    # Build score lookup by trace_id
    score_map: dict[str, float] = {}
    for s in scores.data:
        if s.name == "kdd_score" and s.trace_id:
            score_map[s.trace_id] = s.value

    results: list[dict] = []
    for trace in traces.data:
        score = score_map.get(trace.id, -1.0)
        if score >= threshold and score >= 0:
            continue

        # This is a failure — fetch details
        metadata = trace.metadata or {}
        results.append({
            "task_id": trace.name or metadata.get("task_id", "?"),
            "trace_id": trace.id,
            "score": score,
            "success": metadata.get("success", True),
            "error": metadata.get("error", ""),
            "total_iterations": metadata.get("total_iterations", 0),
            "decision_path": metadata.get("decision_path", ""),
            "harness_blocks": metadata.get("harness_blocks", 0),
            "harness_flags_total": metadata.get("harness_flags_total", 0),
            "qa_answer_type": metadata.get("qa_answer_type", ""),
            "answer_columns": metadata.get("answer_columns", []),
            "execution_path": metadata.get("execution_path", []),
            "time_seconds": metadata.get("time_seconds", 0),
        })

    results.sort(key=lambda r: r["score"])
    return results


def fetch_task_trace(task_id: str, session_id: str = "") -> dict[str, Any]:
    """Fetch full trace for a specific task.

    Returns dict with summary, all observations (LLM I/O), and scores.
    """
    lf = _get_langfuse()

    # Find the trace
    kwargs: dict[str, Any] = {"limit": 50}
    if session_id:
        kwargs["session_id"] = session_id

    traces = lf.api.trace.list(**kwargs)
    target = None
    for t in traces.data:
        name = t.name or (t.metadata or {}).get("task_id", "")
        if name == task_id:
            target = t
            break

    if not target:
        print(f"Trace not found for task_id={task_id}")
        return {}

    # Fetch observations (agent spans)
    observations = lf.api.observations.get_many(trace_id=target.id, limit=50)

    spans: list[dict] = []
    for obs in observations.data:
        span: dict[str, Any] = {
            "agent": obs.name,
            "type": obs.type,
            "duration_ms": (
                int((obs.end_time - obs.start_time).total_seconds() * 1000)
                if obs.end_time and obs.start_time else 0
            ),
            "metadata": obs.metadata or {},
        }

        if obs.type == "GENERATION":
            span["input"] = _truncate_str(obs.input, 2000) if obs.input else ""
            span["output"] = _truncate_str(obs.output, 2000) if obs.output else ""
            usage = obs.usage_details or {}
            span["input_tokens"] = usage.get("input", 0)
            span["output_tokens"] = usage.get("output", 0)

        if obs.level == "ERROR":
            span["error"] = obs.status_message or "unknown error"

        spans.append(span)

    return {
        "task_id": task_id,
        "trace_id": target.id,
        "metadata": target.metadata or {},
        "observations": spans,
    }


def fetch_worst(n: int = 10) -> list[dict]:
    """Fetch the N worst-scoring tasks across all sessions."""
    lf = _get_langfuse()
    scores = lf.api.scores.get_many(name="kdd_score", limit=200)

    entries: list[dict] = []
    for s in scores.data:
        entries.append({
            "trace_id": s.trace_id,
            "score": s.value,
            "comment": s.comment or "",
        })

    entries.sort(key=lambda e: e["score"])
    worst = entries[:n]

    # Enrich with trace metadata
    for entry in worst:
        try:
            trace = lf.api.trace.get(entry["trace_id"])
            metadata = trace.metadata or {}
            entry["task_id"] = trace.name or metadata.get("task_id", "?")
            entry["decision_path"] = metadata.get("decision_path", "")
            entry["harness_blocks"] = metadata.get("harness_blocks", 0)
            entry["error"] = metadata.get("error", "")
        except Exception:
            entry["task_id"] = "?"

    return worst


def print_failures(failures: list[dict]) -> None:
    """Pretty-print failure summary."""
    if not failures:
        print("\nNo failures found.")
        return

    print(f"\n{'Task':<15} {'Score':<8} {'Iters':<6} {'HG Blocks':<10} {'Path'}")
    print("-" * 90)
    for f in failures:
        path = " → ".join(f.get("execution_path", [])) or f.get("decision_path", "")
        print(
            f"{f['task_id']:<15} "
            f"{f['score']:<8.2f} "
            f"{f.get('total_iterations', '?'):<6} "
            f"{f.get('harness_blocks', 0):<10} "
            f"{path[:45]}"
        )

    print(f"\nTotal failures: {len(failures)}")

    # Aggregate stats
    harness_blocks = sum(f.get("harness_blocks", 0) for f in failures)
    if harness_blocks:
        print(f"Total harness blocks across failures: {harness_blocks}")


def print_trace(trace: dict) -> None:
    """Pretty-print a single task trace."""
    if not trace:
        return

    print(f"\n=== {trace['task_id']} (trace: {trace['trace_id']}) ===\n")

    meta = trace.get("metadata", {})
    if meta:
        print(f"Score: {meta.get('score', '?')}")
        print(f"Iterations: {meta.get('total_iterations', '?')}")
        print(f"Decision path: {meta.get('decision_path', '?')}")
        print(f"Answer columns: {meta.get('answer_columns', [])}")
        print(f"Harness blocks: {meta.get('harness_blocks', 0)}")
        print()

    for obs in trace.get("observations", []):
        agent = obs.get("agent", "?")
        dur = obs.get("duration_ms", 0)
        obs_type = obs.get("type", "span")
        tokens = ""
        if obs.get("input_tokens"):
            tokens = f" [{obs['input_tokens']}+{obs['output_tokens']} tokens]"

        error_mark = " ERROR" if obs.get("error") else ""
        print(f"  [{obs_type:<12}] {agent:<20} {dur:>6}ms{tokens}{error_mark}")

        if obs.get("error"):
            print(f"               Error: {obs['error'][:200]}")

        # Show LLM output preview for generations
        if obs_type == "GENERATION" and obs.get("output"):
            preview = obs["output"][:300].replace("\n", " ")
            print(f"               Output: {preview}...")

        # Show harness flags from metadata
        flags = obs.get("metadata", {}).get("harness_flags", [])
        if flags:
            for flag in flags:
                print(f"               Flag: [{flag.get('severity', '?')}] {flag.get('rule', '?')}: {flag.get('message', '')[:100]}")


def _truncate_str(val: Any, max_len: int) -> str:
    s = str(val) if val else ""
    return s[:max_len] if len(s) > max_len else s


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch failure traces from Langfuse")
    parser.add_argument("--session", help="Session ID to analyze")
    parser.add_argument("--task", help="Specific task_id to fetch full trace")
    parser.add_argument("--worst", type=int, help="Fetch N worst-scoring tasks")
    parser.add_argument("--threshold", type=float, default=0.5, help="Score threshold for failures (default: 0.5)")
    parser.add_argument("--list-sessions", action="store_true", help="List recent sessions")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.list_sessions:
        list_sessions()
    elif args.task:
        trace = fetch_task_trace(args.task, session_id=args.session or "")
        if args.json:
            print(json.dumps(trace, indent=2, default=str))
        else:
            print_trace(trace)
    elif args.worst:
        worst = fetch_worst(args.worst)
        if args.json:
            print(json.dumps(worst, indent=2, default=str))
        else:
            print_failures(worst)
    elif args.session:
        failures = fetch_session_failures(args.session, threshold=args.threshold)
        if args.json:
            print(json.dumps(failures, indent=2, default=str))
        else:
            print_failures(failures)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
