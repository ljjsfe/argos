"""Tests for Evidence + EvidenceLedger.

Design reference: docs/EVIDENCE_LEDGER_DESIGN.md §7.
"""

from __future__ import annotations

import pytest

from dataline.core.evidence_ledger import (
    CANONICAL_PRIORITIES,
    Evidence,
    EvidenceLedger,
    evidence_ledger_enabled,
)


# ── Evidence dataclass invariants ────────────────────────────────────


class TestEvidenceInvariants:
    def test_basic_construction(self):
        ev = Evidence(source="focus_hints", payload="hello", priority=70)
        assert ev.source == "focus_hints"
        assert ev.payload == "hello"
        assert ev.priority == 70
        assert ev.confidence == 1.0
        assert ev.risk_class == "additive"

    def test_empty_source_rejected(self):
        with pytest.raises(ValueError):
            Evidence(source="", payload="x")

    def test_priority_out_of_bounds(self):
        with pytest.raises(ValueError):
            Evidence(source="x", payload="y", priority=-1)
        with pytest.raises(ValueError):
            Evidence(source="x", payload="y", priority=101)

    def test_confidence_out_of_bounds(self):
        with pytest.raises(ValueError):
            Evidence(source="x", payload="y", confidence=-0.1)
        with pytest.raises(ValueError):
            Evidence(source="x", payload="y", confidence=1.5)

    def test_invalid_risk_class(self):
        with pytest.raises(ValueError):
            Evidence(source="x", payload="y", risk_class="dangerous")

    def test_canonical_priorities_documented(self):
        # All current emitter sources must have a canonical priority slot.
        # If a new source is added, document its priority here.
        for required in ("focus_hints", "domain_bindings", "doc_glossary", "narrative_tables"):
            assert required in CANONICAL_PRIORITIES, (
                f"{required} missing from CANONICAL_PRIORITIES — "
                "add it before introducing a new emitter"
            )

    def test_to_section_preserves_fields(self):
        ev = Evidence(
            source="focus_hints",
            payload="line1\nline2",
            priority=70,
            section_heading="## Focus Hints",
            compressible=False,
        )
        s = ev.to_section()
        assert s.name == "focus_hints"
        assert s.content == "line1\nline2"
        assert s.priority == 70
        assert s.heading == "## Focus Hints"
        assert s.compressible is False

    def test_section_name_defaults_to_source(self):
        ev = Evidence(source="abc", payload="x")
        assert ev.to_section().name == "abc"

    def test_section_name_override(self):
        ev = Evidence(source="abc", payload="x", section_name="custom_name")
        assert ev.to_section().name == "custom_name"


# ── EvidenceLedger.add / get / all ───────────────────────────────────


class TestLedgerAddGet:
    def test_add_and_get(self):
        ledger = EvidenceLedger()
        ev = Evidence(source="focus_hints", payload="x")
        assert ledger.add(ev) is True
        assert ledger.get("focus_hints") == [ev]
        assert ledger.all() == [ev]

    def test_empty_payload_dropped(self):
        ledger = EvidenceLedger()
        assert ledger.add(Evidence(source="x", payload="   ")) is False
        assert ledger.all() == []
        s = ledger.summary()
        assert s["n_dropped"] == 1
        assert s["dropped"][0] == ("x", "empty_payload")

    def test_multiple_sources(self):
        ledger = EvidenceLedger()
        ledger.add(Evidence(source="focus_hints", payload="a"))
        ledger.add(Evidence(source="domain_bindings", payload="b"))
        ledger.add(Evidence(source="focus_hints", payload="c"))
        assert len(ledger.get("focus_hints")) == 2
        assert len(ledger.get("domain_bindings")) == 1
        assert len(ledger.all()) == 3


# ── EvidenceLedger.to_sections (priority-sorted) ─────────────────────


class TestLedgerToSections:
    def test_priority_sort_desc(self):
        ledger = EvidenceLedger()
        ledger.add(Evidence(source="low", payload="L", priority=10))
        ledger.add(Evidence(source="high", payload="H", priority=90))
        ledger.add(Evidence(source="mid", payload="M", priority=50))
        sections = ledger.to_sections()
        assert [s.name for s in sections] == ["high", "mid", "low"]

    def test_stable_tiebreak_insertion_order(self):
        ledger = EvidenceLedger()
        ledger.add(Evidence(source="a", payload="A", priority=70))
        ledger.add(Evidence(source="b", payload="B", priority=70))
        sections = ledger.to_sections()
        # Same priority → preserve insertion order.
        assert [s.name for s in sections] == ["a", "b"]


# ── Env-var disable ──────────────────────────────────────────────────


class TestLedgerEnvDisable:
    def test_canonical_disable_env(self, monkeypatch):
        monkeypatch.setenv("DATALINE_DISABLE_FOCUS_HINTS", "1")
        ledger = EvidenceLedger()
        ok = ledger.add(Evidence(source="focus_hints", payload="x"))
        assert ok is False
        assert ledger.all() == []
        assert ledger.summary()["n_dropped"] == 1

    def test_legacy_a2_env_still_works(self, monkeypatch):
        # Back-compat: DATALINE_DISABLE_A2 was the original env var for
        # domain_bindings (commit b346bca). Migration must keep honoring it.
        monkeypatch.setenv("DATALINE_DISABLE_A2", "1")
        ledger = EvidenceLedger()
        assert ledger.add(Evidence(source="domain_bindings", payload="x")) is False

    def test_other_sources_unaffected_by_one_disable(self, monkeypatch):
        monkeypatch.setenv("DATALINE_DISABLE_FOCUS_HINTS", "1")
        ledger = EvidenceLedger()
        assert ledger.add(Evidence(source="domain_bindings", payload="x")) is True
        assert ledger.add(Evidence(source="focus_hints", payload="y")) is False
        assert [e.source for e in ledger.all()] == ["domain_bindings"]


# ── summary() shape ──────────────────────────────────────────────────


class TestLedgerSummary:
    def test_summary_shape(self):
        ledger = EvidenceLedger()
        ledger.add(Evidence(source="focus_hints", payload="aa", cost_tokens=10))
        ledger.add(Evidence(source="focus_hints", payload="bbb", cost_tokens=15))
        ledger.add(Evidence(source="doc_glossary", payload="c", priority=80, cost_tokens=200))
        s = ledger.summary()
        assert s["n_items"] == 3
        assert s["n_dropped"] == 0
        fh = s["by_source"]["focus_hints"]
        assert fh["count"] == 2
        assert fh["total_chars"] == 5
        assert fh["total_cost_tokens"] == 25
        dg = s["by_source"]["doc_glossary"]
        assert dg["max_priority"] == 80


# ── Master feature flag ──────────────────────────────────────────────


class TestFeatureFlag:
    def test_default_off(self, monkeypatch):
        # Ensure clean env
        monkeypatch.delenv("DATALINE_EVIDENCE_LEDGER", raising=False)
        assert evidence_ledger_enabled() is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv("DATALINE_EVIDENCE_LEDGER", "1")
        assert evidence_ledger_enabled() is True

    def test_empty_string_treated_as_off(self, monkeypatch):
        monkeypatch.setenv("DATALINE_EVIDENCE_LEDGER", "")
        assert evidence_ledger_enabled() is False
