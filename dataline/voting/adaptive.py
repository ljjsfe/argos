"""Adaptive voting: complexity-gated + uncertainty-triggered N-run majority vote.

Two-tier policy without difficulty labels (works on any unseen dataset):

Tier 1 — pre-run complexity (deterministic, zero LLM cost):
    Compute from manifest alone (file count, knowledge.md presence,
    cross-source FK, narrative doc, multimodal).
    Score >= threshold → vote N times immediately.

Tier 2 — post-run uncertainty (observable, zero extra cost):
    For tasks below complexity threshold, run once and inspect signals
    (iteration count, repeated Judge continues, HarnessGate WARN density).
    High uncertainty → top up to N runs and vote.

Both tiers feed into majority_vote() which canonicalizes answers (sorted
columns + rows) for unordered comparison and tie-breaks on lowest
iteration count.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable


def complexity_score(manifest: Any, has_knowledge_doc: bool = False) -> int:
    """Tier 1 complexity score from manifest only (0-6).

    Signals are DIFFERENTIATING (skipped when uniform across the dataset):

    - +1 if 4+ structured sources (high join surface area)
    - +1 if 6+ cross-source relations (dense FK web → row-explosion risk)
    - +2 if narrative doc present (markdown >10KB excluding knowledge.md, or any pdf/docx)
    - +2 if multimodal (image)
    - +1 if knowledge.md is "large" (>10KB → likely many definitions/formulas)

    NOTE: bare presence of knowledge.md is NOT counted — it's near-universal
    on KDD-style benchmarks and would saturate the signal. Size threshold
    keeps it differentiating.
    """
    score = 0

    structured = sum(
        1 for e in manifest.entries
        if e.file_type in ("csv", "json", "sqlite", "parquet", "excel")
    )
    if structured >= 4:
        score += 1

    if len(manifest.cross_source_relations) >= 6:
        score += 1

    # Narrative document: distinguish from knowledge.md by name + size threshold
    has_narrative = any(
        (
            e.file_type == "markdown"
            and "knowledge" not in e.file_path.lower()
            and e.size_bytes > 10_000
        )
        or e.file_type in ("pdf", "docx")
        for e in manifest.entries
    )
    if has_narrative:
        score += 2

    if any(e.file_type == "image" for e in manifest.entries):
        score += 2

    # Large knowledge.md = many definitions/metrics = high mapping risk
    knowledge_size = max(
        (e.size_bytes for e in manifest.entries
         if "knowledge" in e.file_path.lower()),
        default=0,
    )
    if knowledge_size >= 10_000:
        score += 1

    return score


def uncertainty_score(result: Any) -> int:
    """Tier 2 uncertainty score from a single run's trace (0-3).

    Signals:
    - Used 3+ steps (multiple iteration cycles to converge)
    - Judge issued "continue" 2+ times (repeatedly unhappy with answer)
    - HarnessGate produced 2+ WARN flags across iterations

    Each contributes +1. High uncertainty → vote with more runs.
    """
    score = 0

    steps = getattr(result, "steps", None) or ()
    if len(steps) >= 3:
        score += 1

    trace = getattr(result, "trace", None) or ()
    judge_continues = sum(
        1 for e in trace
        if isinstance(e, dict)
        and e.get("agent") == "judge"
        and "continue" in str(e.get("message", "")).lower()
    )
    if judge_continues >= 2:
        score += 1

    warn_count = sum(
        1 for e in trace
        if isinstance(e, dict)
        and e.get("agent") == "harness_gate"
        and ":warn]" in str(e.get("message", ""))
    )
    if warn_count >= 2:
        score += 1

    return score


def normalize_answer(answer: Any) -> tuple:
    """Canonical form for unordered comparison.

    For dict-of-lists (the standard answer shape), sort each list and
    sort by column name so {col1:[a,b]} == {col1:[b,a]} for voting.
    """
    if isinstance(answer, dict):
        canon: list[tuple[str, Any]] = []
        for k, v in sorted(answer.items(), key=lambda kv: str(kv[0])):
            if isinstance(v, list):
                canon.append((str(k), tuple(sorted(str(x) for x in v))))
            else:
                canon.append((str(k), str(v)))
        return tuple(canon)
    if isinstance(answer, list):
        return tuple(sorted(str(x) for x in answer))
    return (str(answer),)


def majority_vote(results: list[Any]) -> Any:
    """Return the result whose answer matches the majority canonical form.

    On tie, prefer the result with the fewest steps (most confident path).
    """
    if not results:
        return None
    if len(results) == 1:
        return results[0]

    canonical = [normalize_answer(getattr(r, "answer", {})) for r in results]
    counts = Counter(canonical)
    top_canon, top_count = counts.most_common(1)[0]

    matching = [r for r, c in zip(results, canonical) if c == top_canon]
    return min(
        matching,
        key=lambda r: len(getattr(r, "steps", ()) or ()),
    )


def adaptive_run(
    run_fn: Callable[[], Any],
    manifest: Any,
    *,
    has_knowledge_doc: bool = False,
    complexity_threshold: int = 2,
    uncertainty_threshold: int = 2,
    max_votes: int = 3,
) -> tuple[Any, list[Any], str]:
    """Execute task with adaptive voting policy.

    Returns (selected_result, all_results, trigger_reason).
    trigger_reason ∈ {"complexity_triggered", "uncertainty_triggered", "single_run"}.

    Caller passes a 0-arg `run_fn` that performs ONE complete task run and
    returns a TaskResult-like object with .answer / .steps / .trace.
    """
    cscore = complexity_score(manifest, has_knowledge_doc=has_knowledge_doc)

    if cscore >= complexity_threshold:
        results = [run_fn() for _ in range(max_votes)]
        return majority_vote(results), results, "complexity_triggered"

    first = run_fn()
    uscore = uncertainty_score(first)
    if uscore >= uncertainty_threshold:
        more = [run_fn() for _ in range(max_votes - 1)]
        all_results = [first] + more
        return majority_vote(all_results), all_results, "uncertainty_triggered"

    return first, [first], "single_run"
