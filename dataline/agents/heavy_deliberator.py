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
