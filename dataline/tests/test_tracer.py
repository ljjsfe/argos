"""Tests for TaskTracer and real-time progress monitoring (Phase 5)."""

import json
import os
import tempfile
import time

import pytest

from dataline.core.tracer import Progress, Span, SpanBuilder, TaskTracer


class TestSpanBuilder:
    def test_builds_immutable_span(self):
        builder = SpanBuilder(name="t/planner", agent="planner", start_time=time.time())
        time.sleep(0.01)  # ensure measurable duration
        span = builder.build()
        assert span.name == "t/planner"
        assert span.agent == "planner"
        assert span.duration_ms >= 1
        assert span.error == ""

    def test_set_llm_io(self):
        builder = SpanBuilder(name="t/coder", agent="coder", start_time=time.time())
        builder.set_llm_io(
            system_prompt="You are a coder",
            user_prompt="Write code to load CSV",
            response="```python\nimport pandas as pd\n```",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.005,
        )
        span = builder.build()
        assert "[SYSTEM]" in span.llm_input
        assert "You are a coder" in span.llm_input
        assert "[USER]" in span.llm_input
        assert "pandas" in span.llm_output
        assert span.input_tokens == 100
        assert span.output_tokens == 50
        assert span.cost_usd == pytest.approx(0.005)

    def test_truncates_long_input(self):
        builder = SpanBuilder(name="t/test", agent="test", start_time=time.time())
        long_prompt = "x" * 20000
        builder.set_llm_io(
            system_prompt=long_prompt,
            user_prompt="short",
            response="ok",
        )
        span = builder.build()
        assert len(span.llm_input) < 20000
        assert "truncated" in span.llm_input

    def test_error_captured(self):
        builder = SpanBuilder(name="t/test", agent="test", start_time=time.time())
        builder.error = "Something went wrong"
        span = builder.build()
        assert span.error == "Something went wrong"


class TestTaskTracer:
    def test_span_context_manager(self):
        tracer = TaskTracer("t1")
        with tracer.span("planner") as s:
            s.metadata["iteration"] = 0
        assert len(tracer.spans) == 1
        assert tracer.spans[0].agent == "planner"
        assert tracer.spans[0].metadata["iteration"] == 0

    def test_multiple_spans(self):
        tracer = TaskTracer("t1")
        with tracer.span("profiler"):
            pass
        with tracer.span("analyzer"):
            pass
        with tracer.span("planner"):
            pass
        assert len(tracer.spans) == 3
        agents = [s.agent for s in tracer.spans]
        assert agents == ["profiler", "analyzer", "planner"]

    def test_progress_updates(self):
        tracer = TaskTracer("t1")
        assert tracer.progress.status == "running"
        assert tracer.progress.task_id == "t1"

        tracer.set_iteration(2, 13)
        assert tracer.progress.current_iteration == 2
        assert tracer.progress.total_iterations == 13

        tracer.finish(success=True)
        assert tracer.progress.status == "completed"

    def test_progress_on_failure(self):
        tracer = TaskTracer("t1")
        tracer.finish(success=False, error="API timeout")
        assert tracer.progress.status == "failed"
        assert "API timeout" in tracer.progress.message

    def test_writes_status_json(self):
        with tempfile.TemporaryDirectory() as td:
            tracer = TaskTracer("t1", output_dir=td)
            with tracer.span("planner"):
                pass
            tracer.finish()

            status_path = os.path.join(td, "status.json")
            assert os.path.exists(status_path)
            with open(status_path) as f:
                data = json.load(f)
            assert data["task_id"] == "t1"
            assert data["status"] == "completed"

    def test_writes_trace_json_with_layers(self):
        with tempfile.TemporaryDirectory() as td:
            tracer = TaskTracer("t1", output_dir=td)
            with tracer.span("planner", metadata={"iteration": 0}) as s:
                s.set_llm_io("sys", "user", "resp", input_tokens=10, output_tokens=5, cost_usd=0.001)

            # Simulate orchestrator passing observations
            tracer.set_observations({
                "iterations": [{
                    "iteration": 0,
                    "plan_description": "Load CSV",
                    "plan_sources": ["data.csv"],
                    "code_success": True,
                    "debug_retries": 0,
                    "stdout_preview": "DataFrame: 100 rows",
                    "judge_action": "finish",
                    "judge_reasoning": "Data loaded",
                    "judge_missing": "",
                    "judge_guidance": "",
                }],
                "final": {
                    "code_failures": 0,
                    "backtracks_used": 0,
                    "total_debug_retries": 0,
                    "stagnation_stops": False,
                    "answer_columns": ["col1"],
                    "answer_rows": 10,
                },
            })
            tracer.finish()

            trace_path = os.path.join(td, "trace_agent.json")
            assert os.path.exists(trace_path)
            with open(trace_path) as f:
                data = json.load(f)

            # L0: summary
            assert data["summary"]["task_id"] == "t1"
            assert data["summary"]["total_tokens"] == 15
            assert data["summary"]["decision_path"] == "finish"
            assert data["summary"]["answer_columns"] == ["col1"]

            # L1: iteration summaries
            assert len(data["iterations"]) == 1
            assert data["iterations"][0]["plan"] == "Load CSV"
            assert data["iterations"][0]["judge"]["action"] == "finish"

            # L2: full spans
            assert len(data["spans"]) == 1
            assert data["spans"][0]["agent"] == "planner"
            assert "llm_input" in data["spans"][0]

    def test_span_exception_captured(self):
        tracer = TaskTracer("t1")
        with pytest.raises(ValueError, match="test error"):
            with tracer.span("coder"):
                raise ValueError("test error")
        assert len(tracer.spans) == 1
        assert tracer.spans[0].error == "test error"

    def test_tokens_accumulate_across_spans(self):
        with tempfile.TemporaryDirectory() as td:
            tracer = TaskTracer("t1", output_dir=td)
            with tracer.span("planner") as s:
                s.set_llm_io("s", "u", "r", input_tokens=100, output_tokens=50, cost_usd=0.01)
            with tracer.span("coder") as s:
                s.set_llm_io("s", "u", "r", input_tokens=200, output_tokens=100, cost_usd=0.02)
            tracer.finish()

            assert tracer.progress.tokens_used == 450
            assert tracer.progress.cost_usd == pytest.approx(0.03)


class TestProgress:
    def test_to_dict(self):
        p = Progress(
            task_id="t1",
            status="running",
            current_agent="planner",
            current_iteration=2,
            total_iterations=13,
            steps_completed=2,
            tokens_used=5000,
            cost_usd=0.123456,
            elapsed_seconds=45.67,
            message="Running planner...",
        )
        d = p.to_dict()
        assert d["task_id"] == "t1"
        assert d["cost_usd"] == 0.1235  # rounded to 4dp
        assert d["elapsed_seconds"] == 45.7  # rounded to 1dp
        assert d["message"] == "Running planner..."
