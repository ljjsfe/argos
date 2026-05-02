"""Playbook loader + retrieval.

Lightweight: a YAML file of entries, keyword-overlap retrieval. No vector
DB, no embeddings — explicitly chosen because the working set is < 100
entries and a richer retriever adds complexity without clear gain at
this scale.

Fail-soft: if the YAML is missing or malformed, return an empty tuple.
The orchestrator should always treat the playbook as advisory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path(__file__).parent / "data_analysis.yaml"
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "in", "on", "to", "and", "or",
    "is", "are", "was", "were", "with", "by", "from", "that", "this",
    "be", "been", "have", "has", "had", "do", "does", "did", "but",
    "what", "which", "who", "how", "many", "much", "any", "all",
    "list", "give", "show", "find",  # too generic in question text
})


@dataclass(frozen=True)
class PlaybookEntry:
    """Single curated pattern. Immutable."""
    id: str
    type: str           # "strategy" | "recovery" | "optimization"
    trigger: str        # observable condition
    action: str         # concrete agent behavior
    evidence: tuple[str, ...] = ()  # task_ids that motivated this
    added_date: str = ""

    def keywords(self) -> set[str]:
        """Tokens used for retrieval matching."""
        joined = f"{self.trigger} {self.action}"
        return _tokenize(joined)


def load_entries(path: str | Path | None = None) -> tuple[PlaybookEntry, ...]:
    """Read entries from YAML. Empty file or missing file → empty tuple."""
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return ()

    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML missing — playbook disabled")
        return ()

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        logger.warning("playbook YAML parse error: %s", e)
        return ()

    if not isinstance(raw, dict):
        return ()
    items = raw.get("entries") or []
    if not isinstance(items, list):
        return ()

    out: list[PlaybookEntry] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        try:
            out.append(PlaybookEntry(
                id=str(item.get("id") or f"pb_unknown_{i}"),
                type=str(item.get("type") or "strategy"),
                trigger=str(item.get("trigger") or ""),
                action=str(item.get("action") or ""),
                evidence=tuple(str(t) for t in (item.get("evidence") or ())),
                added_date=str(item.get("added_date") or ""),
            ))
        except (TypeError, ValueError) as e:
            logger.warning("skipping malformed playbook entry %d: %s", i, e)
            continue
    return tuple(out)


def retrieve_relevant(
    entries: tuple[PlaybookEntry, ...],
    question: str,
    schema_hint: str = "",
    k: int = 5,
) -> tuple[PlaybookEntry, ...]:
    """Pick top-k entries by keyword-overlap with the question + schema.

    Score is the size of the intersection between question/schema tokens
    and entry trigger+action tokens. Ties broken by entry order (newer
    entries first when they're appended at the end).
    """
    if not entries:
        return ()

    query_tokens = _tokenize(f"{question}\n{schema_hint}")
    if not query_tokens:
        return ()

    scored: list[tuple[int, int, PlaybookEntry]] = []
    for idx, e in enumerate(entries):
        score = len(query_tokens & e.keywords())
        if score > 0:
            scored.append((score, -idx, e))  # -idx → newer first on tie

    scored.sort(reverse=True)
    return tuple(e for _, _, e in scored[:k])


def format_for_prompt(entries: tuple[PlaybookEntry, ...]) -> str:
    """Render entries as a compact markdown block for prompt injection.

    Empty input → empty string (caller can skip the section entirely).
    """
    if not entries:
        return ""
    lines: list[str] = []
    for e in entries:
        lines.append(f"- [{e.type}] {e.trigger} → {e.action}")
    return "\n".join(lines)


def _tokenize(text: str) -> set[str]:
    """Lowercase tokens of length ≥ 3, stopwords removed."""
    return {
        t.lower() for t in _TOKEN_RE.findall(text)
        if len(t) >= 3 and t.lower() not in _STOPWORDS
    }
