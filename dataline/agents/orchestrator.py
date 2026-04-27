"""Orchestrator: unified loop coordinating all agents.

Pipeline (v18 — deterministic fixes + Judge re-enable):
  Profiler → DomainRules → QuestionSpec(deterministic)
  → Loop(PlannerCoder → Sandbox → HarnessGate → Judge) → Finalizer

Key design (v18):
- Four-tier exit: BLOCK → retry, WARN+iter0 → soft-retry, PASS → Judge → accept/retry
- 7 BLOCK rules (100% precision): nan, empty_output, empty_answer, dict_string,
  value_embellishment, error_string_answer, excuse_answer
- BLOCK never downgrades — known-bad answers never accepted
- Code failure (rc!=0) skips to next iteration, not accepted
- Judge (1 LLM call) provides semantic verification after HarnessGate PASS
- Best clean result tracked for fallback on max_iterations exhaustion
- LLM calls: PlannerCoder(1-3) + Judge(0-3) + Finalizer(1) = 3-5 typical
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
    update_harness_feedback,
    update_judge_guidance,
)
from ..core.tracer import TaskTracer
from ..core.tracing_llm import TracingLLMClient
from ..core.types import (
    AnalysisState,
    HarnessFlag,
    Manifest,
    PlanStep,
    SandboxResult,
    StepRecord,
)
from ..core.workspace import Workspace
from ..profiler import manifest as profiler
from ..profiler.manifest import manifest_to_json
from . import analyzer, debugger, finalizer, harness_gate, judge as judge_agent
from .question_analyzer import analyze_deterministic
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
    max_iterations = config.get("agent", {}).get("max_iterations", 3)
    max_retries = config.get("agent", {}).get("max_retries", 2)

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

        # ─── Stage 2: Domain rules (deterministic, zero LLM cost) ───
        with tracer.span("domain_rules"):
            _log(trace, "domain_rules", "Extracting domain rules from docs")
            domain_rules_raw = analyzer._extract_domain_rules(manifest)

            # Compile if very large (conditional LLM call)
            domain_rules = analyzer.compile_domain_rules(
                domain_rules_raw, traced_llm, cm.budget_tokens,
            )
            if len(domain_rules) < len(domain_rules_raw):
                _log(trace, "domain_rules",
                     f"Compiled: {len(domain_rules_raw)} → {len(domain_rules)} chars")

        workspace.write_domain_rules(domain_rules)

        obs["analyzer"] = {
            "domain_rules_length_chars": len(domain_rules),
        }

        # ─── Stage 3: Initialize state ───
        state = create_initial_state(
            task_id, question, manifest, "", domain_rules,
        )

        # ─── Stage 3b: Deterministic question shape inference (zero LLM) ───
        question_spec = analyze_deterministic(question)
        spec_guidance = question_spec.to_guidance()
        if spec_guidance:
            state = set_question_analysis(state, spec_guidance)
            _log(trace, "question_spec",
                 f"Inferred: {question_spec.answer_type}/{question_spec.computation_type}"
                 f" rows={question_spec.expected_row_count}")
        obs["question_spec"] = {
            "answer_type": question_spec.answer_type,
            "computation_type": question_spec.computation_type,
            "expected_row_count": question_spec.expected_row_count,
            "tie_possible": question_spec.tie_possible,
        }

        # Track execution state
        steps_done: list[StepRecord] = []
        # Best non-blocked result for fallback when max_iterations exhausted
        best_clean_result: tuple[StepRecord, str, SandboxResult] | None = None

        # ─── Stage 4: Unified Loop ───
        for iteration in range(max_iterations):
            tracer.set_iteration(iteration, max_iterations)
            _log(trace, "iteration", f"--- Iteration {iteration} ---")
            iter_obs: dict[str, Any] = {"iteration": iteration}

            # ── PlannerCoder: plan + generate code candidates ──
            with tracer.span("planner_coder", metadata={"iteration": iteration}):
                _log(trace, "planner_coder", "Planning and generating code")
                pc_output = planner_coder_generate(
                    question, manifest_json, "", steps_done,
                    traced_llm, state=state, cm=cm,
                    iteration=iteration,
                    max_iterations=max_iterations,
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
                            base_code, base_result, manifest_json, "",
                            traced_llm, state=state, cm=cm,
                            retry_number=retry,
                            previous_attempts=previous_attempts,
                            question=question,
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

            # ── Code failure: skip to next iteration (don't accept bad output) ──
            if result.return_code != 0:
                _log(trace, "orchestrator",
                     f"All code failed at iteration {iteration} — retrying")
                iter_obs["code_success"] = False
                iter_obs["judge_action"] = "continue (all_code_failed)"
                # Still record step for context in next iteration
                step_record = StepRecord(
                    plan=pc_output.plan, code=winning_code,
                    result=result, step_index=iteration,
                )
                steps_done.append(step_record)
                finding = summarize_step_output(result.stdout)
                state = add_step(state, step_record, finding)
                obs["iterations"].append(iter_obs)
                continue

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
                data_profile=state.manifest_summary,
                question_spec=question_spec,
                structured_json=result.structured_json,
            )

            # ── Classify flags (no fatigue downgrade — BLOCKs stay BLOCKs) ──
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
                # Known-bad answer — retry without Judge
                harness_msg = "\n".join(
                    f"[{f.rule}] {f.message}" for f in blocking
                )
                state = update_harness_feedback(state, harness_msg)
                _log(trace, "harness_gate",
                     f"BLOCKED — {len(blocking)} block flags, retrying")
                iter_obs["judge_action"] = "continue (harness_blocked)"
                obs["iterations"].append(iter_obs)
                continue

            # ── Track best non-blocked result for fallback ──
            best_clean_result = (step_record, winning_code, result)

            # ── WARN soft-retry: retry once on first iteration ──
            if warnings and iteration == 0:
                harness_msg = "\n".join(
                    f"[{f.rule}] {f.message}" for f in warnings
                )
                state = update_harness_feedback(state, harness_msg)
                _log(trace, "harness_gate",
                     f"WARN soft-retry — {len(warnings)} warnings, retrying once")
                iter_obs["judge_action"] = "continue (warn_soft_retry)"
                obs["iterations"].append(iter_obs)
                continue

            # ── Judge: semantic verification (1 LLM call) ──
            with tracer.span("judge", metadata={"iteration": iteration}):
                judge_decision = judge_agent.evaluate(
                    question=question,
                    steps_done=steps_done,
                    llm=traced_llm,
                    state=state,
                    cm=cm,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    harness_warnings=warnings,
                )

            _log(trace, "judge",
                 f"Decision: {judge_decision.action} | "
                 f"Reasoning: {judge_decision.reasoning[:120]}")
            iter_obs["judge_action"] = judge_decision.action
            iter_obs["judge_reasoning"] = judge_decision.reasoning

            if judge_decision.action == "finish":
                if state.harness_feedback:
                    state = update_harness_feedback(state, "")
                _log(trace, "orchestrator",
                     f"Judge accepted — finishing at iteration {iteration}")
                obs["iterations"].append(iter_obs)
                break

            # Judge says continue or backtrack — store guidance for next iteration
            if judge_decision.action == "backtrack":
                truncate_to = judge_decision.truncate_to
                state = truncate_to_step(state, truncate_to)
                _log(trace, "judge", f"Backtrack to step {truncate_to}")

            if judge_decision.guidance_for_next_step:
                state = update_judge_guidance(
                    state, judge_decision.guidance_for_next_step,
                )
            if warnings:
                warn_msg = "\n".join(
                    f"[{f.rule}] {f.message}" for f in warnings
                )
                state = update_harness_feedback(state, warn_msg)

            obs["iterations"].append(iter_obs)

        else:
            # Loop exhausted max_iterations without break
            if best_clean_result is not None:
                step_record, winning_code, result = best_clean_result
                _log(trace, "orchestrator",
                     "Max iterations — using best non-blocked result")
            else:
                _log(trace, "orchestrator",
                     "Max iterations — all blocked, accepting last result")

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
    keywords. The planner_hint from PlannerCoder is used as a tiebreaker.
    """
    stripped = code.strip()
    first_word = stripped.split()[0].upper() if stripped else ""

    # Obvious SQL starters
    sql_starters = ("SELECT", "WITH", "INSERT", "CREATE", "UPDATE", "DELETE",
                    "ATTACH", "PRAGMA", "EXPLAIN")
    if first_word in sql_starters:
        # Only reject as SQL if it has unambiguous Python keywords
        # (not "= " which matches SQL WHERE/ON clauses)
        has_python = any(
            kw in code for kw in ("import ", "def ", "print(", "from ", "class ")
        )
        if not has_python:
            return "sql"

    # Trust planner hint when code doesn't obviously start with SQL
    # but also doesn't have Python keywords
    if planner_hint == "sql" and first_word not in ("", ):
        has_python = any(
            kw in code for kw in ("import ", "def ", "print(", "from ", "class ")
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
