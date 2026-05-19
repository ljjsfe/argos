"""Heavy deliberator agent: synthesize K independent trajectories into one answer.

After the orchestrator runs K independent trajectories (Phase 1 baseline +
K-1 high-temp), this agent reads the serialized memory cache and decides
the final answer. Reference: arXiv 2605.02396 (HeavySkill paper).

Design rules from the paper:
  - Deliberator does NOT vote. It critically evaluates each trajectory.
  - It may pick a minority answer if its reasoning is rigorous.
  - It may re-derive the answer if all K are wrong.
  - Shuffle trajectory order to remove position bias.
  - Single deliberation (no iterative — paper §4.3 warns HM up / HP down).
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from ..core.llm_client import LLMClient
from ..core.types import HeavyDecision, HeavyTrajectory


_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "heavy_deliberator.md"

# Maximum chars per trajectory section. The deliberator's prompt grows linearly
# with K * per-trajectory size, so we cap each piece to keep total prompt
# tractable for the small (3B active) Qwen model.
_MAX_CODE_CHARS = 1500
_MAX_STDOUT_CHARS = 1200
_MAX_ANSWER_CHARS = 1500
_MAX_REASONING_CHARS = 500


def _normalize_csv(text: str) -> str:
    """Canonical form for comparing two CSV answers.

    Mirrors the official KDD scorer behaviour: column NAMES are ignored,
    only the per-column data value sets matter. We canonicalise by:
      1. drop the header row
      2. sort data rows
      3. lower-case + strip cells; numeric cells round to 2dp
      4. include the column count so a 1-col answer can't accidentally
         match a 2-col one with the same values

    Empty answer → "" (cannot win the majority).
    """
    if not text or not text.strip():
        return ""
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    # Need at least header + 1 data row, OR a single header-only line is
    # treated as no-data.
    if len(lines) < 2:
        return ""
    data_rows = lines[1:]
    if not data_rows:
        return ""
    # Determine column count from the first data row (consistent across CSV).
    n_cols = len(data_rows[0].split(","))
    norm_rows: list[str] = []
    for row in data_rows:
        cells = []
        for cell in row.split(","):
            c = cell.strip().lower()
            try:
                cells.append(f"{round(float(c), 2):.2f}")
            except ValueError:
                cells.append(c)
        norm_rows.append(",".join(cells))
    norm_rows.sort()
    return f"cols={n_cols}\n" + "\n".join(norm_rows)


def _majority_vote(trajectories: list[HeavyTrajectory]) -> "HeavyTrajectory | None":
    """If a strict majority of trajectories share the same normalized answer,
    return one of them. Otherwise return None (fall through to LLM).

    Strict majority = > K/2 (so for K=3 this is ≥2). Empty-answer signatures
    are skipped — only non-empty candidates can win.

    This is a UNIVERSAL deterministic safeguard: when 2+ independent
    trajectories converge on the same answer, that signal is stronger than
    any single-LLM-call deliberation. Catches the deliberator-prior-override
    failure observed on task_415 (Brawn vs McLaren) etc.
    """
    if not trajectories:
        return None
    sig_to_trajs: dict[str, list[HeavyTrajectory]] = {}
    for t in trajectories:
        sig = _normalize_csv(t.final_answer_csv)
        if not sig:
            continue
        sig_to_trajs.setdefault(sig, []).append(t)
    threshold = len(trajectories) // 2 + 1  # strict majority
    for sig, ts in sig_to_trajs.items():
        if len(ts) >= threshold:
            # Prefer the lowest temperature among matching trajectories
            # (typically the baseline) for stability.
            return min(ts, key=lambda t: (t.temperature, t.traj_id))
    return None


def deliberate(
    question: str,
    trajectories: list[HeavyTrajectory],
    llm: LLMClient,
    *,
    domain_rules: str = "",
    prompt_path: Path | None = None,
    seed: int | None = None,
) -> HeavyDecision:
    """Synthesize K trajectories into one final answer.

    Args:
      question: the original task question.
      trajectories: K independent trajectory summaries.
      llm: LLM client (temperature handled by client, deliberator runs at
           the same temperature as caller — typically low for synthesis).
      domain_rules: optional domain knowledge to include in prompt.
      prompt_path: override the default prompt template.
      seed: optional seed for shuffling trajectory order (None = random).

    Returns:
      HeavyDecision with final_answer_csv, reasoning, matched_trajectory_id.

    If parsing fails or the deliberator output is unusable, falls back to
    the trajectory whose Finalizer produced the longest non-empty CSV —
    a "least bad" choice. The caller can detect this via matched_trajectory_id
    being set when reasoning includes "fallback".
    """
    if not trajectories:
        return HeavyDecision(
            final_answer_csv="",
            reasoning="no trajectories provided",
            matched_trajectory_id=-1,
            trajectories_seen=0,
        )

    # ── Deterministic safeguard: strict-majority vote ─────────────────────
    # When ≥ ⌊K/2⌋+1 trajectories converge on the same normalized answer,
    # trust that consensus instead of calling the LLM deliberator. Catches
    # the deliberator-prior-override failure mode where an LLM call can
    # invent training-data "knowledge" that contradicts what the data
    # actually shows (e.g. inventing a sports-history fact that overrides
    # 2/3 correct trajectories).
    majority = _majority_vote(trajectories)
    if majority is not None:
        return HeavyDecision(
            final_answer_csv=majority.final_answer_csv,
            reasoning=(
                f"majority_vote: ≥{len(trajectories)//2 + 1}/{len(trajectories)} "
                f"trajectories converged on traj_id={majority.traj_id}'s answer; "
                f"deterministic agreement trusted over LLM deliberation."
            ),
            matched_trajectory_id=majority.traj_id,
            trajectories_seen=len(trajectories),
        )

    template = (prompt_path or _DEFAULT_PROMPT).read_text(encoding="utf-8")

    # Shuffle to remove position bias (paper §2.2).
    shuffled = list(trajectories)
    rng = random.Random(seed)
    rng.shuffle(shuffled)

    traj_block = _format_trajectories(shuffled)
    system_prompt = (
        template
        .replace("{question}", question)
        .replace("{K}", str(len(trajectories)))
        .replace("{domain_rules}", domain_rules.strip() or "(none provided)")
        .replace("{trajectories}", traj_block)
    )

    user_prompt = (
        "Read the K trajectories above. Decide the final answer per the "
        "five steps. Output the JSON object now."
    )

    response = llm.chat(system_prompt, user_prompt)

    parsed = _parse_response(response)
    if parsed is None:
        # Fallback: pick the trajectory with the most informative non-empty answer.
        best = max(
            trajectories,
            key=lambda t: (
                # Prefer Phase-1 baseline (traj_id=0) on tie, then longest answer.
                len(t.final_answer_csv.strip()),
                -t.traj_id,
            ),
        )
        return HeavyDecision(
            final_answer_csv=best.final_answer_csv,
            reasoning=f"parse_fallback: kept trajectory {best.traj_id}",
            matched_trajectory_id=best.traj_id,
            trajectories_seen=len(trajectories),
        )

    final_csv = (parsed.get("final_answer_csv") or "").strip()
    matched_id = parsed.get("matched_trajectory_id", -1)
    reasoning = (parsed.get("reasoning") or "")[:1000]

    # Sanity check: if deliberator returned empty CSV, fall back to longest
    # trajectory answer rather than ship empty.
    if not final_csv:
        best = max(trajectories, key=lambda t: len(t.final_answer_csv.strip()))
        return HeavyDecision(
            final_answer_csv=best.final_answer_csv,
            reasoning=f"empty_output_fallback: kept trajectory {best.traj_id} ({reasoning[:120]})",
            matched_trajectory_id=best.traj_id,
            trajectories_seen=len(trajectories),
        )

    return HeavyDecision(
        final_answer_csv=final_csv,
        reasoning=reasoning,
        matched_trajectory_id=int(matched_id) if isinstance(matched_id, int) else -1,
        trajectories_seen=len(trajectories),
    )


def _format_trajectories(trajs: list[HeavyTrajectory]) -> str:
    """Serialize trajectories into the deliberator's input block.

    Each block contains code, stdout tail, final answer, judge action, and
    harness warnings — exactly what the deliberator needs to critique.
    """
    parts: list[str] = []
    for i, t in enumerate(trajs, start=1):
        flags = ", ".join(t.harness_flags) if t.harness_flags else "(none)"
        reasoning = (t.judge_reasoning or "")[:_MAX_REASONING_CHARS]
        parts.append(
            f"### Trajectory #{i}  (traj_id={t.traj_id}, T={t.temperature})\n"
            f"\n"
            f"**code ({t.final_code_lang}):**\n"
            f"```\n{t.final_code[:_MAX_CODE_CHARS]}\n```\n"
            f"\n"
            f"**stdout tail:**\n"
            f"```\n{t.raw_stdout_tail[:_MAX_STDOUT_CHARS]}\n```\n"
            f"\n"
            f"**final_answer:**\n"
            f"```\n{t.final_answer_csv[:_MAX_ANSWER_CHARS]}\n```\n"
            f"\n"
            f"- judge action: `{t.judge_action or 'n/a'}`\n"
            f"- judge reasoning: {reasoning or '(none)'}\n"
            f"- harness warnings: {flags}\n"
            f"- steps executed: {t.steps_executed}, success: {t.success}\n"
        )
    return "\n---\n".join(parts)


# Look for {...} possibly inside ```json fences. Greedy outer braces to
# capture nested structure.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)


def _parse_response(text: str) -> dict | None:
    """Extract JSON from the LLM response. Tolerant of ```json fences and
    surrounding prose. Returns None on irrecoverable parse failure.
    """
    if not text or not text.strip():
        return None

    # Try fenced JSON first (most common with chat models)
    m = _JSON_FENCE_RE.search(text)
    candidates = []
    if m:
        candidates.append(m.group(1))

    # Fall back to bare JSON object
    m2 = _BARE_JSON_RE.search(text)
    if m2:
        candidates.append(m2.group(0))

    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None
