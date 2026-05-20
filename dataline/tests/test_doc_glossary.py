"""Tests for B2 — LLM-extracted doc glossary, deterministic matching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataline.agents.doc_glossary import (
    DocGlossary,
    EMPTY_GLOSSARY,
    FormulaDef,
    RuleDef,
    SCHEMA_VERSION,
    TermDefV2,
    _cache_key,
    _collect_doc_files,
    _from_dict,
    _question_phrases,
    _read_and_concat,
    _to_dict,
    build_doc_glossary_hints,
    extract_doc_glossary,
)


# ── _from_dict / _to_dict round-trip ─────────────────────────────

class TestSchema:
    def test_empty(self):
        g = _from_dict({})
        assert g.terms == ()
        assert g.formulas == ()
        assert g.rules == ()

    def test_roundtrip(self):
        src = {
            "schema_version": SCHEMA_VERSION,
            "source_files": ["x.md"],
            "terms": [{
                "name": "Admission",
                "aliases": ["admission status"],
                "definition": "Whether the patient was admitted (+) or outpatient (-).",
                "data_field": {"table_or_file": "patient.csv", "column": "Admission"},
                "value_enum": [{"value": "+", "meaning": "inpatient"},
                               {"value": "-", "meaning": "outpatient"}],
                "value_range": None,
                "source_section": "Patient",
            }],
            "formulas": [{
                "name": "Inpatient Ratio",
                "expression": "COUNT(+) / COUNT(-)",
                "inputs": ["Admission"],
                "source_section": "KPIs",
            }],
            "rules": [{
                "statement": "Severe thrombosis means Thrombosis=1 or 2",
                "applies_to": ["Thrombosis"],
                "source_section": "Examination",
            }],
            "synonyms": [{"a": "ID", "b": "patient_id", "note": None}],
        }
        g = _from_dict(src)
        assert len(g.terms) == 1
        assert g.terms[0].name == "Admission"
        assert g.terms[0].data_field_column == "Admission"
        assert len(g.terms[0].value_enum) == 2
        # round-trip back
        out = _to_dict(g)
        assert out["terms"][0]["name"] == "Admission"
        assert out["formulas"][0]["expression"] == "COUNT(+) / COUNT(-)"

    def test_tolerant_to_garbage(self):
        # Extra keys ignored; bad types skipped.
        g = _from_dict({"terms": "not a list", "formulas": None, "extra": 42})
        assert g.terms == ()
        assert g.formulas == ()

    def test_term_without_name_dropped(self):
        g = _from_dict({"terms": [{"definition": "no name"}, {"name": "ok"}]})
        assert len(g.terms) == 1
        assert g.terms[0].name == "ok"

    def test_truncation(self):
        long = "x" * 500
        g = _from_dict({"terms": [{"name": "T", "definition": long}]})
        assert len(g.terms[0].definition) == 240


# ── _question_phrases ────────────────────────────────────────────

class TestQuestionPhrases:
    def test_quoted(self):
        ps = _question_phrases('What is "Yearly Kickoff"?')
        assert "Yearly Kickoff" in ps

    def test_capital_phrase(self):
        # Sentence-initial verb 'Find' may absorb into the title-case
        # capture; what matters is that 'october meeting' surfaces as a
        # candidate phrase (lower- or title-case forms both flow into
        # substring matching downstream).
        ps = _question_phrases("Identify the October Meeting budget.")
        assert any("October Meeting" in p for p in ps), ps

    def test_lowercase_noun_phrase(self):
        ps = _question_phrases("how many white blood cells")
        assert "white blood cells" in ps


# ── build_doc_glossary_hints ─────────────────────────────────────

class TestBuildHints:
    def _make_glossary(self) -> DocGlossary:
        return DocGlossary(
            schema_version=SCHEMA_VERSION,
            source_files=("knowledge.md",),
            terms=(
                TermDefV2(
                    name="Thrombosis",
                    aliases=(),
                    definition="Degree of thrombosis; 1 most severe, 2 severe.",
                    data_field_column="Thrombosis",
                    value_enum=(("1", "most severe"), ("2", "severe")),
                    source_section="Examination",
                ),
                TermDefV2(
                    name="Admission",
                    aliases=("admission status",),
                    definition="Whether patient was admitted (+) or outpatient (-).",
                    data_field_column="Admission",
                    value_enum=(("+", "inpatient"), ("-", "outpatient")),
                    source_section="Patient",
                ),
                TermDefV2(
                    name="UnrelatedTerm",
                    definition="Has no overlap with the question.",
                ),
            ),
            formulas=(
                FormulaDef(
                    name="Inpatient Ratio",
                    expression="COUNT(+) / COUNT(-)",
                    inputs=("Admission",),
                    source_section="KPIs",
                ),
            ),
        )

    def test_empty_glossary(self):
        assert build_doc_glossary_hints("any?", EMPTY_GLOSSARY) == ""

    def test_strong_match(self):
        out = build_doc_glossary_hints(
            "Among patients with severe thrombosis, count by admission",
            self._make_glossary(),
        )
        assert "Thrombosis" in out
        assert "Admission" in out
        assert "UnrelatedTerm" not in out

    def test_capped_at_12_lines(self):
        terms = tuple(
            TermDefV2(
                name=f"Field{i}",
                aliases=(),
                definition="generic widget",
                data_field_column=f"col_{i}",
                source_section="Generic",
            )
            for i in range(40)
        )
        g = DocGlossary(terms=terms)
        # Question mentions 'field' which appears in many term names.
        out = build_doc_glossary_hints("compute field0 field1 field2 field3 field4 field5 field6 field7 field8 field9 field10 field11 field12 field13", g)
        assert out.count("\n") < 12

    def test_no_match_returns_empty(self):
        out = build_doc_glossary_hints("totally unrelated query?", self._make_glossary())
        assert out == ""

    def test_formula_emitted_when_input_in_question(self):
        out = build_doc_glossary_hints(
            "What is the Inpatient Ratio for males?", self._make_glossary(),
        )
        assert "Formula" in out or "Inpatient Ratio" in out


# ── extract_doc_glossary (with mock LLM) ─────────────────────────

class _FakeLLM:
    def __init__(self, response_json: dict, fail: bool = False):
        self._response = response_json
        self._fail = fail
        self.calls = 0
        # Match shape used by extract_doc_glossary to read model name.
        self._config = type("Cfg", (), {"model": "fake-model"})()

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        if self._fail:
            raise RuntimeError("simulated LLM failure")
        return json.dumps(self._response)


def _write_doc(tmp_path: Path, body: str = "# Title\nSome content."):
    (tmp_path / "context").mkdir(parents=True, exist_ok=True)
    (tmp_path / "task.json").write_text('{"task_id":"t","question":"?"}')
    (tmp_path / "context/knowledge.md").write_text(body)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect _REPO_ROOT_CACHE to a per-test tmp dir so the content-hash
    cache doesn't leak state across tests (cache key is now content-only,
    so identical-doc tests would share entries without this isolation)."""
    import dataline.agents.doc_glossary as dg
    monkeypatch.setattr(dg, "_REPO_ROOT_CACHE", tmp_path / "_cache_iso")


class TestExtract:
    def test_no_docs_returns_empty(self, tmp_path):
        (tmp_path / "task.json").write_text('{"task_id":"t","question":"?"}')
        llm = _FakeLLM({})
        g = extract_doc_glossary(str(tmp_path), None, llm)
        assert g is EMPTY_GLOSSARY
        assert llm.calls == 0

    def test_llm_failure_returns_empty(self, tmp_path):
        _write_doc(tmp_path)
        llm = _FakeLLM({}, fail=True)
        g = extract_doc_glossary(str(tmp_path), None, llm)
        assert g.terms == ()

    def test_basic_extraction(self, tmp_path):
        _write_doc(tmp_path, "### Glossary\n- **Foo**: a thing.\n")
        llm = _FakeLLM({
            "schema_version": SCHEMA_VERSION,
            "source_files": ["context/knowledge.md"],
            "terms": [{
                "name": "Foo", "aliases": [], "definition": "a thing.",
                "data_field": {"table_or_file": None, "column": None},
                "value_enum": [], "value_range": None,
                "source_section": "Glossary",
            }],
            "formulas": [], "rules": [], "synonyms": [],
        })
        g = extract_doc_glossary(str(tmp_path), None, llm)
        assert len(g.terms) == 1
        assert g.terms[0].name == "Foo"
        assert llm.calls == 1

    def test_cache_hit_skips_llm(self, tmp_path):
        _write_doc(tmp_path)
        llm = _FakeLLM({
            "schema_version": SCHEMA_VERSION,
            "source_files": ["context/knowledge.md"],
            "terms": [{
                "name": "Cached", "aliases": [], "definition": ".",
                "data_field": {"table_or_file": None, "column": None},
                "value_enum": [], "value_range": None, "source_section": "",
            }],
            "formulas": [], "rules": [], "synonyms": [],
        })
        # First call: LLM runs.
        g1 = extract_doc_glossary(str(tmp_path), None, llm)
        assert llm.calls == 1
        # Second call: cache hit.
        g2 = extract_doc_glossary(str(tmp_path), None, llm)
        assert llm.calls == 1
        assert g1.terms[0].name == g2.terms[0].name


# ── _collect / _read_and_concat ──────────────────────────────────

class TestCollect:
    def test_picks_up_knowledge_and_doc_subdir(self, tmp_path):
        (tmp_path / "context").mkdir()
        (tmp_path / "context" / "knowledge.md").write_text("A")
        (tmp_path / "context" / "doc").mkdir()
        (tmp_path / "context" / "doc" / "extra.md").write_text("B")
        files = _collect_doc_files(tmp_path)
        names = [p.name for p in files]
        assert "knowledge.md" in names
        assert "extra.md" in names

    def test_truncation(self, tmp_path):
        (tmp_path / "context").mkdir()
        big = "x" * 50000
        (tmp_path / "context" / "knowledge.md").write_text(big)
        files = _collect_doc_files(tmp_path)
        text, paths = _read_and_concat(files, max_bytes=1000)
        assert len(text) <= 1500  # 1000 + header + truncation marker
        assert "[truncated]" in text


# ── cache LOCATION hygiene (audit 2026-05-19) ────────────────────


class TestCacheLocation:
    """The B2 cache MUST NOT live inside task_dir.

    The 2026-05-19 audit found that writing cache files into `task_dir`
    caused Profiler to re-scan them as inputs on subsequent runs, leading
    to self-injected info pollution across all 50 KDD task inputs.
    """

    def test_cache_writes_outside_task_dir(self, tmp_path, monkeypatch):
        # Redirect the module-level cache root to an isolated tmp dir so
        # this test does not pollute the real .dataline_cache/.
        import dataline.agents.doc_glossary as dg
        cache_root = tmp_path / "fake_repo_cache" / "doc_glossary"
        monkeypatch.setattr(dg, "_REPO_ROOT_CACHE", cache_root)

        task_dir = tmp_path / "task"
        _write_doc(task_dir)
        (task_dir / "task.json").write_text('{"task_id":"t","question":"?"}')

        llm = _FakeLLM({
            "schema_version": SCHEMA_VERSION,
            "source_files": ["context/knowledge.md"],
            "terms": [{
                "name": "Foo", "aliases": [], "definition": ".",
                "data_field": {"table_or_file": None, "column": None},
                "value_enum": [], "value_range": None, "source_section": "",
            }],
            "formulas": [], "rules": [], "synonyms": [],
        })
        extract_doc_glossary(str(task_dir), None, llm)

        # The cache file must land in our redirected root, not under task_dir.
        produced_in_task = list(task_dir.rglob(".dataline_cache"))
        assert produced_in_task == [], (
            f"Cache leaked into task_dir: {produced_in_task}"
        )
        produced_in_cache = list(cache_root.glob("*.json"))
        assert len(produced_in_cache) == 1, (
            f"Expected 1 cache file in repo cache, got {produced_in_cache}"
        )


# ── cache key stability ──────────────────────────────────────────

def test_cache_key_stable(tmp_path):
    (tmp_path / "a.md").write_text("alpha")
    (tmp_path / "b.md").write_text("beta")
    files = [tmp_path / "a.md", tmp_path / "b.md"]
    k1 = _cache_key(files, "model-x")
    k2 = _cache_key(list(reversed(files)), "model-x")
    assert k1 == k2  # order-independent
    # Different model → different key
    k3 = _cache_key(files, "model-y")
    assert k1 != k3
    # Same model + content → same key
    k4 = _cache_key(files, "model-x")
    assert k1 == k4


def test_cache_key_is_content_derived_not_path(tmp_path):
    """Two files with identical bytes at different paths must produce the
    same cache key — comment in doc_glossary.py says cross-task sharing
    works naturally when docs repeat. Verify the implementation matches.
    """
    (tmp_path / "task_a").mkdir()
    (tmp_path / "task_b").mkdir()
    bytes_ = "## Glossary\n- **Foo**: bar.\n"
    (tmp_path / "task_a" / "knowledge.md").write_text(bytes_)
    (tmp_path / "task_b" / "knowledge.md").write_text(bytes_)

    k_a = _cache_key([tmp_path / "task_a" / "knowledge.md"], "model-x")
    k_b = _cache_key([tmp_path / "task_b" / "knowledge.md"], "model-x")
    assert k_a == k_b, "Identical content at different paths must share cache"

    # And: same path, different content → different key
    (tmp_path / "task_b" / "knowledge.md").write_text("## Other\n")
    k_b2 = _cache_key([tmp_path / "task_b" / "knowledge.md"], "model-x")
    assert k_a != k_b2
