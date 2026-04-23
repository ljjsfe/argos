"""Orchestrator: unified loop coordinating all agents.

Pipeline (v5):
  Profiler → Analyzer → QuestionAnalyzer → Loop(PlannerCoder → Sandbox → HarnessGate → Judge) → Finalizer

Changes from v4:
- QuestionAnalyzer runs once before loop, produces QuestionSpec
- HarnessGate sits between Sandbox and Judge (zero LLM cost)
  - block flags → skip Judge, use message as guidance
  - warn flags → passed to Judge as reference
- Skeptic removed (experimentally proven 0% effective)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.context_manager import ContextManager
from ..core.llm_client import LLMClient
from ..core.sandbox import Sandbox
from ..core.state import (
    add_step,
    create_initial_state,
    set_question_analysis,
    summarize_step_output,
    truncate_to_step,
    update_judge_guidance,
)
from ..core.tracer import TaskTracer
from ..core.tracing_llm import TracingLLMClient
from ..core.types import (
    AnalysisState,
    JudgeDecision,
    Manifest,
    PlanStep,
    SandboxResult,
    StepRecord,
)
from ..core.workspace import Workspace
from ..profiler import manifest as profiler
from ..profiler.manifest import manifest_to_json
from . import analyzer, judge, debugger, finalizer, harness_gate, question_analyzer
from .planner_coder import generate as planner_coder_generate, PlannerCoderOutput
from .code_validator import validate_column_references

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """Complete result from running a task."""
    task_id: str
    question: str
    answer: dict[str, Any]         # {"col_name": [values]}
    steps: list[StepRecord]
    trace: list[dict]              # Full trace for diagnostics
    observations: dict[str, Any]   # Structured observation points
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    time_seconds: float = 0.0
    success: bool = True
    error: str = ""
    benchmark: str = "kdd"


def run_task(
    task_dir: str,
    question: str,
    llm: LLMClient,
    config: dict,
    task_id: str = "",
    output_dir: str = "",
    benchmark: str = "kdd",
    guidelines: str = "",
    session_id: str = "",
) -> TaskResult:
    """Run the full agent pipeline on a single task."""
    start_time = time.time()
    trace: list[dict] = []
    max_iterations = config.get("agent", {}).get("max_iterations", 8)
    min_iterations = config.get("agent", {}).get("min_iterations", 1)
    max_retries = config.get("agent", {}).get("max_retries", 2)
    backtrack_limit = config.get("agent", {}).get("backtrack_limit", 3)
    stagnation_threshold = config.get("agent", {}).get("stagnation_threshold", 2)

    # Initialize tracer for real-time progress + LLM I/O logging
    tracer = TaskTracer(task_id, output_dir, session_id=session_id)
    traced_llm = TracingLLMClient(llm, tracer)

    sandbox = Sandbox(
        task_dir=task_dir,
        timeout=config.get("sandbox", {}).get("timeout_seconds", 120),
        max_memory_mb=config.get("sandbox", {}).get("max_memory_mb", 1024),
    )

    # Workspace: file-based state, persisted to output_dir/workspace/
    workspace = Workspace(temp_dir=sandbox.temp_dir, output_dir=output_dir)

    # Context manager: enforces token budget across all agent calls
    context_window = config.get("llm", {}).get("context_window", 262_144)
    cm = ContextManager(token_limit=context_window)

    obs: dict[str, Any] = {
        "profiler": {},
        "analyzer": {},
        "question_analyzer": {},
        "iterations": [],
        "final": {},
    }

    try:
        # ─── Stage 1: Profile (deterministic, zero LLM cost) ───
        with tracer.span("profiler"):
            _log(trace, "profiler", "Scanning task directory")
            manifest = profiler.scan(task_dir)
            manifest_json = manifest_to_json(manifest)
            _log(trace, "profiler", f"Found {len(manifest.entries)} files, "
                 f"{len(manifest.cross_source_relations)} relations")

        obs["profiler"] = {
            "files_found": len(manifest.entries),
            "file_types": sorted({e.file_type for e in manifest.entries}),
            "cross_source_relations": len(manifest.cross_source_relations),
            "total_size_bytes": sum(e.size_bytes for e in manifest.entries),
        }

        # ─── Stage 2: Analyze (deep profiling + domain rule extraction) ───
        with tracer.span("analyzer"):
            _log(trace, "analyzer", "Running deep data analysis")
            data_profile, domain_rules_raw = analyzer.analyze(
                manifest, traced_llm, sandbox,
            )
            _log(trace, "analyzer", f"Profile: {len(data_profile)} chars, "
                 f"domain rules: {len(domain_rules_raw)} chars")

        # Compile domain rules if they exceed budget fraction
        with tracer.span("domain_compiler"):
            domain_rules = analyzer.compile_domain_rules(
                domain_rules_raw, traced_llm, cm.budget_tokens,
            )
            if len(domain_rules) < len(domain_rules_raw):
                _log(trace, "domain_compiler",
                     f"Compiled: {len(domain_rules_raw)} → {len(domain_rules)} chars")

        workspace.write_domain_rules(domain_rules)
        workspace.write_data_profile(data_profile)

        obs["analyzer"] = {
            "profile_length_chars": len(data_profile),
            "domain_rules_length_chars": len(domain_rules),
            "analysis_success": len(data_profile) > 50,
        }

        # ─── Stage 2b: QuestionAnalyzer (1 LLM call, infer answer shape) ───
        with tracer.span("question_analyzer"):
            _log(trace, "question_analyzer", "Inferring answer shape")
            question_spec = question_analyzer.analyze(
                question, manifest_json, domain_rules, traced_llm,
            )
            qa_guidance = question_spec.to_guidance()
            _log(trace, "question_analyzer",
                 f"type={question_spec.answer_type}, "
                 f"cols={question_spec.expected_column_count}, "
                 f"rows={question_spec.expected_row_count}, "
                 f"style={question_spec.value_style}")

        obs["question_analyzer"] = {
            "answer_type": question_spec.answer_type,
            "expected_column_count": question_spec.expected_column_count,
            "expected_row_count": question_spec.expected_row_count,
            "value_style": question_spec.value_style,
        }

        # ─── Stage 3: Initialize state ───
        state = create_initial_state(
            task_id, question, manifest, data_profile, domain_rules,
        )

        # Inject QA analysis into state for downstream agents
        if qa_guidance:
            state = set_question_analysis(state, qa_guidance)

        # Track execution state
        steps_done: list[StepRecord] = []
        backtracks_used = 0
        stagnation_count = 0
        strategy_changes_used = 0
        max_strategy_changes = 1
        judge_guidance = ""

        # ─── Stage 4: Unified Loop ───
        for iteration in range(max_iterations):
            tracer.set_iteration(iteration, max_iterations)
            _log(trace, "iteration", f"--- Iteration {iteration} ---")
            iter_obs: dict[str, Any] = {"iteration": iteration}

            # Update state with judge guidance from prior iteration
            if judge_guidance:
                state = update_judge_guidance(state, judge_guidance)
                workspace.write_judge_guidance(judge_guidance)

            # ── PlannerCoder: plan + generate code candidates ──
            with tracer.span("planner_coder", metadata={"iteration": iteration}):
                _log(trace, "planner_coder", "Planning and generating code")
                pc_output = planner_coder_generate(
                    question, manifest_json, data_profile, steps_done,
                    traced_llm, state=state, cm=cm,
                    qa_guidance=qa_guidance,
                )
            _log(trace, "planner_coder",
                 f"Plan: {pc_output.plan.step_description} | "
                 f"Language: {pc_output.language} | "
                 f"Candidates: {len(pc_output.candidates)} | "
                 f"Reasoning: {pc_output.reasoning[:100]}")

            iter_obs["plan_description"] = pc_output.plan.step_description
            iter_obs["language"] = pc_output.language
            iter_obs["num_candidates"] = len(pc_output.candidates)
            iter_obs["reasoning"] = pc_output.reasoning

            # ── Execute candidates in order ──
            result: SandboxResult | None = None
            winning_code = ""
            step_id = f"step_{iteration}"

            for ci, candidate_code in enumerate(pc_output.candidates):
                # Detect if candidate is raw SQL (no Python imports/statements)
                candidate_lang = _detect_language(candidate_code, pc_output.language)

                # Pre-execution validation (only for Python — raw SQL has no column refs to annotate)
                if candidate_lang == "python":
                    annotated_code, col_warnings = validate_column_references(
                        candidate_code, manifest,
                    )
                    if col_warnings:
                        _log(trace, "code_validator", f"Candidate {ci} warnings: {col_warnings}")
                        candidate_code = annotated_code

                with tracer.span("sandbox", metadata={"step_id": step_id, "candidate": ci, "lang": candidate_lang}):
                    candidate_result = sandbox.execute(
                        candidate_code, step_id=f"{step_id}_c{ci}",
                        language=candidate_lang,
                    )

                if candidate_result.return_code == 0:
                    result = candidate_result
                    winning_code = candidate_code
                    _log(trace, "sandbox",
                         f"Candidate {ci} ({candidate_lang}) succeeded | "
                         f"output: {len(candidate_result.stdout)} chars | "
                         f"time: {candidate_result.execution_time_ms}ms")
                    iter_obs["winning_candidate"] = ci
                    iter_obs["winning_language"] = candidate_lang
                    break
                else:
                    _log(trace, "sandbox",
                         f"Candidate {ci} ({candidate_lang}) failed: "
                         f"{candidate_result.stderr[:200]}")

            # If all candidates failed, try debugger on the first one
            if result is None or result.return_code != 0:
                base_code = pc_output.candidates[0] if pc_output.candidates else ""
                base_result = result or SandboxResult(
                    stdout="", stderr="No candidates generated",
                    return_code=-1, execution_time_ms=0, step_id=step_id,
                )

                previous_attempts: list[tuple[str, str]] = []
                for retry in range(max_retries):
                    with tracer.span("debugger", metadata={"retry": retry}):
                        fixed_code = debugger.fix(
                            base_code, base_result, manifest_json, data_profile,
                            traced_llm, state=state, cm=cm,
                            retry_number=retry,
                            previous_attempts=previous_attempts,
                        )
                    new_result = sandbox.execute(
                        fixed_code, step_id=f"{step_id}_fix{retry}",
                    )
                    _log(trace, "debugger", f"Retry {retry}: rc={new_result.return_code}")
                    previous_attempts.append((fixed_code[:500], new_result.stderr[:300]))

                    if new_result.return_code == 0:
                        result = new_result
                        winning_code = fixed_code
                        break
                    base_code = fixed_code
                    base_result = new_result

                # If still failing after retries, use the last result
                if result is None or result.return_code != 0:
                    result = base_result
                    winning_code = base_code

            # Write step to workspace
            workspace.write_step(iteration, winning_code, result.stdout)

            iter_obs["code_success"] = result.return_code == 0
            iter_obs["exec_time_ms"] = result.execution_time_ms
            iter_obs["stdout_preview"] = result.stdout[:200].strip()

            # Record step
            step_record = StepRecord(
                plan=pc_output.plan,
                code=winning_code,
                result=result,
                step_index=iteration,
            )
            steps_done.append(step_record)

            # Update state
            finding = summarize_step_output(result.stdout)
            state = add_step(state, step_record, finding)
            workspace.append_progress(iteration, pc_output.plan.step_description, finding)

            # ── HarnessGate: deterministic verification (zero LLM cost) ──
            harness_flags = harness_gate.check(
                question=question,
                code=winning_code,
                stdout=result.stdout,
                return_code=result.return_code,
                data_profile=data_profile,
                question_spec=question_spec,
                structured_json=result.structured_json,
            )

            blocking = [f for f in harness_flags if f.severity == "block"]
            warnings = [f for f in harness_flags if f.severity == "warn"]

            if harness_flags:
                flag_summary = "; ".join(
                    f"[{f.rule}:{f.severity}] {f.message[:80]}"
                    for f in harness_flags
                )
                _log(trace, "harness_gate", flag_summary)
                iter_obs["harness_flags"] = [
                    {"rule": f.rule, "severity": f.severity, "message": f.message}
                    for f in harness_flags
                ]

            if blocking:
                # Skip Judge — use blocking messages as guidance for next iteration
                judge_guidance = "\n".join(
                    f"[{f.rule}] {f.message}" for f in blocking
                )
                _log(trace, "harness_gate",
                     f"BLOCKED — skipping Judge, {len(blocking)} block flags")
                iter_obs["judge_sufficient"] = False
                iter_obs["judge_action"] = "continue (harness_blocked)"
                obs["iterations"].append(iter_obs)

                prev_guidance = judge_guidance
                continue

            # ── Judge: sufficiency + routing + guidance ──
            with tracer.span("judge", metadata={"iteration": iteration}):
                _log(trace, "judge", "Evaluating progress")
                verdict = judge.evaluate(
                    question, steps_done, traced_llm,
                    state=state, cm=cm,
                    iteration=iteration, max_iterations=max_iterations,
                    harness_warnings=warnings,
                )
            _log(trace, "judge",
                 f"sufficient={verdict.sufficient}, action={verdict.action}, "
                 f"missing={verdict.missing}")

            iter_obs["judge_sufficient"] = verdict.sufficient
            iter_obs["judge_action"] = verdict.action
            iter_obs["judge_reasoning"] = verdict.reasoning
            iter_obs["judge_guidance"] = verdict.guidance_for_next_step
            obs["iterations"].append(iter_obs)

            # Store guidance for next iteration
            prev_guidance = judge_guidance
            judge_guidance = verdict.guidance_for_next_step

            # ── Loop control ──
            if verdict.action == "finish":
                if iteration < min_iterations - 1:
                    _log(trace, "orchestrator",
                         f"Overriding premature finish at iteration {iteration} "
                         f"(min_iterations={min_iterations})")
                    judge_guidance = (
                        verdict.missing
                        or "Verify the results: cross-check against the data, "
                        "confirm row counts, and validate assumptions."
                    )
                else:
                    break

            elif verdict.action == "backtrack" and backtracks_used < backtrack_limit:
                truncate_to = max(0, min(verdict.truncate_to, len(steps_done) - 1))
                steps_done = steps_done[:truncate_to]
                state = truncate_to_step(state, truncate_to)
                backtracks_used += 1
                stagnation_count = 0
                judge_guidance = ""
                _log(trace, "judge", f"Backtracked to step {truncate_to}")

            else:
                # Stagnation detection
                step_failed = result.return_code != 0
                guidance_repeated = (
                    prev_guidance
                    and verdict.guidance_for_next_step
                    and prev_guidance.strip()[:80] == verdict.guidance_for_next_step.strip()[:80]
                )

                if guidance_repeated and not step_failed:
                    _log(trace, "orchestrator",
                         "Guidance repeated without code failure — "
                         "accepting current result.")
                    break

                if step_failed:
                    stagnation_count += 1
                else:
                    stagnation_count = 0

                if stagnation_count >= stagnation_threshold:
                    if strategy_changes_used >= max_strategy_changes:
                        _log(trace, "orchestrator",
                             "Early stop: stagnation persists after strategy change")
                        break
                    strategy_changes_used += 1
                    _log(trace, "orchestrator",
                         f"Forcing strategy change #{strategy_changes_used}")
                    judge_guidance = (
                        "MANDATORY STRATEGY CHANGE: The previous approach has failed "
                        f"{stagnation_count} consecutive times. Use a completely "
                        "different strategy — different columns, different joins, "
                        "different data loading method, or re-read raw data."
                    )
                    stagnation_count = 0
                    state = set_question_analysis(state, "")

        # ─── Stage 5: Finalizer ───
        with tracer.span("finalizer"):
            _log(trace, "finalizer", "Formatting final answer")
            answer = finalizer.format_answer(
                question, steps_done, traced_llm,
                state=state, cm=cm, benchmark=benchmark,
                guidelines=guidelines,
            )
        _log(trace, "finalizer", f"Answer columns: {list(answer.keys())}")

        elapsed = time.time() - start_time
        usage = llm.total_usage

        # Build execution path summary for diagnostics
        execution_path = [
            f"step_{i.get('iteration')}:{i.get('winning_language', i.get('language', '?'))}"
            for i in obs["iterations"]
        ]

        obs["final"] = {
            "total_iterations": len(obs["iterations"]),
            "backtracks_used": backtracks_used,
            "answer_columns": list(answer.keys()),
            "answer_rows": len(next(iter(answer.values()), [])) if answer else 0,
            "success": True,
            "execution_path": execution_path,
            "languages_used": sorted({i.get("winning_language", i.get("language", "?")) for i in obs["iterations"]}),
        }
        _log(trace, "summary",
             f"Completed in {len(obs['iterations'])} iterations | "
             f"Path: {' → '.join(execution_path)}")

        workspace.persist()
        tracer.set_observations(obs)
        tracer.finish(success=True)

        return TaskResult(
            task_id=task_id,
            question=question,
            answer=answer,
            steps=steps_done,
            trace=trace,
            observations=obs,
            total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            total_cost_usd=usage.get("cost_usd", 0.0),
            time_seconds=elapsed,
            benchmark=benchmark,
        )

    except Exception as e:
        elapsed = time.time() - start_time
        _log(trace, "error", str(e))
        obs["final"] = {"success": False, "error": str(e)}
        workspace.persist()
        tracer.set_observations(obs)
        tracer.finish(success=False, error=str(e))
        return TaskResult(
            task_id=task_id,
            question=question,
            answer={},
            steps=[],
            trace=trace,
            observations=obs,
            time_seconds=elapsed,
            success=False,
            error=str(e),
            benchmark=benchmark,
        )
    finally:
        sandbox.cleanup()


def _detect_language(code: str, planner_hint: str) -> str:
    """Detect whether a candidate is raw SQL or a Python script.

    Raw SQL: starts with SELECT/WITH/INSERT/CREATE or contains no Python
    keywords (import, def, print, =). The planner_hint from PlannerCoder
    is used as a tiebreaker.
    """
    stripped = code.strip()
    first_word = stripped.split()[0].upper() if stripped else ""

    # Obvious SQL starters
    if first_word in ("SELECT", "WITH", "INSERT", "CREATE", "UPDATE", "DELETE",
                      "ATTACH", "PRAGMA", "EXPLAIN"):
        # But if it also has Python (import, def), it's a Python script with SQL inside
        has_python = any(
            kw in code for kw in ("import ", "def ", "print(", "from ", "= ", "if ")
        )
        if not has_python:
            return "sql"

    return "python"


def _log(trace: list[dict], agent: str, message: str) -> None:
    trace.append({
        "timestamp": time.time(),
        "agent": agent,
        "message": message,
    })
