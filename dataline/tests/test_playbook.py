"""Unit tests for the playbook module."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from dataline.playbook import (
    PlaybookEntry,
    format_for_prompt,
    load_entries,
    record_use,
    retrieve_relevant,
)
from dataline.playbook.tracker import read_telemetry


# --- load_entries ---


def test_load_missing_file_returns_empty():
    assert load_entries("/nonexistent/path.yaml") == ()


def test_load_empty_yaml():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("entries: []\n")
        path = f.name
    try:
        assert load_entries(path) == ()
    finally:
        os.unlink(path)


def test_load_well_formed():
    yaml_text = """
entries:
  - id: pb_001
    type: strategy
    trigger: "join on shared-name ID returns 0 rows"
    action: "verify with value_overlap, then try cross-name FK"
    evidence: ["task_11"]
    added_date: "2026-05-02"
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = f.name
    try:
        out = load_entries(path)
        assert len(out) == 1
        e = out[0]
        assert e.id == "pb_001"
        assert e.type == "strategy"
        assert "value_overlap" in e.action
        assert e.evidence == ("task_11",)
    finally:
        os.unlink(path)


def test_load_skips_malformed_entry():
    yaml_text = """
entries:
  - id: pb_001
    type: strategy
    trigger: "good entry"
    action: "do thing"
  - "this is a string, not a dict"
  - id: pb_002
    type: recovery
    trigger: "another good"
    action: "another thing"
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = f.name
    try:
        out = load_entries(path)
        assert len(out) == 2
        assert {e.id for e in out} == {"pb_001", "pb_002"}
    finally:
        os.unlink(path)


# --- retrieve_relevant ---


def _e(eid: str, trigger: str, action: str = "act") -> PlaybookEntry:
    return PlaybookEntry(
        id=eid, type="strategy", trigger=trigger, action=action,
    )


def test_retrieve_returns_empty_on_no_overlap():
    entries = (_e("x", "json column parsing"),)
    out = retrieve_relevant(entries, question="how many rows?", k=3)
    assert out == ()


def test_retrieve_picks_overlapping_entry():
    entries = (
        _e("a", "json column parsing", "use parse_jsonish_column"),
        _e("b", "average computation", "AVG over rows"),
    )
    out = retrieve_relevant(entries, question="parse json into columns", k=3)
    assert len(out) == 1
    assert out[0].id == "a"


def test_retrieve_respects_k_limit():
    entries = tuple(
        _e(f"e{i}", "join cardinality check", "verify rows after join")
        for i in range(8)
    )
    out = retrieve_relevant(entries, question="join cardinality matters", k=3)
    assert len(out) == 3


def test_retrieve_stopwords_dont_match():
    # 'list' is a stopword — query of just "list rows" should not light up
    # an entry whose only overlapping token is "list".
    entries = (_e("a", "list answer format", "output multiple rows"),)
    out = retrieve_relevant(entries, question="list it", k=3)
    assert out == ()  # 'list' filtered, 'it' too short → no overlap


# --- format_for_prompt ---


def test_format_empty_returns_empty_string():
    assert format_for_prompt(()) == ""


def test_format_renders_each_entry():
    entries = (
        _e("a", "trig A", "action A"),
        _e("b", "trig B", "action B"),
    )
    rendered = format_for_prompt(entries)
    assert "[strategy] trig A → action A" in rendered
    assert "[strategy] trig B → action B" in rendered


# --- tracker ---


def test_record_use_creates_file_and_increments():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tel.json"
        record_use(("pb_001", "pb_002"), won=False, path=path)
        record_use(("pb_001",), won=True, path=path)

        data = read_telemetry(path)
        assert data["pb_001"]["use_count"] == 2
        assert data["pb_001"]["win_count"] == 1
        assert data["pb_002"]["use_count"] == 1
        assert data["pb_002"]["win_count"] == 0


def test_record_use_failsoft_on_bad_path():
    # Writing to a directory we cannot create — should not raise.
    record_use(("pb_001",), won=True, path="/dev/null/cannot/exist.json")


def test_record_use_handles_empty_ids():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tel.json"
        record_use((), won=True, path=path)
        # File should not be created for empty input.
        assert not path.exists()


def test_telemetry_read_handles_corrupt_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tel.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        assert read_telemetry(path) == {}
