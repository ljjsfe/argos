"""Tests for narrative_extractor (Block 4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataline.profiler.narrative_extractor import (
    ExtractedSchema,
    _content_hash,
    _parse_llm_output,
    _sanitize_view_name,
    detect_narrative_shape,
    extract_records,
)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect cache root per-test (same pattern as test_doc_glossary)."""
    import dataline.profiler.narrative_extractor as ne
    monkeypatch.setattr(ne, "_REPO_ROOT_CACHE", tmp_path / "cache")


# ── detect_narrative_shape ────────────────────────────────────────────

class TestDetectNarrativeShape:
    def test_patient_narrative_detected(self):
        text = (
            "Patient 43003 is a male born 1937. His creatinine was 1.2 mg/dL.\n\n"
            "Patient 133382 is a male born 1934. His creatinine was 0.9 mg/dL.\n\n"
            "Patient 387907 is a male born 1937. His creatinine was 1.5 mg/dL.\n"
        )
        is_narr, reason = detect_narrative_shape(text * 5)  # repeat to clear length
        assert is_narr, reason

    def test_too_short_rejected(self):
        is_narr, reason = detect_narrative_shape("Hi there.")
        assert not is_narr
        assert "too short" in reason

    def test_table_heavy_rejected(self):
        text = (
            "| col1 | col2 |\n|------|------|\n| a    | b    |\n| c    | d    |\n"
            "| e    | f    |\n| g    | h    |\n"
        ) * 20
        is_narr, reason = detect_narrative_shape(text)
        assert not is_narr
        assert "table-heavy" in reason

    def test_no_repeating_entity_rejected(self):
        text = (
            "This is a long document with prose but no repeating entity references. "
            "There are no Patient 5 or Molecule 12 patterns here. "
            "Just continuous text discussing concepts without instance IDs.\n"
        ) * 20
        is_narr, reason = detect_narrative_shape(text)
        assert not is_narr
        assert "no repeating" in reason

    def test_glossary_style_rejected(self):
        # Glossaries have **term**: definition format, no repeating IDs.
        text = (
            "## Glossary\n\n"
            "- **PatientID**: A unique identifier for each patient record.\n"
            "- **Creatinine**: Lab value measured in mg/dL.\n"
            "- **Fibrinogen**: Plasma protein.\n"
        ) * 30
        is_narr, reason = detect_narrative_shape(text)
        assert not is_narr  # no Patient N pattern, just one mention


# ── _sanitize_view_name ───────────────────────────────────────────────

class TestSanitizeViewName:
    def test_basic(self):
        assert _sanitize_view_name("Patient.md") == "patient"

    def test_dashes_to_underscores(self):
        assert _sanitize_view_name("Lab-Records.md") == "lab_records"

    def test_strips_collapses(self):
        assert _sanitize_view_name("  Big Doc!.md  ") == "big_doc"

    def test_empty_fallback(self):
        assert _sanitize_view_name(".md") == "narrative"
        assert _sanitize_view_name("---") == "narrative"


# ── _content_hash ─────────────────────────────────────────────────────

class TestContentHash:
    def test_same_content_same_hash(self):
        a = _content_hash("hello world", "modelX")
        b = _content_hash("hello world", "modelX")
        assert a == b

    def test_diff_content_diff_hash(self):
        a = _content_hash("hello", "modelX")
        b = _content_hash("hello world", "modelX")
        assert a != b

    def test_diff_model_diff_hash(self):
        a = _content_hash("hello", "modelX")
        b = _content_hash("hello", "modelY")
        assert a != b


# ── _parse_llm_output ─────────────────────────────────────────────────

class TestParseLlmOutput:
    def test_plain_json(self):
        raw = '{"entity_type": "patient", "schema": [], "records": []}'
        out = _parse_llm_output(raw)
        assert out["entity_type"] == "patient"

    def test_fenced_json(self):
        raw = (
            "Here is the output:\n```json\n"
            '{"entity_type": "molecule", "schema": [], "records": []}'
            "\n```\nDone."
        )
        out = _parse_llm_output(raw)
        assert out["entity_type"] == "molecule"

    def test_trailing_text_tolerated(self):
        raw = (
            '{"entity_type": "x", "schema": [], "records": []}\n'
            "Some explanation after the JSON.\n"
        )
        out = _parse_llm_output(raw)
        assert out is not None

    def test_invalid_returns_none(self):
        assert _parse_llm_output("") is None
        assert _parse_llm_output("not json at all") is None
        assert _parse_llm_output("{broken") is None


# ── extract_records (end-to-end with mock LLM) ───────────────────────

class _FakeLLM:
    """Mock client with .chat(system, user) -> str (real LLMClient shape)."""

    def __init__(self, response: str):
        self.response = response
        self.calls = 0
        self.last_prompt = ""

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_prompt = user  # what we care about checking
        return self.response


class TestExtractRecords:
    NARRATIVE = (
        "Patient 43003 male, born 1937. Creatinine 1.2 mg/dL.\n\n"
        "Patient 133382 male, born 1934. Creatinine 0.9 mg/dL.\n\n"
        "Patient 387907 male, born 1937. Creatinine 1.5 mg/dL.\n\n"
        "Patient 444499 male, born 1954. Creatinine 0.8 mg/dL.\n\n"
        "Patient 485308 male, born 1966. Creatinine 1.1 mg/dL.\n"
    )

    def _valid_response(self):
        return json.dumps({
            "entity_type": "patient",
            "schema": [
                {"name": "patient_id", "type": "integer"},
                {"name": "sex", "type": "text"},
                {"name": "birth_year", "type": "integer"},
                {"name": "creatinine_mg_dl", "type": "real"},
            ],
            "records": [
                {"patient_id": 43003, "sex": "male", "birth_year": 1937, "creatinine_mg_dl": 1.2},
                {"patient_id": 133382, "sex": "male", "birth_year": 1934, "creatinine_mg_dl": 0.9},
                {"patient_id": 387907, "sex": "male", "birth_year": 1937, "creatinine_mg_dl": 1.5},
                {"patient_id": 444499, "sex": "male", "birth_year": 1954, "creatinine_mg_dl": 0.8},
                {"patient_id": 485308, "sex": "male", "birth_year": 1966, "creatinine_mg_dl": 1.1},
            ],
        })

    def test_happy_path(self):
        llm = _FakeLLM(self._valid_response())
        schema = extract_records("/path/Patient.md", self.NARRATIVE * 3, llm)
        assert schema is not None
        assert schema.entity_type == "patient"
        assert "patient_id" in schema.columns
        assert schema.row_count == 5
        assert Path(schema.csv_path).is_file()
        assert llm.calls == 1

    def test_cache_hit(self):
        llm = _FakeLLM(self._valid_response())
        schema1 = extract_records("/path/Patient.md", self.NARRATIVE * 3, llm)
        schema2 = extract_records("/path/Patient.md", self.NARRATIVE * 3, llm)
        assert schema1 is not None and schema2 is not None
        assert llm.calls == 1  # second call hit cache

    def test_cross_task_cache_share(self):
        """Same content from different paths shares cache (content-only hash)."""
        llm = _FakeLLM(self._valid_response())
        schema1 = extract_records("/task_A/Patient.md", self.NARRATIVE * 3, llm)
        schema2 = extract_records("/task_B/Patient.md", self.NARRATIVE * 3, llm)
        assert schema1 is not None and schema2 is not None
        assert llm.calls == 1

    def test_non_narrative_returns_none_without_llm_call(self):
        llm = _FakeLLM("anything")
        # Short doc fails detect_narrative_shape upfront.
        schema = extract_records("/path/note.md", "Just a short note.", llm)
        assert schema is None
        assert llm.calls == 0

    def test_invalid_llm_response_returns_none(self):
        llm = _FakeLLM("not valid json")
        schema = extract_records("/path/Patient.md", self.NARRATIVE * 3, llm)
        assert schema is None

    def test_empty_records_returns_none(self):
        llm = _FakeLLM(json.dumps({"entity_type": None, "schema": [], "records": []}))
        schema = extract_records("/path/Patient.md", self.NARRATIVE * 3, llm)
        assert schema is None

    def test_llm_exception_returns_none(self):
        class _BrokenLLM:
            def chat(self, system, user):
                raise RuntimeError("network down")
        schema = extract_records("/path/Patient.md", self.NARRATIVE * 3, _BrokenLLM())
        assert schema is None

    def test_no_llm_client_returns_none(self):
        schema = extract_records("/path/Patient.md", self.NARRATIVE * 3, None)
        assert schema is None

    def test_truncation_for_very_long_doc(self):
        """Very long docs should still get extracted (truncated input)."""
        llm = _FakeLLM(self._valid_response())
        # 60K chars — should trigger truncation in _build_prompt.
        big = self.NARRATIVE * 1000
        schema = extract_records("/path/Patient.md", big, llm)
        assert schema is not None
        # Prompt should contain truncation marker.
        assert "[... truncated" in llm.last_prompt
