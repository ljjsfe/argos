"""Orchestrator: unified loop coordinating all agents.

Pipeline (v19 — reliability upgrade):
  Profiler → DomainRules → QuestionSpec(deterministic)
  → Loop(PlannerCoder → Sandbox → HarnessGate → Judge) → Finalizer

Key design (v19):
- Four-tier exit: BLOCK → retry, WARN+iter0 → soft-retry, PASS → Judge → accept/retry
- BLOCK rules (100% precision): nan (per-column), empty_output, empty_answer,
  dict_string, value_embellishment, error_string_answer, excuse_answer
- BLOCK never downgrades — known-bad answers never accepted
- Repeated WARN escalation: whitelisted rules escalate to BLOCK after ≥3 fires
- Code failure (rc!=0) skips to next iteration, not accepted
- Judge (1 LLM call) provides semantic verification after HarnessGate PASS
- Best clean result tracked for fallback on max_iterations exhaustion
- LLM calls: PlannerCoder(1-3) + Judge(0-3) + Finalizer(1) = 3-5 typical
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.context_manager import ContextManager
from ..core.llm_client import LLMClient
from ..core.sandbox import Sandbox
from ..core.state import (
    add_step,
    create_initial_state,
    set_doc_glossary_hints,
    set_domain_bindings,
    set_focus_hints,
    set_playbook_hints,
    set_question_analysis,
    set_task_mode,
    summarize_step_output,
    truncate_to_step,
    update_harness_feedback,
    update_judge_guidance,
    update_repl_state_summary,
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
from ..playbook import (
    format_for_prompt as _playbook_format,
    load_entries as _playbook_load,
    record_use as _playbook_record_use,
    retrieve_relevant as _playbook_retrieve,
)
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
    # In-memory deterministic task context (manifest + domain rules) computed
    # during Stage 1+2. HeavySkill extras can reuse this instead of re-running
    # the profiler/domain compile. Excluded from repr and compare because it
    # holds live objects (Manifest) that don't serialize cleanly.
    runtime_context: Any = field(default=None, repr=False, compare=False)


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
    precomputed_context: Any = None,
) -> TaskResult:
    """Run the full agent pipeline on a single task.

    When `precomputed_context` is provided (a `PrecomputedTaskContext` dataclass),
    Stage 1 (profiler scan) and Stage 2 (domain-rules extraction + compile)
    are SKIPPED — the cached manifest/domain_rules are used directly. This is
    safe because both are deterministic given task_dir. Used by HeavySkill
    heavy_runner to share work across K parallel trajectories on the same task.
    """
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

    # Optional in-process Python REPL with persistent globals across iterations.
    # Variables / imports from iteration N stay alive in N+1, so the agent
    # can build cumulative state. No os.chdir / os.environ writes — uses
    # thread-local TASK_DIR via data_helpers.set_task_context() for parallel
    # safety.
    stateful_repl = None
    if config.get("sandbox", {}).get("stateful_python", True):
        from ..core.stateful_python import StatefulPythonExec
        stateful_repl = StatefulPythonExec(
            task_dir=task_dir,
            temp_dir=sandbox.temp_dir,
            timeout=config.get("sandbox", {}).get("timeout_seconds", 120),
        )
        sandbox._stateful_repl = stateful_repl

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
        # ─── Stage 1+2: profile + domain rules (use cache if provided) ───
        if precomputed_context is not None:
            # Reuse manifest + domain_rules from heavy-mode shared cache.
            manifest = precomputed_context.manifest
            manifest_json = precomputed_context.manifest_json
            domain_rules_raw = precomputed_context.domain_rules_raw
            domain_rules = precomputed_context.domain_rules
            _log(trace, "profiler", f"Using shared cache: {len(manifest.entries)} files, "
                 f"{len(manifest.cross_source_relations)} relations")
            obs["profiler"] = {
                "files_found": len(manifest.entries),
                "file_types": sorted({e.file_type for e in manifest.entries}),
                "cross_source_relations": len(manifest.cross_source_relations),
                "total_size_bytes": sum(e.size_bytes for e in manifest.entries),
                "shared_cache_used": True,
            }
        else:
            # Stage 1: Profile (deterministic, zero LLM cost)
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

            # Stage 2: Domain rules (deterministic + optional LLM compile)
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

                # Promote metric definitions to a structured top-of-doc index.
                # Helps small models locate formulas without scanning long prose.
                from .metric_matcher import build_metric_index
                metric_index = build_metric_index(domain_rules_raw)
                if metric_index:
                    domain_rules = metric_index + "\n\n---\n\n" + domain_rules
                    _log(trace, "domain_rules",
                         f"Promoted {metric_index.count(chr(10) + '- ')} metric definitions to index")

        workspace.write_domain_rules(domain_rules)

        obs["analyzer"] = {
            "domain_rules_length_chars": len(domain_rules),
        }

        # Capture the deterministic task-level work for HeavySkill extras to
        # reuse without re-running profiler / domain extraction. Only built
        # when this run did the work itself — when precomputed_context is
        # provided, we keep that same reference so callers can chain.
        from ..core.types import PrecomputedTaskContext as _PrecCtx
        runtime_ctx = precomputed_context if precomputed_context is not None else _PrecCtx(
            manifest=manifest,
            manifest_json=manifest_json,
            domain_rules_raw=domain_rules_raw,
            domain_rules=domain_rules,
        )

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

        # ─── Stage 3c: Deterministic task routing (zero LLM) ───
        task_mode = _route_task(manifest, domain_rules)
        state = set_task_mode(state, task_mode)
        _log(trace, "task_router", f"Task mode: {task_mode}")
        obs["task_mode"] = task_mode

        # ─── Stage 3c': Focus hints — question entity → manifest binding ───
        # Deterministic, zero LLM. Universal across benchmarks. Replaces the
        # filename-as-hint mechanism that previously leaked through polluted
        # inputs (see docs/LEAK_TO_HONEST_INFO_MAP.md).
        try:
            from .focus_hints import build_focus_hints
            _fh = build_focus_hints(question, manifest)
            if _fh:
                state = set_focus_hints(state, _fh)
                _log(trace, "focus_hints", f"{_fh.count(chr(10))+1} hints generated")
                obs["focus_hints_lines"] = _fh.count("\n") + 1
        except Exception as e:
            _log(trace, "focus_hints", f"skipped: {e}")

        # ─── Stage 3c'': Domain bindings — question entity → knowledge.md term ───
        try:
            from .term_binding import build_domain_bindings
            _db = build_domain_bindings(question, task_dir, manifest)
            if _db:
                state = set_domain_bindings(state, _db)
                _log(trace, "domain_bindings", f"{_db.count(chr(10))+1} bindings generated")
                obs["domain_bindings_lines"] = _db.count("\n") + 1
        except Exception as e:
            _log(trace, "domain_bindings", f"skipped: {e}")

        # ─── Stage 3c''': B2 doc glossary (LLM-extracted, format-agnostic) ───
        # Universal replacement for A2's markdown-bold-prefix parser. ONE LLM
        # call per task (cached) → structured glossary → deterministic match
        # → DOC_GLOSSARY_HINTS in PlannerCoder context. Coexists with A2;
        # A2 will retire after eval evidence shows B2 dominates.
        try:
            from .doc_glossary import extract_doc_glossary, build_doc_glossary_hints
            _glossary = extract_doc_glossary(task_dir, manifest, llm)
            _dg_hints = build_doc_glossary_hints(question, _glossary)
            if _dg_hints:
                state = set_doc_glossary_hints(state, _dg_hints)
                _log(trace, "doc_glossary",
                     f"{len(_glossary.terms)} terms; {_dg_hints.count(chr(10))+1} hints")
                obs["doc_glossary_terms"] = len(_glossary.terms)
                obs["doc_glossary_hint_lines"] = _dg_hints.count("\n") + 1
        except Exception as e:
            _log(trace, "doc_glossary", f"skipped: {e}")

        # ─── Stage 3d: Playbook retrieval (curated patterns, zero LLM) ───
        # Fail-soft: missing/empty playbook → no hints, no behavior change.
        try:
            _pb_entries = _playbook_load()
            _pb_top = _playbook_retrieve(
                _pb_entries, question, schema_hint=state.manifest_summary, k=5,
            )
            _pb_hints = _playbook_format(_pb_top)
            _pb_ids = tuple(e.id for e in _pb_top)
            if _pb_hints:
                state = set_playbook_hints(state, _pb_hints, _pb_ids)
                _log(trace, "playbook",
                     f"Retrieved {len(_pb_top)} entries: {list(_pb_ids)}")
                obs["playbook_retrieved"] = list(_pb_ids)
            else:
                obs["playbook_retrieved"] = []
        except Exception as e:
            _log(trace, "playbook", f"retrieval failed (fail-soft): {e}")
            obs["playbook_retrieved"] = []

        # ─── Stage 3e: Image attachments (zero LLM, just discovery) ───
        # If the manifest contains image or scanned-style PDF files, prepare
        # to attach them to every PlannerCoder call. The agent decides whether
        # to use the visual or the structured path; we don't hard-route.
        # KDD/DABstep tasks have no images → empty tuple → falls through to
        # ordinary chat() path with no behavior change.
        image_paths: tuple[str, ...] = tuple(
            e.file_path for e in manifest.entries
            if e.file_type in ("image", "pdf")
        )
        if image_paths:
            _log(trace, "vision", f"Attaching {len(image_paths)} visual file(s) to planner: "
                 f"{[__import__('os').path.basename(p) for p in image_paths]}")
            obs["vision_attached"] = [__import__("os").path.basename(p) for p in image_paths]
        else:
            obs["vision_attached"] = []

        # Track execution state
        steps_done: list[StepRecord] = []
        # Best non-blocked result for fallback when max_iterations exhausted
        best_clean_result: tuple[StepRecord, str, SandboxResult] | None = None
        # Track per-rule WARN counts for escalation
        warn_counts: dict[str, int] = {}  # rule_name → consecutive count
        # Track per-rule code seen while escalation has fired — if the agent
        # produces the same code after a BLOCK escalation, the rule is most
        # likely wrong and locking the agent. Demote back to WARN.
        escalated_codes: dict[str, set[str]] = {}
        # Track consecutive block counts per rule for stagnation detection.
        # When the same block rule fires ≥3 times in a row, the agent is
        # stuck — add a stagnation alert to force a completely new approach.
        block_rule_counts: dict[str, int] = {}

        # ─── Stage 4: Unified Loop ───
        for iteration in range(max_iterations):
            tracer.set_iteration(iteration, max_iterations)
            _log(trace, "iteration", f"--- Iteration {iteration} ---")
            iter_obs: dict[str, Any] = {"iteration": iteration}

            # ── Refresh REPL state summary (only when REPL is active) ──
            # Lists variables alive from prior iterations so PlannerCoder
            # context can show "Available REPL State" and prompt the agent
            # to reuse rather than re-load.
            if stateful_repl is not None and iteration > 0:
                try:
                    var_dict = stateful_repl.list_vars()
                    if var_dict:
                        summary = "\n".join(f"- `{k}`: {v}" for k, v in var_dict.items())
                        state = update_repl_state_summary(state, summary)
                except Exception:
                    pass  # introspection should never break the loop

            # ── PlannerCoder: plan + generate code candidates ──
            with tracer.span("planner_coder", metadata={"iteration": iteration}):
                _log(trace, "planner_coder", "Planning and generating code")
                pc_output = planner_coder_generate(
                    question, manifest_json, "", steps_done,
                    traced_llm, state=state, cm=cm,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    image_paths=image_paths,
                )
            _log(trace, "planner_coder",
                 f"Plan: {pc_output.plan.step_description} | "
                 f"Language: {pc_output.language} | "
                 f"Candidates: {len(pc_output.candidates)} | "
                 f"Parse: {pc_output.parse_status} | "
                 f"Reasoning: {pc_output.reasoning[:100]}")

            iter_obs["plan_description"] = pc_output.plan.step_description
            iter_obs["language"] = pc_output.language
            iter_obs["num_candidates"] = len(pc_output.candidates)
            iter_obs["parse_status"] = pc_output.parse_status
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
                         f"{candidate_result.stderr[:1500]}")

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
            weak_column_rules = {"qa_column_count", "sql_column_count"}

            # ── WARN escalation: whitelisted rules → BLOCK after ≥3 fires ──
            # Source of truth is ESCALATABLE_RULES in harness_gate.py so that
            # rule authors can see (next to the rule) whether it escalates.
            _ESCALATION_WHITELIST = harness_gate.ESCALATABLE_RULES
            # Update counts for current warnings
            current_warn_rules = {f.rule for f in warnings}
            for rule in current_warn_rules:
                warn_counts[rule] = warn_counts.get(rule, 0) + 1
            # Reset counts for rules not firing this iteration
            for rule in list(warn_counts):
                if rule not in current_warn_rules:
                    warn_counts[rule] = 0

            # Escalate — but only when the agent has actually changed code
            # since the last BLOCK from this rule. If the same code keeps
            # producing the same WARN, the rule is wrong and locking the
            # agent (see task_169 in v21: 7 consecutive identical SUM/12
            # blocked queries because agg_type fired on a valid decomposed
            # average). Demoting back to WARN lets the loop progress.
            escalated: list[HarnessFlag] = []
            remaining_warnings: list[HarnessFlag] = []
            code_signature = (winning_code or "").strip()
            for f in warnings:
                eligible = (
                    f.rule in _ESCALATION_WHITELIST
                    and warn_counts.get(f.rule, 0) >= 3
                )
                if not eligible:
                    remaining_warnings.append(f)
                    continue
                seen_codes = escalated_codes.setdefault(f.rule, set())
                if code_signature and code_signature in seen_codes:
                    # Already escalated on this exact code — the rule is
                    # not converging. Stop escalating; keep the WARN.
                    remaining_warnings.append(f)
                    continue
                seen_codes.add(code_signature)
                escalated.append(HarnessFlag(
                    rule=f.rule,
                    severity="block",
                    message=_escalated_harness_message(
                        f, warn_counts[f.rule], question_spec,
                    ),
                ))

            if escalated:
                blocking = blocking + escalated
                warnings = remaining_warnings
                _log(trace, "harness_gate",
                     f"Escalated {len(escalated)} repeated WARNs to BLOCK: "
                     f"{[f.rule for f in escalated]}")

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
                if escalated:
                    iter_obs["escalated_warns"] = [f.rule for f in escalated]

            if blocking:
                # Known-bad answer — retry without Judge
                block_rules = [f.rule for f in blocking]
                # Stagnation detection: if the same rule fires ≥3 consecutive
                # times the agent is stuck. Prepend a STAGNATION alert.
                stagnation_rules = []
                for rule in block_rules:
                    block_rule_counts[rule] = block_rule_counts.get(rule, 0) + 1
                    if block_rule_counts[rule] >= 3:
                        stagnation_rules.append(rule)
                # Reset counts for rules not firing this iteration
                for rule in list(block_rule_counts):
                    if rule not in block_rules:
                        block_rule_counts[rule] = 0

                harness_msg = "\n".join(
                    f"[{f.rule}] {f.message}" for f in blocking
                )
                if stagnation_rules:
                    stagnation_msg = (
                        f"STAGNATION ALERT: Rule(s) {stagnation_rules} have blocked "
                        f"{max(block_rule_counts[r] for r in stagnation_rules)} times in a row. "
                        f"Your current approach is fundamentally wrong for this data. "
                        f"Try a COMPLETELY DIFFERENT method: different table, "
                        f"different column, different join, or switch language (SQL↔Python)."
                    )
                    harness_msg = stagnation_msg + "\n\n" + harness_msg
                    _log(trace, "harness_gate",
                         f"STAGNATION detected for {stagnation_rules} — injecting pivot guidance")

                state = update_harness_feedback(state, harness_msg)
                _log(trace, "harness_gate",
                     f"BLOCKED — {len(blocking)} block flags, retrying")
                iter_obs["judge_action"] = "continue (harness_blocked)"
                obs["iterations"].append(iter_obs)
                continue

            # ── Non-blocking iteration: reset block counts ──
            for rule in list(block_rule_counts):
                block_rule_counts[rule] = 0

            # ── Guard: if code ran but save_result() was never called, force retry ──
            if not result.structured_json:
                has_computed_output = bool(re.search(
                    r'(?:percentage|count|total|average|result|sum|mean|ratio)[:\s=]+[\d.]+',
                    result.stdout, re.IGNORECASE,
                ))
                if has_computed_output:
                    save_msg = (
                        "[missing_save_result] Code produced a numeric result but "
                        "save_result() was never called — the answer was not captured. "
                        "You MUST call save_result(answer={'column_name': [value]}) "
                        "at the END of your code. Do not just print() the result."
                    )
                    state = update_harness_feedback(state, save_msg)
                    _log(trace, "harness_gate",
                         "BLOCKED — code computed answer but save_result() not called")
                    iter_obs["judge_action"] = "continue (missing_save_result)"
                    obs["iterations"].append(iter_obs)
                    continue

            # ── Track best non-blocked result for fallback ──
            best_clean_result = (step_record, winning_code, result)

            actionable_warnings = [
                f for f in warnings if f.rule not in weak_column_rules
            ]

            # ── WARN soft-retry: retry once on first iteration ──
            # Column-count warnings are weak evidence from regex heuristics;
            # do not let them drive loop control or rewrite attempts.
            if actionable_warnings and iteration == 0:
                harness_msg = "\n".join(
                    f"[{f.rule}] {f.message}" for f in actionable_warnings
                )
                state = update_harness_feedback(state, harness_msg)
                _log(trace, "harness_gate",
                     f"WARN soft-retry — {len(actionable_warnings)} warnings, retrying once")
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
                    question_spec=question_spec,
                )

            _log(trace, "judge",
                 f"Decision: {judge_decision.action} | "
                 f"Reasoning: {judge_decision.reasoning[:120]}")
            iter_obs["judge_action"] = judge_decision.action
            iter_obs["judge_reasoning"] = judge_decision.reasoning

            if judge_decision.action == "finish":
                # Guard: never accept finish with empty structured_json.
                # If save_result() wasn't called, the finalizer would write
                # an empty prediction regardless of what Judge "saw" in stdout.
                if not result.structured_json:
                    save_msg = (
                        "[missing_save_result] Judge says done but save_result() "
                        "was never called — the answer cannot be captured. "
                        "You MUST end your code with: "
                        "save_result(answer={'column_name': [value]})"
                    )
                    state = update_harness_feedback(state, save_msg)
                    _log(trace, "harness_gate",
                         "BLOCKED — Judge finish overridden: save_result() not called")
                    iter_obs["judge_action"] = "continue (finish_overridden_missing_save_result)"
                    obs["iterations"].append(iter_obs)
                    continue

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
            actionable_warnings = [
                f for f in warnings if f.rule not in weak_column_rules
            ]
            if actionable_warnings:
                warn_msg = "\n".join(
                    f"[{f.rule}] {f.message}" for f in actionable_warnings
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
                question_spec=question_spec,
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

        # Record playbook use (use_count only — win_count needs gold and is
        # set by the eval scorer when it runs, not at task completion time).
        if state.playbook_entry_ids:
            try:
                _playbook_record_use(state.playbook_entry_ids, won=False)
            except Exception:  # fail-soft: telemetry never breaks the agent
                pass
        _log(trace, "summary",
             f"Completed in {len(obs['iterations'])} iterations | "
             f"Path: {' → '.join(execution_path)}")

        workspace.persist()
        tracer.set_observations(obs)
        tracer.finish(success=True)

        if stateful_repl is not None:
            stateful_repl.cleanup()

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
            runtime_context=runtime_ctx,
        )

    except Exception as e:
        elapsed = time.time() - start_time
        _log(trace, "error", str(e))
        obs["final"] = {"success": False, "error": str(e)}
        workspace.persist()
        tracer.set_observations(obs)
        tracer.finish(success=False, error=str(e))
        if stateful_repl is not None:
            stateful_repl.cleanup()
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


# ---------------------------------------------------------------------------
# Deterministic task routing
# ---------------------------------------------------------------------------

def _route_task(manifest: Manifest, domain_rules: str) -> str:
    """Deterministic task routing based on manifest structure.

    Returns a task_mode string. Zero LLM cost.
    """
    structured = [
        e for e in manifest.entries
        if e.file_type in ("csv", "json", "sqlite", "excel", "parquet")
    ]
    unstructured = [
        e for e in manifest.entries
        if e.file_type in ("markdown", "pdf", "docx")
    ]

    has_structured = len(structured) > 0
    has_unstructured = len(unstructured) > 0
    multi_table = len(structured) > 1 or any(
        len(e.summary.get("tables", [])) > 1 for e in structured
    )

    # Unstructured files present → Python extraction likely needed
    if has_unstructured and not has_structured:
        return "python_extract"

    # Multiple structured sources with cross-source relations → multi SQL
    if multi_table and len(manifest.cross_source_relations) > 0:
        return "multi_sql"

    # Multiple structured but no detected relations → still multi SQL
    if multi_table:
        return "multi_sql"

    # Single structured source + domain docs → note docs available
    if has_structured and domain_rules:
        return "document_needed"

    # Default: single SQL
    if has_structured:
        return "single_sql"

    return "general"


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


def _escalated_harness_message(
    flag: HarnessFlag,
    count: int,
    question_spec: Any,
) -> str:
    """Replace repeated weak feedback with a concrete required change."""
    prefix = f"[ESCALATED — same warning {count}x] "
    qtype = getattr(question_spec, "computation_type", "unknown")

    if flag.rule == "agg_type":
        extra = ""
        if qtype != "ratio":
            extra = " Do NOT multiply by 100 unless the question asks for a percentage."
        return (
            prefix
            + "CRITICAL: change the aggregation, do not repeat the same query. "
            + flag.message
            + " If the question asks for average/mean, use AVG(...) or .mean(), "
            + "not SUM(...)."
            + extra
        )

    if flag.rule == "qa_column_count":
        return (
            prefix
            + "CRITICAL: the answer column count is wrong. Return ONLY the "
            + "columns directly requested by the question; move join keys, "
            + "metrics used only for sorting, and debug fields out of answer."
            + " "
            + flag.message
        )

    if flag.rule == "join_cardinality":
        return (
            prefix
            + "CRITICAL: the join appears to multiply rows. Use a different "
            + "join key, cast key types to match, or pre-aggregate/deduplicate "
            + "before joining. "
            + flag.message
        )

    if flag.rule == "sql_where_value":
        return (
            prefix
            + "CRITICAL: the filter value does not match known values. Inspect "
            + "distinct values and use the exact spelling/case from the data. "
            + flag.message
        )

    return prefix + flag.message


def _log(trace: list[dict], agent: str, message: str) -> None:
    trace.append({
        "timestamp": time.time(),
        "agent": agent,
        "message": message,
    })
