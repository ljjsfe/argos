"""A2 — markdown bold-prefix term → column/entity binding.

Deterministic FAST-PATH adapter for documentation written in the
markdown bold-prefix glossary convention:

  ### Section
  - **term**: definition body
  - **term (type):** definition
  - **term:** definition

Matches question entities against (a) the term name itself and
(b) the definition body. Emits a DOMAIN_BINDINGS block telling the
planner which terms the question touches.

SCOPE — be honest about what this covers:
- Works on docs that use the `- **term**:` convention (≈ 70 % of KDD
  knowledge.md files; some Sphinx-style docs; many README/glossary files).
- Does NOT match heading-section-as-glossary, backtick-prefixed columns,
  em-dash glossaries, or any prose-only documentation. On such docs A2
  is silently a no-op — emits no hints, no harm.
- For format-agnostic coverage, B2 (`dataline/agents/doc_glossary.py`)
  runs after A2 with one LLM call per task. A2 and B2 are complementary,
  not redundant: A2 is the 0-LLM fast-path for the common bold-prefix
  style; B2 is the LLM fallback for any other doc shape.

Zero LLM cost. Where the convention matches, A2 is the cheapest possible
adapter. Where it doesn't, B2 carries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..core.types import Manifest


# Markdown patterns ─────────────────────────────────────────────────
_HEADING_RE = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.MULTILINE)

# Markdown patterns where definitions appear:
#   - **term**: body
#   - **term:** body
#   - **term (type):** body
#   - **a, b**: body
# The capture group includes any inline parenthetical type annotation; the
# trailing `:` may be inside or outside the bold span.
_TERM_RE = re.compile(
    r"^\s*[-*]\s+\*\*\s*(?P<term>[^*\n:]{1,80}?)\s*(?::\s*\*\*|\*\*\s*:)\s*"
    r"(?P<body>.*?)(?=^\s*[-*]\s+\*\*|\n#{1,4}\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass(frozen=True)
class TermDef:
    section: str
    term: str  # canonical (as appears in the bold span, may include comma list)
    body: str  # definition body text (single line stripped)

    def aliases(self) -> list[str]:
        """Split comma/slash-separated term names like 'first_name, last_name'."""
        out: list[str] = []
        for piece in re.split(r"[,/]", self.term):
            # Strip parenthetical type annotations: 'SEX (text)' → 'SEX'.
            piece = re.sub(r"\s*\([^)]*\)\s*", "", piece).strip()
            if piece:
                out.append(piece)
        return out


# Sections and term names whose contents are illustrative ("Example 1:",
# "Use Case", "SQL", "Formula", "Description"), not domain definitions.
# Bindings against these almost always create noise.
_EXAMPLE_SECTION_RE = re.compile(
    r"^(example|use\s*case|sample|illustration)\b", re.IGNORECASE
)
# Meta-term filter — these markdown bold spans are documentation scaffolding
# in EXAMPLE / USE CASE sections, not domain glossary entries. Note: we do
# NOT filter "description" here — the pre-v88 audit caught that DABstep's
# `payments-readme.md` uses **Description** as a legitimate dataset term
# entry; filtering it produced a false negative.
_META_TERM_NAMES = frozenset({
    "metric", "formula", "explanation", "sql", "code", "example",
    "use case", "details", "rationale", "note", "notes", "comment",
})


def parse_knowledge_terms(md_text: str) -> list[TermDef]:
    """Return all `- **TERM**: definition` entries grouped by `### Section`.

    Filters out example/illustrative sections — their bold terms ('Metric',
    'Formula', 'SQL') are document scaffolding, not domain definitions.
    """
    if not md_text:
        return []
    terms: list[TermDef] = []

    # Walk headings to attribute terms to the nearest enclosing section.
    headings = [(m.start(), m.group(1).strip()) for m in _HEADING_RE.finditer(md_text)]

    def _section_at(pos: int) -> str:
        section = ""
        for h_start, h_text in headings:
            if h_start <= pos:
                section = h_text
            else:
                break
        return section

    for m in _TERM_RE.finditer(md_text):
        raw_term = m.group("term").strip()
        section = _section_at(m.start())
        if _EXAMPLE_SECTION_RE.search(section):
            continue
        if raw_term.lower() in _META_TERM_NAMES:
            continue
        body = m.group("body").strip().split("\n")[0]  # first line of body
        body = re.sub(r"\s+", " ", body)[:240]
        terms.append(TermDef(section=section, term=raw_term, body=body))
    return terms


def _read_docs(task_dir: str) -> str:
    """Concatenate knowledge.md + context/doc/*.md content. Bounded."""
    root = Path(task_dir)
    chunks: list[str] = []
    seen_paths: set[str] = set()
    candidates: list[Path] = []
    candidates.extend(root.glob("context/knowledge.md"))
    candidates.extend(root.glob("knowledge.md"))
    candidates.extend(sorted(root.glob("context/doc/*.md")))
    candidates.extend(sorted(root.glob("doc/*.md")))
    for p in candidates:
        ap = str(p.resolve())
        if ap in seen_paths or not p.is_file():
            continue
        seen_paths.add(ap)
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n\n".join(chunks)


def _entity_matches_term(entity: str, term_def: TermDef) -> str | None:
    """Return a short evidence string if `entity` matches the term name or
    appears in the term's definition body; None otherwise.

    Both are lower-cased; whole-word match against alias list, word-boundary
    against body. Aliases like 'first_name' match 'first_name' AND
    'first name' (underscore ↔ space tolerant).
    """
    e_lower = entity.lower().strip()
    if not e_lower or len(e_lower) < 3:
        return None
    # Name / alias match
    for alias in term_def.aliases():
        a_lower = alias.lower()
        a_normalized = re.sub(r"[_\-]+", " ", a_lower)
        if e_lower == a_lower or e_lower == a_normalized:
            return f"matches knowledge.md term '{alias}' ({term_def.section})"
    # Body match — only emit when the entity appears as a standalone word
    # in the body. Avoid 'a' matching 'are', etc.
    body_lower = term_def.body.lower()
    if not body_lower:
        return None
    pattern = r"\b" + re.escape(e_lower) + r"\b"
    if re.search(pattern, body_lower):
        return f"appears in knowledge.md definition of '{term_def.term}' ({term_def.section})"
    return None


def build_domain_bindings(question: str, task_dir: str, manifest: Manifest | None = None) -> str:
    """Return DOMAIN_BINDINGS block (or "" when nothing meaningful)."""
    md = _read_docs(task_dir)
    if not md:
        return ""
    terms = parse_knowledge_terms(md)
    if not terms:
        return ""

    # Pull entities from question. Reuse the focus_hints extractor for
    # consistency rather than duplicating regex.
    from .focus_hints import extract_entities
    ent = extract_entities(question)
    candidate_entities = (
        ent["quoted"]
        + ent["capital_phrases"]
        + ent["capital_singles"]
        # Lowercase noun-phrase guess for domain words like "white blood cells".
        + _extract_lowercase_phrases(question)
    )
    # De-dup preserving order.
    seen: set[str] = set()
    candidates: list[str] = []
    for e in candidate_entities:
        k = e.lower()
        if k in seen or len(k) < 3:
            continue
        seen.add(k)
        candidates.append(e)

    matched: list[str] = []  # evidence lines
    seen_evidence: set[tuple[str, str]] = set()
    for ent_text in candidates:
        for td in terms:
            ev = _entity_matches_term(ent_text, td)
            if ev is None:
                continue
            key = (ent_text.lower(), ev)
            if key in seen_evidence:
                continue
            seen_evidence.add(key)
            matched.append(f"- '{ent_text}' → {ev}")
            if len(matched) >= 10:
                return "\n".join(matched)
    return "\n".join(matched)


_NOUN_PHRASE_RE = re.compile(
    r"\b([a-z]{3,}(?:\s+[a-z]{3,}){1,3})\b"
)


_NOUN_PHRASE_STOP = frozenset({
    "the", "and", "for", "with", "from", "have", "has", "had", "their",
    "more", "than", "less", "into", "onto", "over", "many", "much",
    "much", "this", "that", "these", "those", "what", "when", "where",
    "which", "while", "yet", "but", "are", "was", "were", "been", "being",
    "level", "value", "values", "kind", "kinds", "type", "types",
    "patient", "patients", "people", "person", "case", "cases", "item",
    "give", "show", "find", "tell",
})


def _extract_lowercase_phrases(text: str) -> list[str]:
    """Pull short multi-word lowercase noun phrases (e.g. "white blood cells").

    Stopwords filter both individual words and final phrases — we explicitly
    scan every contiguous 2-4 word window rather than relying on a single
    greedy regex match per position, so phrases buried after stop-word
    leading runs still surface.
    """
    out: list[str] = []
    seen: set[str] = set()
    # Tokenise on lowercase alphabetic runs of length ≥ 3.
    tokens = re.findall(r"[a-z]{3,}", text.lower())
    n = len(tokens)
    for size in (3, 2, 4):  # prefer 3-word phrases, then 2 / 4
        for i in range(n - size + 1):
            window = tokens[i:i + size]
            if any(w in _NOUN_PHRASE_STOP for w in window):
                continue
            phrase = " ".join(window)
            if phrase in seen:
                continue
            seen.add(phrase)
            out.append(phrase)
    return out
