"""Playbook: curated, human-authored data-analysis patterns.

A playbook entry is a (trigger, action) pair distilled by humans from
trace analysis or general data-engineering experience. Entries are
RETRIEVED at task start (top-N by relevance) and injected into the
PlannerCoder context as advisory guidance.

This is explicitly NOT auto-distilled by an LLM — see DECISIONS.md
ADR-002 for why curated > learned for our model strength (3B-active).

Public API:
- load_entries(path) → tuple[PlaybookEntry, ...]
- retrieve_relevant(entries, question, schema, k=5) → tuple[PlaybookEntry, ...]
- format_for_prompt(entries) → str  (renders to markdown injection block)
- record_use(entry_id, won: bool)   (tracker — fail-soft)
"""

from .loader import (
    PlaybookEntry,
    load_entries,
    retrieve_relevant,
    format_for_prompt,
)
from .tracker import record_use

__all__ = [
    "PlaybookEntry",
    "load_entries",
    "retrieve_relevant",
    "format_for_prompt",
    "record_use",
]
