"""HeavySkill orchestrator wrapper: failure-triggered K-trajectory + deliberation.

Wraps the existing `orchestrator.run_task()` with the four-phase HeavySkill
loop:

  Phase 1: run baseline trajectory (temp=0) using the existing pipeline.
  Phase 2: deterministic confidence check on the baseline trajectory.
           If confident, return Phase-1 result unchanged (saves 2x cost).
  Phase 3: spawn K-1 additional trajectories at higher temperatures.
  Phase 4: deliberator synthesizes the K trajectories into one answer.

The wrapper is intentionally a separate file from orchestrator.run_task so
the baseline pipeline stays untouched and the HeavySkill code path is
clearly opt-in via config (`heavy_mode.enabled`).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd

from ..core.llm_client import LLMClient
from ..core.types import HeavyDecision, HeavyTrajectory
from . import heavy_confidence
from . import heavy_deliberator
from .orchestrator import TaskResult, run_task

logger = logging.getLogger(__name__)


def run_task_heavy(
    task_dir: str,
    question: str,
    llm: LLMClient,
    config: dict,
    *,
    task_id: str = "",
    output_dir: str = "",
    benchmark: str = "kdd",
    guidelines: str = "",
    session_id: str = "",
) -> TaskResult:
    """Run a task with optional HeavySkill heavy-mode escalation.

    If `heavy_mode.enabled` is false in config, behaves identically to
    `run_task`. Otherwise, runs the baseline trajectory, checks confidence,
    and possibly spawns K-1 additional trajectories + deliberator.

    Returns a TaskResult whose `answer` has been replaced with the
    deliberator's synthesized output (when heavy mode triggered) or kept
    as the baseline answer (when confident).
    """
    heavy_cfg = (config or {}).get("heavy_mode", {}) or {}
    max_iterations = (config or {}).get("agent", {}).get("max_iterations", 8)

    # Track real wall time of the whole heavy_runner invocation. The
    # individual TaskResults' time_seconds are sequential (each one's own
    # elapsed) and would double-count the K-1 trajectories that run in
    # parallel. Reporting their sum overstates the wall-clock cost.
    wrapper_start = time.time()

    # ── Phase 1: baseline trajectory at the LLM's current temperature ──
    baseline = run_task(
        task_dir=task_dir,
        question=question,
        llm=llm,
        config=config,
        task_id=task_id,
        output_dir=output_dir,
        benchmark=benchmark,
        guidelines=guidelines,
        session_id=session_id,
    )

    if not heavy_cfg.get("enabled", False):
        return baseline

    trigger_mode = (heavy_cfg.get("trigger", "auto") or "auto").lower()

    # ── Phase 2: confidence gate ──
    if trigger_mode == "off":
        return baseline
    if trigger_mode == "auto":
        report = heavy_confidence.evaluate_confidence(baseline, max_iterations=max_iterations)
        baseline.observations.setdefault("heavy_mode", {})["confidence_report"] = {
            "is_confident": report.is_confident,
            "reasons": list(report.reasons),
        }
        if report.is_confident:
            baseline.observations["heavy_mode"]["triggered"] = False
            return baseline
    # trigger_mode == "always" falls through (forces heavy mode regardless of confidence)

    # ── Phase 3: spawn K-1 additional trajectories ──
    K = int(heavy_cfg.get("k_trajectories", 3))
    temps = list(heavy_cfg.get("trajectory_temperatures", [0.0, 0.7, 0.7]))
    if len(temps) < K:
        temps = temps + [0.7] * (K - len(temps))

    # Run K-1 additional trajectories CONCURRENTLY (not serially). Each
    # trajectory has its own Sandbox (different temp_dir) and output_dir,
    # so there are no shared-state collisions. The LLMClient's OpenAI client
    # is thread-safe; only the _total_usage counter is shared but its
    # underlying update is a single dict mutation (acceptable accounting drift).
    #
    # Wall-time impact: when K=3, sequential ran 2 trajectories back-to-back
    # (~660s typical). Parallel runs them simultaneously (~330s = max of the two),
    # cutting heavy-task wall time roughly in half. Same total token budget.
    extra_trajectory_dirs = [
        _trajectory_output_dir(output_dir, traj_id) for traj_id in range(1, K)
    ]
    additional_results: list[TaskResult] = []
    if K > 1:
        parallel_start = time.time()
        with ThreadPoolExecutor(max_workers=K - 1) as ex:
            futures = {
                ex.submit(
                    _run_one_trajectory,
                    task_dir=task_dir,
                    question=question,
                    llm=llm.with_temperature(float(temps[traj_id])),
                    config=config,
                    task_id=f"{task_id}_t{traj_id}",
                    output_dir=extra_trajectory_dirs[traj_id - 1],
                    benchmark=benchmark,
                    guidelines=guidelines,
                    session_id=f"{session_id}__t{traj_id}" if session_id else "",
                    traj_id=traj_id,
                    traj_temp=float(temps[traj_id]),
                ): traj_id
                for traj_id in range(1, K)
            }
            for fut in as_completed(futures):
                tid = futures[fut]
                try:
                    res = fut.result()
                    if res is not None:
                        additional_results.append(res)
                except Exception as e:
                    logger.warning("Heavy trajectory %d crashed: %s", tid, e)
        logger.info("Heavy %d trajectories ran in parallel in %.1fs",
                    K - 1, time.time() - parallel_start)

    all_results = [baseline, *additional_results]
    trajectories = [
        _to_heavy_trajectory(r, traj_id=i, temperature=float(temps[i]) if i < len(temps) else 0.7)
        for i, r in enumerate(all_results)
    ]

    # ── Phase 4: deliberator synthesizes final answer ──
    # Deliberator runs at temperature=0 for stable synthesis (paper §4.2:
    # deliberation benefits from instruction-following over creative sampling).
    deliberator_llm = llm.with_temperature(0.0)
    domain_rules = baseline.observations.get("analyzer", {}).get("domain_rules_compiled", "")

    decision = heavy_deliberator.deliberate(
        question=question,
        trajectories=trajectories,
        llm=deliberator_llm,
        domain_rules=domain_rules,
        seed=heavy_cfg.get("deliberator_shuffle_seed"),
    )

    # ── Build the final TaskResult ──
    # Strategy: keep baseline.trace + .observations (most informative — the
    # actual ReAct loop ran here), but replace `answer` with deliberator
    # output. Add a heavy_mode block to observations for telemetry.
    final_answer = _csv_to_answer_dict(decision.final_answer_csv)

    # Persist per-trajectory predictions so debugging + post-hoc analysis
    # can see what each thinker actually proposed (the deliberator may have
    # overridden a correct baseline). One CSV per trajectory + an aggregate
    # JSON for quick inspection.
    _save_trajectory_artifacts(
        output_dir=output_dir,
        baseline=baseline,
        additional_results=additional_results,
        trajectories=trajectories,
        decision=decision,
    )

    baseline.observations.setdefault("heavy_mode", {}).update({
        "triggered": True,
        "k_trajectories": len(trajectories),
        "trajectory_temperatures": [t.temperature for t in trajectories],
        "trajectory_answers_distinct": _count_distinct_answers(trajectories),
        "deliberator_matched_trajectory_id": decision.matched_trajectory_id,
        "deliberator_reasoning": decision.reasoning[:500],
        "extra_trajectory_dirs": extra_trajectory_dirs,
        "baseline_answer_csv_preview": _answer_dict_to_csv(baseline.answer)[:1000],
        # Compute-time accounting (sum across trajectories; double-counts the
        # parallel ones, kept here for cost/usage transparency).
        "compute_time_seconds": round(sum(r.time_seconds for r in all_results), 2),
    })

    # Real wall-clock time of the whole heavy_runner invocation. Extra
    # trajectories run in parallel via ThreadPoolExecutor, so summing their
    # individual time_seconds overstates the user-perceived latency by
    # ~ (K-1)x for K=3. Use the wrapper-level timer instead — it's what
    # downstream KDD budget calculations care about.
    wall_seconds = round(time.time() - wrapper_start, 2)

    return TaskResult(
        task_id=baseline.task_id,
        question=baseline.question,
        answer=final_answer,
        steps=baseline.steps,
        trace=baseline.trace + [
            {"agent": "heavy_deliberator", "message": f"Decision: matched={decision.matched_trajectory_id} | {decision.reasoning[:120]}"},
        ],
        observations=baseline.observations,
        total_tokens=sum(r.total_tokens for r in all_results),
        total_cost_usd=sum(r.total_cost_usd for r in all_results),
        time_seconds=wall_seconds,
        success=baseline.success,
        error=baseline.error,
        benchmark=baseline.benchmark,
    )


# ───────────────────────── helpers ─────────────────────────


def _run_one_trajectory(
    *,
    task_dir: str,
    question: str,
    llm: LLMClient,
    config: dict,
    task_id: str,
    output_dir: str,
    benchmark: str,
    guidelines: str,
    session_id: str,
    traj_id: int,
    traj_temp: float,
) -> TaskResult | None:
    """Run one heavy trajectory inside a worker thread.

    Wrapped in try/except so a single trajectory crash doesn't kill the
    whole heavy mode invocation — the deliberator can work with whatever
    trajectories succeeded.
    """
    start = time.time()
    try:
        result = run_task(
            task_dir=task_dir,
            question=question,
            llm=llm,
            config=config,
            task_id=task_id,
            output_dir=output_dir,
            benchmark=benchmark,
            guidelines=guidelines,
            session_id=session_id,
        )
        elapsed = time.time() - start
        logger.info("Heavy traj %d T=%.2f done in %.1fs", traj_id, traj_temp, elapsed)
        return result
    except Exception as e:
        logger.warning("Heavy trajectory %d failed: %s", traj_id, e)
        return None


def _trajectory_output_dir(base_output_dir: str, traj_id: int) -> str:
    """Make a sibling output dir for an extra trajectory.

    The baseline writes to e.g. `results/run_x/task_42/` and additional
    trajectories write to `results/run_x/task_42__heavy_t1/`. Keeping
    these separate prevents prediction.csv clobbering and makes per-
    trajectory traces easy to inspect after the fact.
    """
    if not base_output_dir:
        return ""
    return f"{base_output_dir.rstrip('/')}__heavy_t{traj_id}"


def _to_heavy_trajectory(result: TaskResult, *, traj_id: int, temperature: float) -> HeavyTrajectory:
    """Convert a TaskResult into a HeavyTrajectory for the deliberator."""
    obs = result.observations or {}
    iters = obs.get("iterations", []) or []
    last = iters[-1] if iters else {}

    # Build a CSV string from the answer dict for the deliberator to read.
    answer_csv = _answer_dict_to_csv(result.answer)

    # Extract winning code from the last iteration (orchestrator stores it
    # under `winning_code` or as part of step record). Fall back to empty.
    final_code = ""
    final_code_lang = ""
    for st in reversed(result.steps or []):
        # StepRecord has `language` and `code` attributes
        if getattr(st, "language", None) and getattr(st, "code", None):
            final_code = st.code
            final_code_lang = st.language
            break
    if not final_code_lang:
        final_code_lang = obs.get("winning_language") or last.get("winning_language", "")

    judge = (last.get("judge") or {})
    flags = tuple(
        f.get("rule", "") for f in (last.get("harness_flags") or []) if f.get("rule")
    )
    stdout_tail = ""
    # last iteration's result_preview holds the stdout tail in trace_agent.json
    if "result_preview" in last:
        stdout_tail = str(last["result_preview"])[:2000]

    return HeavyTrajectory(
        traj_id=traj_id,
        temperature=temperature,
        final_answer_csv=answer_csv,
        final_code=final_code,
        final_code_lang=final_code_lang or "unknown",
        raw_stdout_tail=stdout_tail,
        judge_action=(judge.get("action") or "").lower(),
        judge_reasoning=(judge.get("reasoning") or "")[:500],
        harness_flags=flags,
        steps_executed=len(iters),
        success=result.success,
    )


def _answer_dict_to_csv(answer: dict) -> str:
    """Dict → CSV string for the deliberator. Empty dict → empty string."""
    if not answer:
        return ""
    try:
        # answer is {col: [vals]}; build DataFrame and serialize.
        df = pd.DataFrame(answer)
        return df.to_csv(index=False)
    except Exception:
        return ""


def _csv_to_answer_dict(csv_str: str) -> dict:
    """Parse deliberator's CSV output back to answer dict for save_prediction."""
    if not csv_str or not csv_str.strip():
        return {}
    try:
        df = pd.read_csv(StringIO(csv_str))
        return {c: df[c].tolist() for c in df.columns}
    except Exception:
        return {}


def _count_distinct_answers(trajs: list[HeavyTrajectory]) -> int:
    """Telemetry: how many trajectories produced distinct answers?"""
    answers = {t.final_answer_csv.strip() for t in trajs}
    return len(answers)


def _save_trajectory_artifacts(
    *,
    output_dir: str,
    baseline: TaskResult,
    additional_results: list[TaskResult],
    trajectories: list[HeavyTrajectory],
    decision: "HeavyDecision",
) -> None:
    """Write per-trajectory prediction CSV + a summary JSON to output_dir.

    The main prediction.csv is the deliberator's synthesized answer (written
    by main.py via save_prediction). Here we add:

      - heavy_baseline_prediction.csv   — baseline (traj_0) answer
      - heavy_t1_prediction.csv         — first extra trajectory's answer
      - heavy_t2_prediction.csv         — etc.
      - heavy_trajectories.json         — aggregated metadata for analysis

    Best-effort: failures here are logged but do not crash the pipeline.
    """
    if not output_dir:
        return
    import json as _json
    out_path = Path(output_dir)
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    # Per-trajectory CSV (baseline + each extra). Match traj_id order.
    all_task_results = [baseline, *additional_results]
    for ti, result in enumerate(all_task_results):
        name = "heavy_baseline_prediction.csv" if ti == 0 else f"heavy_t{ti}_prediction.csv"
        try:
            csv = _answer_dict_to_csv(result.answer)
            (out_path / name).write_text(csv, encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to write %s: %s", name, e)

    # Aggregated metadata
    summary = {
        "k_trajectories": len(trajectories),
        "trajectories": [
            {
                "traj_id": t.traj_id,
                "temperature": t.temperature,
                "final_code_lang": t.final_code_lang,
                "judge_action": t.judge_action,
                "harness_flags": list(t.harness_flags),
                "steps_executed": t.steps_executed,
                "success": t.success,
                "answer_preview": t.final_answer_csv[:500],
            }
            for t in trajectories
        ],
        "deliberator_matched_trajectory_id": decision.matched_trajectory_id,
        "deliberator_reasoning": decision.reasoning[:500],
        "deliberator_final_answer_preview": decision.final_answer_csv[:500],
    }
    try:
        (out_path / "heavy_trajectories.json").write_text(
            _json.dumps(summary, indent=2, default=str), encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to write heavy_trajectories.json: %s", e)
