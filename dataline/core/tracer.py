"""Structured tracing with Langfuse integration.

Three-layer trace hierarchy:
  Session (eval run) → Task (single question) → Agent spans

Three-layer output detail:
  L0 — Task summary: pass/fail, bottleneck, decision path, harness flags
  L1 — Iteration summaries: plan → result → harness → judge verdict
  L2 — Full LLM I/O: raw prompts and responses for deep debugging

Langfuse integration (v4 SDK):
- Session = eval run (groups tasks)
- Trace = single task (one question → one prediction.csv)
- Observation = agent call (generation for LLM calls, span for non-LLM)
- Scores = task accuracy (posted after eval scoring)

Without Langfuse installed/configured, traces are saved as JSON only.

Usage in orchestrator:
    tracer = TaskTracer(task_id, output_dir, session_id="eval_20260423")
    with tracer.span("planner_coder", metadata={"iteration": 0}) as s:
        result = planner_coder.generate(...)
        s.set_llm_io(system, user, response)
    tracer.set_observations(obs)
    tracer.finish()
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Span:
    """A single traced operation (agent call, LLM call, etc.)."""
    name: str
    agent: str
    start_time: float
    end_time: float
    duration_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)
    llm_input: str = ""
    llm_output: str = ""
    llm_thinking: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


@dataclass
class SpanBuilder:
    """Mutable builder for constructing a Span during execution."""
    name: str
    agent: str
    start_time: float
    metadata: dict[str, Any] = field(default_factory=dict)
    llm_input: str = ""
    llm_output: str = ""
    llm_thinking: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""

    def set_llm_io(
        self,
        system_prompt: str,
        user_prompt: str,
        response: str,
        thinking: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record LLM input/output on this span."""
        max_prompt = 8000
        max_response = 4000
        self.llm_input = _truncate(f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}", max_prompt)
        self.llm_output = _truncate(response, max_response)
        self.llm_thinking = _truncate(thinking, 2000) if thinking else ""
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd

    def build(self) -> Span:
        """Finalize into an immutable Span."""
        end_time = time.time()
        return Span(
            name=self.name,
            agent=self.agent,
            start_time=self.start_time,
            end_time=end_time,
            duration_ms=int((end_time - self.start_time) * 1000),
            metadata=dict(self.metadata),
            llm_input=self.llm_input,
            llm_output=self.llm_output,
            llm_thinking=self.llm_thinking,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd,
            error=self.error,
        )


class TaskTracer:
    """Traces a single task execution with real-time progress updates.

    Hierarchy in Langfuse:
      Session (session_id) → Trace (task_id) → Observations (agent spans)
    """

    def __init__(self, task_id: str, output_dir: str = "", session_id: str = ""):
        self._task_id = task_id
        self._output_dir = output_dir
        self._session_id = session_id
        self._spans: list[Span] = []
        self._start_time = time.time()
        self._current_span: SpanBuilder | None = None
        self._observations: dict[str, Any] = {}

        # Langfuse (optional)
        self._langfuse, self._lf_trace, self._lf_trace_id = _try_init_langfuse(task_id, session_id)

        # Progress state
        self._progress = Progress(
            task_id=task_id,
            status="running",
            current_agent="",
            current_iteration=0,
            total_iterations=0,
            steps_completed=0,
            tokens_used=0,
            cost_usd=0.0,
            elapsed_seconds=0.0,
        )

    @contextmanager
    def span(
        self,
        agent: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Generator[SpanBuilder, None, None]:
        """Context manager that traces an agent operation."""
        builder = SpanBuilder(
            name=f"{self._task_id}/{agent}",
            agent=agent,
            start_time=time.time(),
            metadata=metadata or {},
        )
        self._current_span = builder

        # Update progress
        self._progress = Progress(
            task_id=self._task_id,
            status="running",
            current_agent=agent,
            current_iteration=self._progress.current_iteration,
            total_iterations=self._progress.total_iterations,
            steps_completed=self._progress.steps_completed,
            tokens_used=self._progress.tokens_used,
            cost_usd=self._progress.cost_usd,
            elapsed_seconds=time.time() - self._start_time,
            message=f"Running {agent}...",
        )
        self._write_progress()

        try:
            yield builder
        except Exception as exc:
            builder.error = str(exc)
            raise
        finally:
            span = builder.build()
            self._spans.append(span)
            self._current_span = None

            # Accumulate token usage
            self._progress = Progress(
                task_id=self._task_id,
                status="running",
                current_agent=agent,
                current_iteration=self._progress.current_iteration,
                total_iterations=self._progress.total_iterations,
                steps_completed=self._progress.steps_completed,
                tokens_used=self._progress.tokens_used + span.input_tokens + span.output_tokens,
                cost_usd=self._progress.cost_usd + span.cost_usd,
                elapsed_seconds=time.time() - self._start_time,
                message=f"{agent} done ({span.duration_ms}ms)",
            )
            self._write_progress()

            # Push to Langfuse if available
            if self._lf_trace:
                _push_span_to_langfuse(self._lf_trace, span)

    def set_iteration(self, iteration: int, total: int) -> None:
        """Update current iteration progress."""
        self._progress = Progress(
            task_id=self._task_id,
            status="running",
            current_agent=self._progress.current_agent,
            current_iteration=iteration,
            total_iterations=total,
            steps_completed=iteration,
            tokens_used=self._progress.tokens_used,
            cost_usd=self._progress.cost_usd,
            elapsed_seconds=time.time() - self._start_time,
            message=f"Iteration {iteration}/{total}",
        )
        self._write_progress()

    def set_observations(self, obs: dict[str, Any]) -> None:
        """Receive structured observations from orchestrator for L0/L1 output."""
        self._observations = obs

    def finish(self, success: bool = True, error: str = "") -> None:
        """Mark task as complete and write final trace."""
        self._progress = Progress(
            task_id=self._task_id,
            status="completed" if success else "failed",
            current_agent="",
            current_iteration=self._progress.current_iteration,
            total_iterations=self._progress.total_iterations,
            steps_completed=self._progress.steps_completed,
            tokens_used=self._progress.tokens_used,
            cost_usd=self._progress.cost_usd,
            elapsed_seconds=time.time() - self._start_time,
            message="Done" if success else f"Failed: {error}",
        )
        self._write_progress()
        self._write_trace(success, error)

        # Flush Langfuse
        if self._langfuse and self._lf_trace:
            try:
                summary = self._build_l0(success, error)
                self._lf_trace.update(
                    metadata=summary,
                    output={
                        "success": success,
                        "decision_path": summary.get("decision_path", ""),
                        "answer_columns": summary.get("answer_columns", []),
                        "harness_blocks": summary.get("harness_blocks", 0),
                    },
                    level="ERROR" if not success else "DEFAULT",
                    status_message=error if error else None,
                )
                self._lf_trace.end()
                self._langfuse.flush()
            except Exception as exc:
                logger.debug("Langfuse flush failed: %s", exc)

    def post_score(self, score: float, comment: str = "") -> None:
        """Post eval score to Langfuse after scoring.

        Called by eval/run_eval.py after score_task() returns.
        Links the numeric score to the task trace in Langfuse.
        """
        if not self._langfuse:
            return
        try:
            self._langfuse.create_score(
                trace_id=self._lf_trace_id,
                name="kdd_score",
                value=score,
                comment=comment or None,
            )
            self._langfuse.flush()
        except Exception as exc:
            logger.debug("Langfuse score post failed: %s", exc)

    def _build_l0(self, success: bool, error: str) -> dict[str, Any]:
        """Build L0 task summary from observations and progress."""
        obs = self._observations
        final = obs.get("final", {})
        iterations = obs.get("iterations", [])
        qa = obs.get("question_analyzer", {})

        # Decision path: "continue → harness_blocked → continue → finish"
        decision_path = " → ".join(
            i.get("judge_action", "?") for i in iterations
        ) if iterations else ""

        # Count harness gate blocks across iterations
        harness_blocks = sum(
            1 for i in iterations
            if "harness_blocked" in str(i.get("judge_action", ""))
        )
        harness_flags_total = sum(
            len(i.get("harness_flags", [])) for i in iterations
        )

        return {
            "task_id": self._task_id,
            "success": success,
            "error": error,
            "total_iterations": len(iterations),
            "total_tokens": self._progress.tokens_used,
            "total_cost_usd": round(self._progress.cost_usd, 4),
            "time_seconds": round(self._progress.elapsed_seconds, 1),
            "decision_path": decision_path,
            "backtracks_used": final.get("backtracks_used", 0),
            "answer_columns": final.get("answer_columns", []),
            "answer_rows": final.get("answer_rows", 0),
            "execution_path": final.get("execution_path", []),
            "languages_used": final.get("languages_used", []),
            # v5: HarnessGate tracking
            "harness_blocks": harness_blocks,
            "harness_flags_total": harness_flags_total,
            # v5: QuestionAnalyzer output
            "qa_answer_type": qa.get("answer_type", "unknown"),
            "qa_expected_columns": qa.get("expected_column_count", 0),
        }

    def _build_l1(self) -> list[dict[str, Any]]:
        """Build L1 iteration summaries from observations."""
        iterations = self._observations.get("iterations", [])
        summaries = []

        for it in iterations:
            iter_idx = it.get("iteration", 0)
            iter_spans = [
                s for s in self._spans
                if s.metadata.get("iteration") == iter_idx
            ]
            iter_tokens = sum(s.input_tokens + s.output_tokens for s in iter_spans)
            iter_cost = sum(s.cost_usd for s in iter_spans)
            iter_duration = sum(s.duration_ms for s in iter_spans)

            summaries.append({
                "iteration": iter_idx,
                "plan": it.get("plan_description", ""),
                "language": it.get("language", ""),
                "winning_language": it.get("winning_language", ""),
                "code_success": it.get("code_success", False),
                "result_preview": it.get("stdout_preview", ""),
                # v5: HarnessGate flags for this iteration
                "harness_flags": it.get("harness_flags", []),
                "judge": {
                    "action": it.get("judge_action", ""),
                    "reasoning": it.get("judge_reasoning", ""),
                    "guidance": it.get("judge_guidance", ""),
                },
                "tokens": iter_tokens,
                "cost_usd": round(iter_cost, 4),
                "duration_ms": iter_duration,
            })

        return summaries

    def _write_progress(self) -> None:
        """Write real-time progress to status.json in output dir."""
        if not self._output_dir:
            return
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            path = os.path.join(self._output_dir, "status.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._progress.to_dict(), f, indent=2)
        except OSError as exc:
            logger.debug("Could not write progress: %s", exc)

    def _write_trace(self, success: bool, error: str) -> None:
        """Write hierarchical trace: L0 summary + L1 iterations + L2 full spans."""
        if not self._output_dir:
            return
        try:
            data = {
                # L0: Task summary — scan in 5 seconds
                "summary": self._build_l0(success, error),

                # L1: Iteration summaries — understand the decision path
                "iterations": self._build_l1(),

                # L2: Full LLM I/O — deep debugging
                "spans": [_span_to_dict(s) for s in self._spans],
            }
            path = os.path.join(self._output_dir, "trace_agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.debug("Could not write trace: %s", exc)

    @property
    def spans(self) -> tuple[Span, ...]:
        return tuple(self._spans)

    @property
    def progress(self) -> "Progress":
        return self._progress


@dataclass(frozen=True)
class Progress:
    """Real-time task execution progress."""
    task_id: str
    status: str  # running | completed | failed
    current_agent: str
    current_iteration: int
    total_iterations: int
    steps_completed: int
    tokens_used: int
    cost_usd: float
    elapsed_seconds: float
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "current_agent": self.current_agent,
            "current_iteration": self.current_iteration,
            "total_iterations": self.total_iterations,
            "steps_completed": self.steps_completed,
            "tokens_used": self.tokens_used,
            "cost_usd": round(self.cost_usd, 4),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "message": self.message,
        }


# --- Langfuse integration (v4.0 SDK) ---
#
# Hierarchy:
#   Session (session_id) → Trace (task_id) → Observations (agent spans)
#
# v4 SDK uses:
#   - Langfuse() reads LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY from env
#   - start_observation(as_type="span"|"generation") for agent calls
#   - create_trace_id(seed=...) for deterministic trace IDs
#   - create_score() for eval metrics
#   - propagate_attributes(session_id=...) for session grouping


def _try_init_langfuse(
    task_id: str, session_id: str = ""
) -> tuple[Any | None, Any | None, str]:
    """Try to initialize Langfuse client + root observation.

    Returns (client, root_observation, trace_id). All None/"" on failure (fail-open).
    """
    try:
        from langfuse import Langfuse

        langfuse = Langfuse()

        # Deterministic trace_id: re-runs of the same task+session produce
        # the same trace, making it easy to compare runs in the UI.
        seed = f"{session_id}__{task_id}" if session_id else task_id
        trace_id = langfuse.create_trace_id(seed=seed)

        # Build trace context
        trace_ctx = {"trace_id": trace_id}

        # Start root observation for this task
        obs_metadata: dict[str, Any] = {
            "task_id": task_id,
            "framework": "dataline",
        }
        if session_id:
            obs_metadata["session_id"] = session_id

        root_obs = langfuse.start_observation(
            trace_context=trace_ctx,
            name=task_id,
            as_type="span",
            metadata=obs_metadata,
        )

        # Set session_id via propagate_attributes if available
        if session_id:
            try:
                from langfuse import propagate_attributes
                ctx = propagate_attributes(session_id=session_id)
                ctx.__enter__()
                # Store for cleanup — but we don't exit it until finish()
                # This is fine because propagate_attributes is thread-local
            except (ImportError, Exception):
                pass

        logger.info("Langfuse tracing: task=%s session=%s trace=%s",
                     task_id, session_id or "(none)", trace_id)

        return langfuse, root_obs, trace_id
    except ImportError:
        logger.debug("Langfuse not installed — traces saved to JSON only")
        return None, None, ""
    except Exception as exc:
        logger.debug("Langfuse init failed: %s — traces saved to JSON only", exc)
        return None, None, ""


def _push_span_to_langfuse(root_obs: Any, span: Span) -> None:
    """Push a completed span as a child observation of the root task span.

    LLM calls → type="generation" (shows token usage in Langfuse UI)
    Non-LLM calls → type="span" (shows timing only)
    """
    try:
        if span.llm_input:
            # Generation: LLM call with full I/O
            obs = root_obs.start_observation(
                name=span.agent,
                as_type="generation",
                input=span.llm_input,
                output=span.llm_output,
                metadata=span.metadata,
                usage_details={
                    "input": span.input_tokens,
                    "output": span.output_tokens,
                    "total": span.input_tokens + span.output_tokens,
                },
                cost_details={"total": span.cost_usd} if span.cost_usd else None,
                level="ERROR" if span.error else "DEFAULT",
                status_message=span.error if span.error else None,
            )
        else:
            # Span: non-LLM operation (sandbox, harness_gate, etc.)
            obs = root_obs.start_observation(
                name=span.agent,
                as_type="span",
                metadata=span.metadata,
                level="ERROR" if span.error else "DEFAULT",
                status_message=span.error if span.error else None,
            )
        obs.end(end_time=int(span.end_time * 1000))
    except Exception as exc:
        logger.debug("Failed to push span to Langfuse: %s", exc)


# --- Helpers ---


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... [truncated, {len(text)} total chars]"


def _span_to_dict(span: Span) -> dict[str, Any]:
    """Convert Span to JSON-serializable dict for trace_agent.json."""
    d: dict[str, Any] = {
        "name": span.name,
        "agent": span.agent,
        "duration_ms": span.duration_ms,
        "metadata": span.metadata,
    }
    if span.input_tokens > 0:
        d["input_tokens"] = span.input_tokens
        d["output_tokens"] = span.output_tokens
        d["cost_usd"] = round(span.cost_usd, 6)
    if span.llm_input:
        d["llm_input"] = span.llm_input
    if span.llm_output:
        d["llm_output"] = span.llm_output
    if span.llm_thinking:
        d["llm_thinking"] = span.llm_thinking
    if span.error:
        d["error"] = span.error
    return d
