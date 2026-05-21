"""Evidence Ledger — typed pipeline for hint emitters.

Design reference: docs/EVIDENCE_LEDGER_DESIGN.md

Unifies the prompt-augmenting hint emitters (A1 focus_hints, A2
term_binding, B2 doc_glossary, Block 4 narrative virtual tables, and
any future Tianfu-style tool) behind a single typed Evidence record
held in a per-task EvidenceLedger.

Phase 1 (this file's scope):
  - Add types + ledger collector.
  - Orchestrator dual-writes: legacy state.* fields AS WELL AS the
    ledger so the new path can be enabled later without breaking
    anything.
  - PlannerCoder + ContextManager read from ledger only when
    DATALINE_EVIDENCE_LEDGER=1 env var is set; otherwise legacy
    state-field path is used unchanged.
  - Default OFF → production prompts byte-identical to v99/d846e07.

Phase 2 (separate commit): default ON after smoke verification.
Phase 3 (separate commit): drop legacy state.* fields.

Risk class: architecture / dual-write. Same family as the 2026-05-20
FilteredTaskView refactor (worked). Does NOT change LLM behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .context_manager import Section


# Canonical priority scale — keep aligned with §5 of the design doc.
# Higher = preserved longer under ContextManager budget pressure.
# Do not introduce new emitters without picking a slot here.
CANONICAL_PRIORITIES = {
    "narrative_tables": 90,
    "doc_glossary": 80,
    "domain_bindings": 72,
    "focus_hints": 70,
}


# Risk classes — see CLAUDE.md "Rule-design discipline" section 3.
# additive   = adds prompt content; LLM may use or ignore. Safe.
# subtractive = removes/overrides existing content; can shift Planner
#              reasoning paths (v94 / G2 lesson). Currently unused but
#              reserved for future use (e.g. context policy that
#              actively suppresses an existing emitter).
_VALID_RISK_CLASSES = frozenset({"additive", "subtractive"})


@dataclass(frozen=True)
class Evidence:
    """One piece of evidence emitted by a hint source.

    Held by EvidenceLedger; rendered to a ContextManager.Section on
    demand. Empty payloads are dropped at ingest (avoids clutter).
    """
    source: str
    payload: str
    priority: int = 50
    confidence: float = 1.0
    cost_tokens: int = 0
    risk_class: str = "additive"
    trace_tag: str = ""
    section_name: str = ""    # defaults to source if empty
    section_heading: str = ""
    compressible: bool = True

    def __post_init__(self):
        # Validate at construction (frozen dataclass — use object.__setattr__ if we needed to normalize, but we just assert)
        if not self.source:
            raise ValueError("Evidence.source must be non-empty")
        if not 0 <= self.priority <= 100:
            raise ValueError(
                f"Evidence.priority must be 0-100, got {self.priority}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Evidence.confidence must be 0.0-1.0, got {self.confidence}"
            )
        if self.risk_class not in _VALID_RISK_CLASSES:
            raise ValueError(
                f"Evidence.risk_class must be one of {sorted(_VALID_RISK_CLASSES)}, "
                f"got {self.risk_class!r}"
            )

    def to_section(self) -> Section:
        """Render this evidence as a ContextManager Section."""
        return Section(
            name=self.section_name or self.source,
            content=self.payload,
            priority=self.priority,
            compressible=self.compressible,
            heading=self.section_heading,
        )


def _env_disable_flag(source: str) -> str:
    """Return the env-var name that disables a specific source.

    `DATALINE_DISABLE_<SOURCE_UPPER>` — e.g. DATALINE_DISABLE_A2
    already exists for domain_bindings (back-compat). New emitters
    follow the same convention.
    """
    return f"DATALINE_DISABLE_{source.upper()}"


# Back-compat: keep the historical A2 env name working even though
# the new canonical name is DATALINE_DISABLE_DOMAIN_BINDINGS.
_LEGACY_DISABLE_ENV_ALIASES = {
    "domain_bindings": ("DATALINE_DISABLE_A2",),
}


class EvidenceLedger:
    """Per-task collection of Evidence records.

    Workflow:
      1. Orchestrator constructs an empty ledger at task start.
      2. Each emitter pushes via `ledger.add(Evidence(...))`. Empty
         payloads silently dropped. Disabled sources silently dropped
         (per `DATALINE_DISABLE_<SOURCE>=1` env var).
      3. PlannerCoder pulls Sections via `ledger.to_sections()` when
         the global feature flag DATALINE_EVIDENCE_LEDGER=1 is set;
         otherwise reads legacy state fields.
      4. Trace summary via `ledger.summary()` → obs["evidence_ledger"].
    """

    def __init__(self) -> None:
        self._items: list[Evidence] = []
        self._dropped: list[tuple[str, str]] = []  # (source, reason) for telemetry

    def add(self, ev: Evidence) -> bool:
        """Append evidence. Returns True if accepted, False if dropped.

        Drop conditions (silent — recorded in _dropped for telemetry):
          - Empty payload after strip
          - Source-specific disable env var set
        """
        if not ev.payload or not ev.payload.strip():
            self._dropped.append((ev.source, "empty_payload"))
            return False
        if self.is_disabled(ev.source):
            self._dropped.append((ev.source, "env_disabled"))
            return False
        self._items.append(ev)
        return True

    def get(self, source: str) -> list[Evidence]:
        """Return all evidence from a given source (insertion-order)."""
        return [e for e in self._items if e.source == source]

    def all(self) -> list[Evidence]:
        """All evidence in insertion order."""
        return list(self._items)

    def to_sections(self) -> list[Section]:
        """Render all evidence as Sections, sorted by priority desc.

        Ordering: priority desc, then insertion order (stable). This
        gives ContextManager a deterministic compression order.
        """
        ranked = sorted(
            enumerate(self._items),
            key=lambda iv: (-iv[1].priority, iv[0]),
        )
        return [ev.to_section() for _, ev in ranked]

    def summary(self) -> dict[str, Any]:
        """Trace-friendly summary. Goes into obs['evidence_ledger']."""
        by_source: dict[str, dict[str, Any]] = {}
        for ev in self._items:
            agg = by_source.setdefault(ev.source, {
                "count": 0, "total_chars": 0,
                "max_priority": 0, "total_cost_tokens": 0,
            })
            agg["count"] += 1
            agg["total_chars"] += len(ev.payload)
            agg["max_priority"] = max(agg["max_priority"], ev.priority)
            agg["total_cost_tokens"] += ev.cost_tokens
        return {
            "n_items": len(self._items),
            "by_source": by_source,
            "n_dropped": len(self._dropped),
            "dropped": list(self._dropped),
        }

    @staticmethod
    def is_disabled(source: str) -> bool:
        """Check whether this source is disabled via env var.

        Checks both the canonical `DATALINE_DISABLE_<SOURCE>` and any
        legacy aliases (e.g. `DATALINE_DISABLE_A2` for domain_bindings).
        """
        if os.environ.get(_env_disable_flag(source)):
            return True
        for legacy in _LEGACY_DISABLE_ENV_ALIASES.get(source, ()):
            if os.environ.get(legacy):
                return True
        return False


# ── Feature-flag helper ──────────────────────────────────────────────

def evidence_ledger_enabled() -> bool:
    """Phase-1 master switch. Default OFF until smoke test passes."""
    return bool(os.environ.get("DATALINE_EVIDENCE_LEDGER"))
