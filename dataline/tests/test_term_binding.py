"""Tests for A2 — knowledge.md term → entity binding."""

from __future__ import annotations

from pathlib import Path

from dataline.agents.term_binding import (
    TermDef,
    build_domain_bindings,
    parse_knowledge_terms,
    _entity_matches_term,
    _extract_lowercase_phrases,
)


# ---- parse_knowledge_terms ----

class TestParseKnowledgeTerms:
    def test_simple_term_with_type(self):
        md = (
            "### Patient\n"
            "- **SEX (text):** Gender of the patient, denoted as 'M' for male.\n"
            "- **Birthday (date):** Date of birth.\n"
        )
        terms = parse_knowledge_terms(md)
        assert len(terms) == 2
        names = [t.term for t in terms]
        assert "SEX (text)" in names
        assert all(t.section == "Patient" for t in terms)

    def test_comma_list_term(self):
        md = (
            "### Members\n"
            "- **first_name, last_name**: Together the full name of a member.\n"
        )
        terms = parse_knowledge_terms(md)
        assert len(terms) == 1
        assert "first_name" in terms[0].aliases()
        assert "last_name" in terms[0].aliases()

    def test_filters_example_sections(self):
        md = (
            "### Example 1: Calculate X\n"
            "- **SQL:**\n"
            "```sql\nSELECT * FROM t\n```\n"
            "- **Formula:** complicated.\n"
            "### Real Entity\n"
            "- **field**: a true definition.\n"
        )
        terms = parse_knowledge_terms(md)
        names = [t.term for t in terms]
        assert "field" in names
        # Example/Formula/SQL meta-terms should be filtered
        assert "SQL" not in names
        assert "Formula" not in names

    def test_meta_term_names_filtered(self):
        md = (
            "### Real Section\n"
            "- **Description**: The description of something.\n"
            "- **actual_column**: Real field.\n"
        )
        terms = parse_knowledge_terms(md)
        assert all(t.term.lower() != "description" for t in terms)
        assert any(t.term == "actual_column" for t in terms)


# ---- _entity_matches_term ----

class TestEntityMatchesTerm:
    def test_name_match(self):
        td = TermDef(section="Patient", term="SEX (text)",
                     body="Gender of the patient")
        m = _entity_matches_term("SEX", td)
        assert m is not None
        assert "SEX" in m

    def test_body_match(self):
        td = TermDef(section="Patient", term="SEX (text)",
                     body="Gender of the patient")
        m = _entity_matches_term("gender", td)
        assert m is not None
        assert "SEX" in m

    def test_no_match(self):
        td = TermDef(section="x", term="abc", body="xyz")
        assert _entity_matches_term("nothing", td) is None

    def test_word_boundary(self):
        # 'ID' should NOT match 'identifier' (substring but not whole word)
        td = TermDef(section="x", term="abc",
                     body="Unique identifier for patient")
        assert _entity_matches_term("ID", td) is None  # too short to even pass length filter
        # 'identifier' should match
        assert _entity_matches_term("identifier", td) is not None

    def test_underscore_alias(self):
        td = TermDef(section="x", term="first_name", body="Full name")
        assert _entity_matches_term("first_name", td) is not None
        assert _entity_matches_term("first name", td) is not None


# ---- _extract_lowercase_phrases ----

class TestLowercasePhrases:
    def test_extracts_noun_phrase(self):
        out = _extract_lowercase_phrases("how many white blood cells are there")
        assert "white blood cells" in out

    def test_filters_stopwords(self):
        out = _extract_lowercase_phrases("the patients have many cases")
        # Should not extract stop-word-only phrases
        assert "the patients" not in out
        assert "have many" not in out


# ---- build_domain_bindings (integration) ----

class TestBuildDomainBindings:
    def _setup(self, tmp_path, knowledge):
        ctx = tmp_path / "context"
        ctx.mkdir()
        (ctx / "knowledge.md").write_text(knowledge)
        (tmp_path / "task.json").write_text('{"task_id":"t","question":"?"}')
        return str(tmp_path)

    def test_name_binding(self, tmp_path):
        task_dir = self._setup(
            tmp_path,
            (
                "### Events\n"
                "- **event_name**: The title of the event.\n"
                "- **type**: Categorizes the event (e.g., Meeting, Game).\n"
            ),
        )
        out = build_domain_bindings("how many events of type Meeting?", task_dir, None)
        assert "Meeting" in out
        assert "type" in out  # term name surfaces in evidence

    def test_no_knowledge_returns_empty(self, tmp_path):
        # task dir without knowledge.md
        (tmp_path / "task.json").write_text('{"task_id":"t","question":"?"}')
        assert build_domain_bindings("any question", str(tmp_path), None) == ""

    def test_capped_output(self, tmp_path):
        # Many terms with the same body word -> many candidate hits.
        body_word = "frequent"
        terms = "\n".join(
            f"- **field_{i}**: Description contains the word {body_word}." for i in range(30)
        )
        task_dir = self._setup(tmp_path, f"### Section\n{terms}\n")
        out = build_domain_bindings(
            f"give me the {body_word} count", task_dir, None,
        )
        # Capped to 10 lines max per build_domain_bindings.
        assert out.count("\n") <= 10
