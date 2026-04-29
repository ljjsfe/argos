"""Immutable data types for dataline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# --- Profiler types ---

@dataclass(frozen=True)
class ManifestEntry:
    """Single file's metadata from profiler."""
    file_path: str
    file_type: str  # csv | sqlite | json | markdown | pdf | docx | excel | image | parquet
    size_bytes: int
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CrossSourceRelation:
    """Auto-discovered relation between two data sources."""
    source_a: str
    source_b: str
    relation: str
    confidence: float


@dataclass(frozen=True)
class Manifest:
    """Complete profiling result for a task directory."""
    entries: tuple[ManifestEntry, ...]
    cross_source_relations: tuple[CrossSourceRelation, ...] = ()
    keyword_tags: tuple[str, ...] = ()


# --- Sandbox types ---

@dataclass(frozen=True)
class SandboxResult:
    """Result from executing code in sandbox."""
    stdout: str
    stderr: str
    return_code: int
    execution_time_ms: int
    step_id: str = ""
    structured_json: str = ""  # JSON-serialized step_result.json written by save_result()


# --- Agent types ---

@dataclass(frozen=True)
class PlanStep:
    """A single planned step from the planner."""
    step_description: str
    data_sources: tuple[str, ...] = ()
    depends_on_prior: bool = False
    expected_output: str = ""


@dataclass(frozen=True)
class StepRecord:
    """Record of a completed step (plan + code + result)."""
    plan: PlanStep
    code: str
    result: SandboxResult
    step_index: int = 0


@dataclass(frozen=True)
class JudgeDecision:
    """Judge's routing decision: action + guidance.

    The Judge identifies remaining red flags and routes to the appropriate
    action. No explicit sufficiency bool — "finish" action = sufficient.
    """
    action: str  # continue | backtrack | finish
    reasoning: str = ""
    missing: str = ""
    guidance_for_next_step: str = ""  # passed to Planner to steer next iteration
    truncate_to: int = 0
    quoted_answer: str = ""  # CoT: exact answer quoted from stdout before verdict


@dataclass(frozen=True)
class QuestionSpec:
    """Structured shape specification inferred from the question.

    Produced by QuestionAnalyzer (one LLM call before the loop).
    Consumed by HarnessGate (Rules 10-14) and PlannerCoder (guidance).
    Fail-open: unknown values cause QA rules to skip silently.
    """
    answer_type: str = "unknown"           # "scalar" | "list" | "table" | "unknown"
    expected_column_count: int = 0         # 0 = unknown
    expected_row_count: str = "unknown"    # "single" | "multiple" | "one_or_more" | "unknown"
    value_style: str = "unknown"           # "numeric" | "exact_term" | "name" | "mixed" | "unknown"
    tie_possible: bool = False             # True when min/max/lowest/highest — result may have ties
    computation_type: str = "unknown"      # "ratio" | "difference" | "count" | "aggregate" | "lookup" | "unknown"
    notes: str = ""                        # semantic interpretation (trace only, NOT injected into prompt)

    def to_guidance(self) -> str:
        """Structural hints for PlannerCoder. Excludes notes (may be wrong)."""
        parts: list[str] = []
        if self.answer_type != "unknown":
            parts.append(f"Expected answer shape: {self.answer_type}")
        # Column-count inference is deliberately weak for natural-language data
        # questions. Only expose the count for strict scalar computations where
        # it is high-confidence; otherwise downstream agents may merge/split
        # valid answer columns based on a regex guess.
        if (
            self.expected_column_count > 0
            and self.computation_type in {"count", "ratio"}
        ):
            parts.append(f"Expected column count: {self.expected_column_count}")
        if self.expected_row_count not in ("unknown", "single"):
            parts.append(f"Expected row count: {self.expected_row_count}")
        elif self.expected_row_count == "single" and not self.tie_possible:
            parts.append(f"Expected row count: single")
        if self.value_style != "unknown":
            parts.append(f"Value style: {self.value_style}")
        if self.tie_possible:
            parts.append(
                "Tie-possible: YES — use WHERE col = (SELECT MIN/MAX ...) or RANK(), "
                "NOT LIMIT 1. Multiple rows may share the extreme value."
            )
        if self.computation_type == "ratio":
            parts.append("Computation type: ratio (return a single numeric ratio/percentage)")
        return "\n".join(parts) if parts else ""


@dataclass(frozen=True)
class HarnessFlag:
    """A single deterministic verification flag from HarnessGate."""
    rule: str        # rule identifier (e.g., "agg_type", "empty_output")
    severity: str    # "block" | "warn"
    message: str     # human-readable description for guidance/Judge context


@dataclass(frozen=True)
class LLMUsage:
    """Token and cost tracking for a single LLM call."""
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    provider: str = ""
    model: str = ""


# --- Eval types ---

@dataclass(frozen=True)
class TaskScore:
    """Eval result for a single task."""
    task_id: str
    score: float  # 0.0–1.0: Recall − λ·(extra/predicted)
    difficulty: str = ""
    failure_category: str = ""  # code_error | wrong_direction | format_error | ...
    failed_at_agent: str = ""   # which agent failed
    error_type: str = ""
    error_detail: str = ""
    tokens_used: int = 0
    cost_usd: float = 0.0
    time_seconds: float = 0.0
    steps_executed: int = 0
    max_steps: int = 13
    suggestion: str = ""


# --- Memory / State types ---

@dataclass(frozen=True)
class AnalysisState:
    """Structured state passed between agents. Replaces raw list[StepRecord].

    Information layers (by priority):
    - Layer 1 (task definition): question, manifest_summary — never changes
    - Layer 2 (domain knowledge): domain_rules — extracted from manual/README, independent channel
    - Layer 3 (data understanding): data_profile_summary — column stats, distributions
    - Layer 4 (execution state): key_findings, completed_steps, variables — updated each step
    - Layer 5 (control signal): judge_guidance — steering from judge to planner
    - Layer 6 (raw detail): full_step_details — complete step records, disk-backed via pickles
    """
    task_id: str
    question: str
    manifest_summary: str                           # column names + types (compressed schema)
    data_profile_summary: str                       # analyzer output: stats, distributions
    domain_rules: str = ""                          # extracted from manual/README/knowledge files
    question_analysis: str = ""                     # pre-execution strategic analysis from QuestionAnalyzer
    key_findings: tuple[str, ...] = ()              # 1-line verified findings
    variables_in_scope: tuple[tuple[str, str], ...] = ()  # (pickle_name, description)
    judge_guidance: str = ""                        # steering instruction from judge for next iteration
    harness_feedback: str = ""                       # deterministic block/warn messages from HarnessGate
    task_mode: str = ""                              # deterministic routing hint (single_sql, multi_sql, python_extract, etc.)
    completed_steps: tuple[str, ...] = ()           # 1-line per step
    full_step_details: tuple[StepRecord, ...] = ()  # raw data for Finalizer + Judge


@dataclass(frozen=True)
class EvalReport:
    """Aggregate evaluation report with diagnostics."""
    overall_accuracy: float
    per_difficulty: dict[str, float] = field(default_factory=dict)
    task_scores: tuple[TaskScore, ...] = ()
    failure_breakdown: dict[str, int] = field(default_factory=dict)
    agent_bottlenecks: dict[str, int] = field(default_factory=dict)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompareReport:
    """Comparison between two eval runs."""
    accuracy_delta: float
    improved_tasks: tuple[str, ...] = ()
    regressed_tasks: tuple[str, ...] = ()
    per_difficulty_delta: dict[str, float] = field(default_factory=dict)
