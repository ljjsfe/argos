"""QuestionAnalyzer: infer answer shape before the main loop.

Two modes:
- analyze_deterministic(question) — regex/heuristic, zero LLM cost (default)
- analyze_llm(question, manifest, domain_rules, llm) — one LLM call (legacy)

Design principles:
- Fail-open: any ambiguity returns all-unknown spec, never blocks the pipeline.
- Conservative: only classify when high confidence. 6 known misclassification
  patterns from dry-run (task_199, task_25, task_27, task_80, task_86, task_173)
  informed the rule priority and safeguards.
- notes field is NOT injected into downstream prompts. Only structural hints
  (to_guidance()) are used.
"""

from __future__ import annotations

import logging
import re

from ..core.types import QuestionSpec

logger = logging.getLogger(__name__)

_UNKNOWN_SPEC = QuestionSpec()


# ---------------------------------------------------------------------------
# Deterministic inference (zero LLM cost)
# ---------------------------------------------------------------------------

# "list" patterns — checked BEFORE aggregation to avoid "list the total" → agg
_LIST_PATTERNS = [
    r"\blist\s+(?:all|the|out|their)\b",
    r"\bplease\s+list\b",
    r"\bwhat\s+are\s+the\s+\w+s\b",  # "what are the names/bonds/..."
]

# Count patterns — "how many", "the number of X" (not "his number")
_COUNT_PATTERNS = [
    r"\bhow\s+many\b",
    r"\bthe\s+(?:total\s+)?number\s+of\b",
    r"\bcount\s+(?:of|the)\b",
]

# Ratio/percentage patterns
_RATIO_PATTERNS = [
    r"\bhow\s+many\s+times\b",
    r"\bwhat\s+(?:is\s+the\s+)?percentage\b",
    r"\bcalculate\s+the\s+percentage\b",
    r"\bwhat\s+(?:is\s+the\s+)?(?:ratio|fraction|proportion)\b",
    r"\bhow\s+much\s+(?:faster|slower|more|less)\s+in\s+percentage\b",
    r"\bpercentage\s+of\b",
    r"\b(?:ratio|rate|percentage|percent|how\s+many\s+times)\b.*\bcompared\s+to\b",
]

# Aggregation patterns — sum/avg/min/max
_AGG_PATTERNS = [
    r"\b(?:what\s+is\s+the\s+)?(?:average|mean)\b",
    r"\b(?:what\s+is\s+the\s+)?(?:total|sum)\b",
]

# Superlative patterns — triggers tie_possible + aggregate
_SUPERLATIVE_PATTERNS = [
    r"\b(?:minimum|maximum|lowest|highest|most|least|fewest|"
    r"largest|smallest|best|worst|fastest|slowest|longest|shortest)\b",
]

# Top/bottom N patterns
_TOP_N_PATTERN = r"\b(?:top|bottom)\s+(\d+)\b"

# Grouping patterns — "for each", "per", "by"
_GROUP_PATTERNS = [
    r"\bfor\s+each\b",
    # "per X" — but skip "per unit" / "per year" etc when in a filter clause
    # ("paid more than X per unit") rather than output grouping ("X per region").
    r"\bper\s+(?!unit\b)(?!year\b)(?!month\b)(?!day\b)(?!hour\b)\w+\b",
    r"\bgroup(?:ed)?\s+by\b",
]

# Singular lookup — "what is the X of Y", "identify the", "provide the"
_SINGULAR_LOOKUP_PATTERNS = [
    r"\bwhat\s+is\s+(?:the\s+)?(?:name|surname|gender|colour|color|"
    r"telephone|phone|email|title|reference|id)\b",
    r"\bwho\s+is\b",
    r"\bidentify\s+the\b",
    r"\bprovide\s+the\b",
    r"\bstate\s+the\b",
    r"\bwrite\s+the\b",
    r"\bwhat(?:'s| is)\s+\w+(?:'s)?\s+(?:major|name|gender|role)\b",
]

# Multi-field detection: "X and Y" or "X, Y, and Z" in question
_MULTI_FIELD_PATTERN = r"\b(?:and\s+(?:the\s+)?(?:their|its|the|his|her))\b"


def _matches_any(text: str, patterns: list[str]) -> bool:
    """Return True if text matches any pattern (case-insensitive)."""
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _has_specific_entity(text: str) -> bool:
    """Detect if question references a specific entity (ID, name in quotes)."""
    return bool(
        re.search(r'(?:id|ID)\s*(?:=\s*|No\.?\s*|")\s*\w+', text)
        or re.search(r'"[^"]{3,}"', text)
        or re.search(r"'[^']{3,}'", text)
    )


def analyze_deterministic(question: str) -> QuestionSpec:
    """Infer structural shape from question text using regex heuristics.

    Zero LLM cost. Fail-open: returns all-unknown spec on any ambiguity.
    Conservative: designed to avoid the 6 known misclassification patterns.
    """
    if not question.strip():
        return _UNKNOWN_SPEC

    q = question.strip()

    # ── Check for list patterns FIRST (higher priority than aggregation) ──
    if _matches_any(q, _LIST_PATTERNS) and not _has_specific_entity(q):
        col_count = _estimate_column_count(q)
        return QuestionSpec(
            answer_type="list",
            expected_column_count=col_count,
            expected_row_count="multiple",
            value_style="unknown",
            computation_type="lookup",
        )

    # ── Top/bottom N ──
    top_match = re.search(_TOP_N_PATTERN, q, re.IGNORECASE)
    if top_match:
        col_count = _estimate_column_count(q)
        return QuestionSpec(
            answer_type="list",
            expected_column_count=col_count,
            expected_row_count="multiple",
            value_style="mixed",
            computation_type="aggregate",
        )

    # ── Grouping → table ──
    if _matches_any(q, _GROUP_PATTERNS):
        return QuestionSpec(
            answer_type="table",
            expected_row_count="multiple",
            value_style="mixed",
            computation_type="aggregate",
        )

    # ── Superlative (min/max/best/...) ──
    if _matches_any(q, _SUPERLATIVE_PATTERNS):
        col_count = _estimate_column_count(q)
        return QuestionSpec(
            answer_type="scalar",
            expected_column_count=col_count,
            expected_row_count="one_or_more",  # ties possible
            value_style="unknown",
            computation_type="aggregate",
            tie_possible=True,
        )

    # ── Ratio / percentage (before count: "how many times" is ratio) ──
    if _matches_any(q, _RATIO_PATTERNS):
        return QuestionSpec(
            answer_type="scalar",
            expected_column_count=1,
            expected_row_count="single",
            value_style="numeric",
            computation_type="ratio",
        )

    # ── Count patterns ──
    if _matches_any(q, _COUNT_PATTERNS):
        # Guard: "his number" / "the number" (attribute) vs "number of X" (count)
        if re.search(r"\b(?:his|her|its|the)\s+number\b", q, re.IGNORECASE):
            # Ambiguous — could be attribute, not count. Fail-open.
            pass
        else:
            return QuestionSpec(
                answer_type="scalar",
                expected_column_count=1,
                expected_row_count="single",
                value_style="numeric",
                computation_type="count",
            )

    # ── Aggregation (avg/sum/total) — only if no "list" keyword present ──
    if _matches_any(q, _AGG_PATTERNS) and not _matches_any(q, _LIST_PATTERNS):
        col_count = _estimate_column_count(q)
        return QuestionSpec(
            answer_type="scalar",
            expected_column_count=col_count,
            expected_row_count="single",
            value_style="numeric",
            computation_type="aggregate",
        )

    # ── Singular lookup — conservative, don't assume single row ──
    if _matches_any(q, _SINGULAR_LOOKUP_PATTERNS):
        col_count = _estimate_column_count(q)
        return QuestionSpec(
            answer_type="scalar",
            expected_column_count=col_count,
            expected_row_count="unknown",  # don't assume single
            value_style="name",
            computation_type="lookup",
        )

    # ── Tally = enumerate ──
    if re.search(r"\btally\b", q, re.IGNORECASE):
        return QuestionSpec(
            answer_type="list",
            expected_row_count="multiple",
            value_style="unknown",
            computation_type="lookup",
        )

    # ── "Which X" filter-lookup — entity selection with a filter condition ──
    # Example: "Which race was Alex Yoong in when he was in track number < 20?"
    # The answer is the entity name only (1 column), not the filter value.
    # Must be last before UNKNOWN to avoid over-riding other patterns.
    if re.search(r"^\s*which\s+\w+", q, re.IGNORECASE) and not _matches_any(q, _GROUP_PATTERNS):
        col_count = _estimate_column_count(q)
        return QuestionSpec(
            answer_type="list",
            expected_column_count=col_count,  # typically 1 from _estimate_column_count
            expected_row_count="one_or_more",
            value_style="name",
            computation_type="lookup",
        )

    # ── "Give/Provide their X status" — single-attribute lookup mid-sentence
    # Catches questions like task_180 where the verb appears after a filter clause:
    # "For all people who paid more than X. Give their consumption status."
    # Falls through here when no other pattern matches; uses _estimate_column_count
    # (which now handles "List/Give/..." patterns) to detect 1-col output.
    col_count_estimate = _estimate_column_count(q)
    if col_count_estimate == 1:
        return QuestionSpec(
            answer_type="list",
            expected_column_count=1,
            expected_row_count="multiple",
            value_style="unknown",
            computation_type="lookup",
        )

    return _UNKNOWN_SPEC


def _estimate_column_count(question: str) -> int:
    """Estimate expected column count from question structure.

    Returns 0 (unknown) if uncertain, never over-counts.
    Conservative: only returns non-zero when high confidence.
    """
    q = question.strip()

    # "and" only implies multiple output columns when it appears in an
    # output-intent phrase. In relative/filter clauses ("bonds that have
    # phosphorus and nitrogen"), "and" describes conditions, not answer fields.
    filter_and = re.search(
        r"\b(?:that|which|who|where|having)\b[^?]*\band\b",
        q,
        re.IGNORECASE,
    )
    output_and = re.search(
        r"\b(?:list|show|provide|state|give|return|include|write)\b"
        r"[^?]*\band\b",
        q,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:names?|ids?|types?|funding\s+types?|values?|dates?|costs?|amounts?)\b"
        r"\s+and\s+(?:the\s+)?"
        r"(?:names?|ids?|types?|funding\s+types?|values?|dates?|costs?|amounts?)\b",
        q,
        re.IGNORECASE,
    ) or re.search(
        # "what is the X and the Y" — asking for multiple output values
        r"\bwhat\s+is\b[^?]*\band\s+(?:the\s+)?(?:average|total|number|"
        r"percentage|count|sum|ratio|mean|cost|amount|value|score|age|name)\b",
        q,
        re.IGNORECASE,
    ) or re.search(
        # "identify X and their/its/the Y" — possessive 'their/its' or the
        # determiner 'the' before the second noun strongly signals two output
        # fields, distinct from "identify X with A and B" (filter).
        # Discovered via task_163 data audit (2026-05-15): the question
        # "Identify the type of expenses and their total value approved..."
        # expects 2 columns (type + total) but was being inferred as 1-col
        # scalar, causing qa_column_count to block the correct 2-col answer.
        r"\bidentify\b[^?]*\band\s+(?:their|its|the)\b",
        q,
        re.IGNORECASE,
    )
    if output_and and not filter_and:
        conjunctions = re.findall(r"\band\b", q, re.IGNORECASE)
        return min(1 + len(conjunctions), 3)
    if filter_and:
        return 0

    # "what is the X" / "what is X's Y" → single attribute → 1 column
    # But NOT "what are the X" (plural → could be list with multiple cols)
    # Guard: skip if output_and matched (multi-output "what is X and Y")
    if not output_and and re.search(
        r"\bwhat\s+is\s+(?:the\s+)?(?:comment|name|title|value|score|"
        r"answer|result|amount|total|average|percentage|number|count|"
        r"ratio|rate|date|year|month|day|time|age|price|cost|"
        r"salary|revenue|profit|weight|height|length|distance|"
        r"duration|speed|temperature|population|area|volume)\b",
        question, re.IGNORECASE,
    ):
        return 1

    # "Which X ..." at question start → asking for a single entity → 1 column.
    # Guard: skip when output_and matched (e.g. "which race and which driver").
    # This handles task_86 ("Which race was Alex Yoong in...") and task_25
    # ("Which event has the lowest cost?") where the answer is entity name only.
    if not output_and and re.search(r"^\s*which\s+\w+", q, re.IGNORECASE):
        return 1

    # "List/Give/Identify/State the X" without "and" in output context
    # → single attribute output. Captures task_38 ("List all the withdrawals")
    # and task_180 ("Give their consumption status") where gold returns 1 col.
    # Allows the verb to appear mid-sentence (after a leading filter clause).
    # Skip if the trailing phrase mentions "and" near a noun (multi-col intent).
    if not output_and and re.search(
        r"\b(?:list|give|identify|state|provide)\s+"
        r"(?:all\s+|the\s+|their\s+|out\s+)*(?:\w+\s+){0,3}(?:of|in|with|that|when|for|whose|whom|where|to|on|by|status|name|id|date|count|cost|amount|value|type|total)\b",
        q, re.IGNORECASE,
    ):
        return 1

    return 0


