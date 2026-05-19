"""Tests for A1 focus_hints — deterministic question entity ↔ manifest binding."""

from __future__ import annotations

from dataline.agents.focus_hints import (
    build_focus_hints,
    extract_entities,
    _match_entity,
)
from dataline.core.types import Manifest, ManifestEntry


# ---- extract_entities ----

class TestExtractEntities:
    def test_quoted_phrase(self):
        e = extract_entities('Identify the budget for "Yearly Kickoff" event.')
        assert "Yearly Kickoff" in e["quoted"]

    def test_capital_phrase(self):
        e = extract_entities("Tell me about Yearly Kickoff and October Meeting.")
        assert "Yearly Kickoff" in e["capital_phrases"]
        assert "October Meeting" in e["capital_phrases"]

    def test_single_caps_filter_stopwords(self):
        e = extract_entities("Which event for Advertisement?")
        assert "Which" not in e["capital_singles"]
        assert "Advertisement" in e["capital_singles"]

    def test_numbers_filter_short(self):
        # NUMBER_RE requires 2+ digits or decimals
        e = extract_entities("paid more than 29.00 for product 5 in 2012")
        assert "29.00" in e["numbers"]
        assert "2012" in e["numbers"]
        assert "5" not in e["numbers"]  # single digits filtered

    def test_dedup(self):
        e = extract_entities("Alex Yoong, Alex Yoong, Alex Yoong")
        assert e["capital_phrases"].count("Alex Yoong") == 1


# ---- _match_entity ----

class TestMatchEntity:
    COLS = {
        "event.csv::event_name": {
            "table": "event.csv",
            "column": "event_name",
            "dtype": "object",
            "distinct_values": ["October Meeting", "Yearly Kickoff", "Spring Elections"],
        },
        "budget.csv::category": {
            "table": "budget.csv",
            "column": "category",
            "dtype": "object",
            "distinct_values": ["Food", "Advertisement", "Parking"],
        },
    }

    def test_exact_value_match(self):
        hits = _match_entity("Yearly Kickoff", self.COLS)
        assert any("Yearly Kickoff" in h for h in hits)
        assert any("event_name" in h for h in hits)

    def test_case_insensitive_value(self):
        hits = _match_entity("YEARLY KICKOFF", self.COLS)
        assert any("Yearly Kickoff" in h for h in hits)

    def test_column_name_overlap(self):
        # 'category' substring of column name 'category'
        hits = _match_entity("category", self.COLS, prefer_value=False)
        assert any("category" in h for h in hits)

    def test_no_match_returns_empty(self):
        assert _match_entity("nonexistent xyzzy", self.COLS) == []

    def test_values_only_skips_column_name_match(self):
        hits = _match_entity("category", self.COLS, values_only=True)
        assert hits == []


# ---- build_focus_hints integration ----

def _mk_entry(file_path, columns):
    return ManifestEntry(
        file_path=file_path,
        file_type="csv",
        size_bytes=100,
        summary={"columns": columns},
    )


class TestBuildFocusHints:
    def _manifest(self):
        return Manifest(entries=(
            _mk_entry(
                "/some/event.csv",
                [
                    {
                        "name": "event_name",
                        "dtype": "object",
                        "cardinality": 3,
                        "sample": ["October Meeting", "Yearly Kickoff", "Spring Elections"],
                    },
                    {
                        "name": "type",
                        "dtype": "object",
                        "cardinality": 2,
                        "sample": ["Meeting", "Election"],
                    },
                ],
            ),
            _mk_entry(
                "/some/budget.csv",
                [
                    {
                        "name": "category",
                        "dtype": "object",
                        "cardinality": 3,
                        "sample": ["Food", "Advertisement", "Parking"],
                    },
                ],
            ),
        ))

    def test_quoted_entities_matched(self):
        out = build_focus_hints(
            'Budget for "Yearly Kickoff" and "October Meeting"?',
            self._manifest(),
        )
        assert "Yearly Kickoff" in out
        assert "October Meeting" in out
        assert "event.csv:event_name" in out

    def test_capital_singles_match_values_only(self):
        # 'Advertisement' is a value in budget.csv:category
        out = build_focus_hints(
            "Find the Advertisement total.",
            self._manifest(),
        )
        assert "Advertisement" in out
        assert "budget.csv:category" in out

    def test_empty_manifest_returns_empty_string(self):
        empty_manifest = Manifest(entries=())
        assert build_focus_hints("any question", empty_manifest) == ""

    def test_empty_question(self):
        assert build_focus_hints("", self._manifest()) == ""

    def test_no_match_returns_empty(self):
        out = build_focus_hints(
            "What is the quantum entanglement coefficient?",
            self._manifest(),
        )
        assert out == ""

    def test_output_capped(self):
        # Manifest with many matching columns shouldn't produce unbounded output
        many_cols = [
            {
                "name": f"col_{i}",
                "dtype": "object",
                "cardinality": 1,
                "sample": ["Advertisement"],
            }
            for i in range(50)
        ]
        big = Manifest(entries=(_mk_entry("/x.csv", many_cols),))
        out = build_focus_hints("Find Advertisement.", big)
        # Cap at 12 lines per build_focus_hints implementation
        assert out.count("\n") <= 12


# ---- end-to-end: real prototype demo task ----

def test_demo_task_352_scenario():
    """task_352 scenario from prototype: Yearly Kickoff and October Meeting
    must bind to event.csv:event_name."""
    manifest = Manifest(entries=(
        _mk_entry(
            "/some/event.csv",
            [{
                "name": "event_name",
                "dtype": "object",
                "cardinality": 4,
                "sample": ["October Meeting", "Yearly Kickoff", "Spring Elections", "Fall Budget"],
            }],
        ),
    ))
    out = build_focus_hints(
        'How many times was the budget in Advertisement for "Yearly Kickoff" meeting more than "October Meeting"?',
        manifest,
    )
    # Both quoted phrases should produce hints anchoring to event_name
    assert "Yearly Kickoff" in out
    assert "October Meeting" in out
    assert out.count("event.csv:event_name") >= 2
