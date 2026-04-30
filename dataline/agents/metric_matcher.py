"""Extract metric definitions from knowledge.md and match against question.

Many domain knowledge documents define metrics as:
  - **Metric Name**: `SQL formula` - description
  - **Metric**: Name (followed by) - **SQL**: `query`
  - **Metric Name**: <LaTeX block>

When the agent's question references a metric (by name keywords),
inject the EXACT formula at high priority so the agent doesn't invent
its own. Pure deterministic regex — no domain coupling, no LLM calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDefinition:
    """A metric extracted from domain documentation."""
    name: str            # canonical metric name as documented
    formula: str         # raw formula expression (SQL or math)
    source_line: str     # original line for context


# Pattern 1: `- **Metric Name**: \`formula\` - description`
_BULLET_BACKTICK = re.compile(
    r"^[\-*]\s*\*\*([^*]+?)\*\*\s*[:.\-]\s*`([^`]+)`",
    re.MULTILINE,
)

# Pattern 2: `- **Metric**: Name` followed shortly by `- **SQL**: \`query\``
_METRIC_SQL_PAIR = re.compile(
    r"\*\*Metric\*\*\s*:\s*([^\n]+?)\s*\n[\s\-*]*\*\*SQL\*\*\s*:\s*`([^`]+)`",
    re.MULTILINE,
)

# Pattern 3: `- **Metric Name**:\n  \[ ... = ... \]` (LaTeX block)
_LATEX_FORMULA = re.compile(
    r"\*\*([^*]+?)\*\*\s*:\s*\n\s*\\\[(.+?)\\\]",
    re.DOTALL,
)


def extract_metrics(text: str) -> list[MetricDefinition]:
    """Pull all metric-definition patterns out of a domain document.

    Returns an ordered list of MetricDefinition. Duplicates collapsed
    by metric name (later occurrences ignored).
    """
    if not text:
        return []

    seen: set[str] = set()
    metrics: list[MetricDefinition] = []

    def _add(name: str, formula: str, line: str) -> None:
        n = name.strip()
        f = formula.strip()
        if not n or not f or n.lower() in seen:
            return
        seen.add(n.lower())
        metrics.append(MetricDefinition(name=n, formula=f, source_line=line.strip()))

    # Pattern 1: Bullet metric with backtick-quoted formula
    for m in _BULLET_BACKTICK.finditer(text):
        name, formula = m.group(1), m.group(2)
        if any(kw in formula.upper() for kw in ("SUM", "AVG", "COUNT", "MIN", "MAX", "DIVIDE", "SUBTRACT", "RANK")):
            _add(name, formula, m.group(0))

    # Pattern 2: Metric+SQL paired bullets
    for m in _METRIC_SQL_PAIR.finditer(text):
        name, formula = m.group(1), m.group(2)
        _add(name, formula, m.group(0))

    # Pattern 3: LaTeX block formula
    for m in _LATEX_FORMULA.finditer(text):
        name, formula = m.group(1), m.group(2)
        # Strip LaTeX cosmetics
        cleaned = re.sub(r"\\text\{([^}]+)\}", r"\1", formula).strip()
        _add(name, cleaned, m.group(0))

    return metrics


_TOKENIZE = re.compile(r"[A-Za-z]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens, length>=3, ignore stopwords."""
    stop = {"the", "and", "for", "with", "what", "how", "is", "are",
            "was", "were", "of", "in", "on", "by", "to", "at", "an", "a"}
    return {w.lower() for w in _TOKENIZE.findall(text) if len(w) >= 3 and w.lower() not in stop}


def match_question_to_metrics(
    question: str,
    metrics: list[MetricDefinition],
    min_overlap: int = 1,
) -> list[MetricDefinition]:
    """Return metrics whose name has token overlap with the question.

    Conservative: requires at least `min_overlap` content-word overlap.
    Sorted by overlap count (most relevant first).
    """
    q_tokens = _tokenize(question)
    if not q_tokens:
        return []

    scored: list[tuple[int, MetricDefinition]] = []
    for m in metrics:
        m_tokens = _tokenize(m.name)
        if not m_tokens:
            continue
        overlap = len(q_tokens & m_tokens)
        if overlap >= min_overlap:
            scored.append((overlap, m))

    scored.sort(key=lambda x: -x[0])
    return [m for _, m in scored]


def build_relevant_metrics_block(question: str, knowledge_text: str) -> str:
    """Top-level convenience: extract + match + format.

    Returns a markdown-rendered block ready for prompt injection,
    or empty string when no relevant metric found.
    """
    metrics = extract_metrics(knowledge_text)
    relevant = match_question_to_metrics(question, metrics)
    if not relevant:
        return ""

    lines = ["The question references the following metric(s) defined in domain rules. "
             "Use the exact formula — do not invent your own:"]
    for m in relevant[:5]:  # cap at top-5 to avoid prompt bloat
        lines.append(f"- **{m.name}**: `{m.formula}`")
    return "\n".join(lines)


def build_metric_index(knowledge_text: str) -> str:
    """Produce a compact, ordered table of all metric definitions.

    Question-agnostic: extracts every metric/formula in the document and
    formats as a top-of-prompt index. Helps the agent locate the right
    formula by name without parsing the full document.

    Returns empty string when no metrics are present.
    """
    metrics = extract_metrics(knowledge_text)
    if not metrics:
        return ""
    lines = [
        "## Domain Metric Index (formulas defined in knowledge.md)",
        "When the question matches a metric below by name, use its formula VERBATIM.",
        "",
    ]
    for m in metrics:
        lines.append(f"- **{m.name}**: `{m.formula}`")
    return "\n".join(lines)
