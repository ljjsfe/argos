"""Adaptive voting for nondeterministic LLM-based data analysis.

API noise is a real measurement floor: same code, same task, different runs
yield different answers ±2-3 tasks. Voting recovers honest signal by sampling
the model's underlying belief — run a task N times, take the majority answer.

Cost-aware: trigger voting only when complexity or post-run uncertainty
signals indicate the answer is at risk. Simple tasks with confident single
runs bypass voting entirely.

Generic: complexity is derived from manifest (file count, knowledge.md,
multimodal, cross-source FK), not from any pre-labeled difficulty field.
"""

from .adaptive import (
    complexity_score,
    uncertainty_score,
    normalize_answer,
    majority_vote,
    adaptive_run,
)

__all__ = [
    "complexity_score",
    "uncertainty_score",
    "normalize_answer",
    "majority_vote",
    "adaptive_run",
]
