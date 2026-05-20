# B2 — Doc-Glossary Extraction (LLM-parsed, structured, format-agnostic)

## 1. Executive summary

The current `term_binding.py` (A2) bolt-parses `- **term**:` patterns out of
markdown. The format audit shows that pattern covers 0% of docs end-to-end
and yields nothing on 30% of KDD tasks and on DABstep's `manual.md`. We
replace it with **B2**: one LLM call per task, given the raw concatenated
doc text, that emits a strict JSON glossary of domain terms, field
mappings, value enumerations, formulas, and synonyms. The glossary is
cached to disk keyed by content hash, then matched deterministically
against the question to produce a small `DOC_GLOSSARY_HINTS` block fed
into PlannerCoder. Style-agnostic because the LLM sees raw text; cheap
because it runs once per task; safe because the matcher is pure regex/set
logic with hard ceilings and the whole stage fails soft (empty → no
hints, agent unaffected).

## 2. Schema

JSON output. The top-level object is a `DocGlossary`. Unknown / not-found
fields are emitted as `null` or empty arrays — never omitted. The LLM is
told to be conservative: when a field cannot be sourced from the doc, it
must be left empty.

```jsonc
{
  "schema_version": "b2.v1",
  "source_files": ["context/knowledge.md", "context/doc/foo.md"],
  "terms": [
    {
      "name": "Admission",                  // canonical term as used in doc
      "aliases": ["admission status"],      // surface variants in the doc
      "definition": "Whether the patient was admitted ('+') or followed up at outpatient clinic ('-').",
      "data_field": {                        // optional binding to a column/field
        "table_or_file": null,               // null when doc doesn't name a file
        "column": "Admission"                // null when no clear column mention
      },
      "value_enum": [                        // categorical values + their meaning
        {"value": "+", "meaning": "inpatient"},
        {"value": "-", "meaning": "outpatient"}
      ],
      "value_range": null,                   // {"min":..,"max":..,"unit":..} when present
      "source_section": "Core Entities & Fields > Patient"
    }
  ],
  "formulas": [
    {
      "name": "Inpatient vs. Outpatient Ratio for Males",
      "expression": "COUNT(ID WHERE Admission='+' AND SEX='M') / COUNT(ID WHERE Admission='-' AND SEX='M')",
      "inputs": ["Admission", "SEX", "ID"],
      "source_section": "Metric Definitions"
    }
  ],
  "rules": [
    {
      "statement": "If a fee rule field is null it applies to all values of that field.",
      "applies_to": ["fees.json"],
      "source_section": "Understanding Payment Processing Fees > Notes"
    }
  ],
  "synonyms": [                              // pure name-equivalence pairs
    {"a": "Diagnosis", "b": "Disease", "note": "use Diagnosis for consistency"}
  ]
}
```

### Style-flavour examples

**Flavour A — heading + prose (DABstep `manual.md`).** Definitions are
embedded in paragraphs; values are inside markdown tables. The LLM
should yield:

```jsonc
{"name": "ACI", "aliases": ["Authorization Characteristics Indicator"],
 "definition": "Field identifying transaction flow submitted to acquirer.",
 "data_field": {"table_or_file": null, "column": "aci"},
 "value_enum": [
   {"value": "A", "meaning": "Card present - Non-authenticated"},
   {"value": "B", "meaning": "Card Present - Authenticated"}
   /* ... */
 ],
 "source_section": "Authorization Characteristics Indicator (ACI)"}
```

**Flavour B — bullet-prefix glossary (KDD `knowledge.md`).** Already
structured; the LLM mostly transcribes.

```jsonc
{"name": "Thrombosis", "aliases": [],
 "definition": "Degree of thrombosis observed during examination.",
 "data_field": {"table_or_file": "Examination", "column": "Thrombosis"},
 "value_enum": [
   {"value": "1", "meaning": "most severe"},
   {"value": "2", "meaning": "severe"}
 ],
 "source_section": "Core Entities & Fields > Examination"}
```

**Flavour C — backtick column list (DABstep `payments-readme.md`).** The
LLM treats backticked identifiers as `data_field.column`:

```jsonc
{"name": "shopper_interaction", "aliases": ["payment method"],
 "definition": "Payment method used in the transaction.",
 "data_field": {"table_or_file": "payments.csv", "column": "shopper_interaction"},
 "value_enum": [
   {"value": "Ecommerce", "meaning": "online"},
   {"value": "POS",       "meaning": "in-person or in-store"}
 ],
 "source_section": "Columns"}
```

## 3. Prompt template

System prompt (concise; Qwen-3B-active calibration):

```
You extract a STRUCTURED GLOSSARY from one or more domain documentation files.

Goal: produce JSON usable by a downstream code generator that answers
questions over the dataset described by these docs.

What to include:
- Domain terms with their definitions (one entry per term).
- When the doc names a data column / field / table for a term, record it
  under data_field.
- When the doc enumerates allowed values for a term, record each value
  and its meaning under value_enum.
- When the doc gives a numeric range / unit, record value_range.
- Business / computation rules and named formulas: record under rules
  and formulas. Inputs must reference term names you also emitted.
- Synonym pairs the doc explicitly states.

What to SKIP:
- Illustrative example sections ("Example 1", "Use Case N", "Sample
  query", "SQL:" blocks, "Explanation:" paragraphs). These are not
  domain knowledge.
- Marketing / introductory prose ("As a valued partner…").
- Contact info, copyright, table-of-contents, version banners.
- Best-practice prose without a defined term ("choose the right ACI",
  "minimize fraud"). Only emit a term if the doc DEFINES it.

Output rules:
- Output ONLY the JSON object — no markdown fence, no commentary.
- Follow the schema exactly. Use null / [] for missing fields.
- Term `name` should be the canonical form used in the doc.
- Do NOT invent columns or tables the doc does not mention.
- Cap: at most 80 terms, 30 formulas, 30 rules, 30 synonyms.
- Each `definition` ≤ 240 chars. Each `expression` ≤ 200 chars.
- If the doc contains no domain content (only contact info /
  boilerplate), return {"terms":[],"formulas":[],"rules":[],"synonyms":[],
  "source_files":[...],"schema_version":"b2.v1"}.

Schema:
<paste the schema block from section 2>
```

User message:

```
Task data files (for reference; do not invent columns outside this list):
<comma-separated list of basenames from the manifest, e.g. payments.csv, fees.json, merchant_data.json>

Documentation (concatenated, with file headers):

===== context/knowledge.md =====
<full text>

===== context/doc/foo.md =====
<full text>
```

Token guardrails: total doc payload truncated at 20 KB (≈ 5k tokens).
If concatenated docs exceed this, the loader chunks them (heading-aligned
splits) and runs the LLM per chunk, then merges by `(name, source_section)`
de-dup.

## 4. Matching algorithm

Goal: given the cached `DocGlossary` and a question, return a short
`DOC_GLOSSARY_HINTS` text block (≤ 12 lines). Pure deterministic
post-processing — no LLM.

```python
def build_doc_glossary_hints(question: str, glossary: DocGlossary) -> str:
    if not glossary or not glossary.terms:
        return ""

    # 1. Tokenise question.
    q_text = question.lower()
    q_tokens = set(re.findall(r"[a-z][a-z0-9_]{2,}", q_text))
    q_phrases = (
        extract_entities(question)["quoted"]
        + extract_entities(question)["capital_phrases"]
        + _lowercase_phrases(question)         # reuse term_binding helper
    )

    # 2. Score each term.
    hits: list[tuple[float, str]] = []
    for t in glossary.terms:
        score = 0.0
        evidence = []

        # 2a. Exact phrase / alias / column match → strong.
        for cand in [t.name, *t.aliases, (t.data_field or {}).get("column", "")]:
            if not cand: continue
            c = cand.lower()
            if c in q_text:
                score += 3.0; evidence.append(f"matches '{cand}'"); break

        # 2b. Token overlap with name or aliases (weaker).
        name_tokens = set(re.findall(r"[a-z0-9_]{3,}", t.name.lower()))
        if name_tokens & q_tokens:
            score += 1.0

        # 2c. Definition body overlap (weakest — gated).
        if score == 0:
            def_tokens = set(re.findall(r"[a-z0-9_]{4,}", t.definition.lower()))
            overlap = def_tokens & q_tokens - STOPWORDS
            if len(overlap) >= 2:
                score += 0.5; evidence.append("definition overlap")

        if score >= 1.0:
            line = _format_term_hint(t, evidence)
            hits.append((score, line))

    # 3. Formula matches: any question token appears in formula.name or
    #    inputs → include.
    for f in glossary.formulas:
        name_tokens = set(re.findall(r"[a-z0-9_]{3,}", f.name.lower()))
        if (name_tokens & q_tokens) or any(i.lower() in q_text for i in f.inputs):
            hits.append((2.5, _format_formula_hint(f)))

    # 4. Rules that mention any matched term name → include up to 3.
    matched_terms = {h_line for _, h_line in hits}
    for r in glossary.rules:
        if any(t_name.lower() in r.statement.lower()
               for t_name in (t.name for t in glossary.terms)
               if any(t_name.lower() in m for m in matched_terms)):
            hits.append((1.5, _format_rule_hint(r)))

    # 5. Rank, dedup, cap at 12 lines.
    hits.sort(key=lambda x: -x[0])
    return "\n".join(_dedup_preserve_order(line for _, line in hits)[:12])
```

`_format_term_hint` produces lines like:

```
- 'Admission' → Whether patient is inpatient ('+') or outpatient ('-'). Values: + (inpatient), - (outpatient). [Core Entities > Patient]
```

Universality: depends only on question text + glossary JSON, both of
which are language- and benchmark-agnostic.

## 5. Caching

**Cache key**: `sha256(schema_version || sorted(source_file_paths) ||
sorted(source_file_contents) || model_name)`. Recomputed every time;
LLM call skipped when key hits.

**Storage**: `<task_dir>/.dataline_cache/doc_glossary_<hash[:16]>.json`.
Read-only after write. `.dataline_cache/` is `.gitignore`d.

**Global cache**: when `DATALINE_GLOBAL_CACHE_DIR` env var is set
(KDD submission uses this for warm starts), an additional copy lives at
`<global>/doc_glossary/<hash[:16]>.json`. Lookup order: task-local →
global → recompute.

**Invalidation**: automatic via content hash. To force refresh, delete
`.dataline_cache/` or bump `schema_version` in the prompt.

**Concurrency**: file writes atomic via `tempfile.NamedTemporaryFile` +
`os.replace`. Concurrent reads safe; concurrent writes idempotent.

## 6. Integration plan

### New files

- `dataline/agents/doc_glossary.py` — extraction + matching + cache.
- `dataline/prompts/doc_glossary.md` — prompt template (kept out of
  Python for diffing).
- `dataline/tests/test_doc_glossary.py` — unit tests (see §7).

### Public functions in `doc_glossary.py`

```python
@dataclass(frozen=True)
class TermDefV2:
    name: str
    aliases: tuple[str, ...]
    definition: str
    data_field_column: str | None
    data_field_table: str | None
    value_enum: tuple[tuple[str, str], ...]  # (value, meaning)
    value_range_min: float | None
    value_range_max: float | None
    value_range_unit: str | None
    source_section: str

@dataclass(frozen=True)
class FormulaDef:
    name: str
    expression: str
    inputs: tuple[str, ...]
    source_section: str

@dataclass(frozen=True)
class RuleDef:
    statement: str
    applies_to: tuple[str, ...]
    source_section: str

@dataclass(frozen=True)
class DocGlossary:
    schema_version: str
    source_files: tuple[str, ...]
    terms: tuple[TermDefV2, ...]
    formulas: tuple[FormulaDef, ...]
    rules: tuple[RuleDef, ...]
    synonyms: tuple[tuple[str, str, str], ...]  # (a, b, note)

def extract_doc_glossary(
    task_dir: str,
    manifest: Manifest | None,
    llm: LLMClient,
    *, max_doc_bytes: int = 20_000,
) -> DocGlossary:
    """One LLM call (or cache hit) → structured glossary. Never raises."""

def build_doc_glossary_hints(
    question: str, glossary: DocGlossary,
) -> str:
    """Deterministic match → ≤12-line hint block. Never raises."""
```

Both functions return empty (`DocGlossary` with empty tuples / empty
string) on any failure path. Logging via `trace` only.

### State field

Add to `AnalysisState` in `dataline/core/types.py`:

```python
doc_glossary_hints: str = ""    # B2 — LLM-extracted glossary, question-matched
```

And a setter in `dataline/core/state.py`:

```python
def set_doc_glossary_hints(state, hints): ...
```

### Orchestrator hook

`dataline/agents/orchestrator.py`, immediately AFTER the existing
domain_bindings stage (so it sits at the same conceptual layer; it will
eventually subsume A2). Wrapped in try/except, fail-soft:

```python
# ─── Stage 3c''': Doc Glossary — LLM-extracted structured terms ───
try:
    from .doc_glossary import extract_doc_glossary, build_doc_glossary_hints
    glossary = extract_doc_glossary(task_dir, manifest, llm)
    dg_hints = build_doc_glossary_hints(question, glossary)
    if dg_hints:
        state = set_doc_glossary_hints(state, dg_hints)
        _log(trace, "doc_glossary",
             f"{len(glossary.terms)} terms; {dg_hints.count(chr(10))+1} hints")
        obs["doc_glossary_terms"] = len(glossary.terms)
        obs["doc_glossary_hint_lines"] = dg_hints.count("\n") + 1
except Exception as e:
    _log(trace, "doc_glossary", f"skipped: {e}")
```

### PlannerCoder context section

In `planner_coder.build_planner_state_context`, between
`domain_bindings` (priority 91) and `manifest` (priority 90):

```python
if state.doc_glossary_hints:
    sections.append(Section(
        name="doc_glossary_hints",
        content=(
            f"## Doc Glossary (question phrases mapped to documented terms)\n"
            f"{state.doc_glossary_hints}\n\n"
            f"Each hint includes the term's definition, enumerated values, "
            f"and source section. Trust these over guessing column meanings."
        ),
        priority=92,   # ABOVE focus_hints (91) — semantic anchors first
        compressible=True,
        heading="",
    ))
```

Priority 92 ranks it above focus_hints (data-value anchors) and below
harness_feedback (96) / judge_guidance (94). Marked compressible so ContextManager can trim under budget pressure.

### A2 coexistence / migration

A2 (`term_binding.build_domain_bindings`) stays wired during the
shadow-launch window. After two clean eval runs where B2 emits hints on
≥90% of docs and A2 hits a strict subset, A2 is deleted. Until then both
sections appear; PlannerCoder treats them as overlapping anchors.

## 7. Validation plan

No KDD eval scores used in B2 design or validation.

**Static checks (CI / pytest):**
1. Run extractor on the three sample docs in
   `data/dabstep/context/manual.md`, `data/dabstep/context/payments-readme.md`,
   `public/input/task_344/context/knowledge.md` (single sample only; not
   reading further KDD tasks).
2. Assert: every emitted `term.name` is non-empty, `definition` ≤ 240
   chars, JSON parses, schema_version present.
3. Assert: emitted `data_field.column` values are either `None` or a
   substring of the raw doc text (LLM did not hallucinate columns).
4. Assert: `value_enum` values for ACI / Account Type recover all rows
   from the doc's markdown table (table parsing sanity).
5. Cache hit on second call: zero LLM invocations (mock the client and
   assert `chat.call_count == 1` across two calls).

**Quality checks (held-out human eval):**
- Hand-author 5 synthetic docs covering: heading+prose, bullet-prefix,
  table-glossary, plain-text, mixed. (Authored independently of
  benchmark data.)
- For each, hand-write the expected term names + value enums.
- Run extractor; compute precision/recall on term names and enum
  values. Target: ≥ 0.85 recall, ≥ 0.90 precision.

**Performance checks:**
- Latency budget: ≤ 8 s per task on a 20 KB doc (incl. one LLM round
  trip).
- Cost budget: ≤ $0.05 per task at current Qwen-3.5-35B-A3B pricing
  (≈ 5k input + 1.5k output tokens).
- Memory: glossary JSON < 64 KB on disk per task.

**Question-matching unit tests:**
- Given a fixture glossary with known terms, assert specific questions
  surface the expected hint lines (string-equal); assert irrelevant
  questions return `""`.
- Stop-word edge: question with no matching tokens → empty.
- Cap test: ≥ 20 matching terms → output ≤ 12 lines.

## 8. Risks + mitigations

| Risk | Mitigation |
|---|---|
| LLM hallucinates columns / tables not in the doc | Inject manifest basenames into user prompt; post-extract validation drops `data_field` entries whose `column` does not substring-appear in raw doc text. |
| LLM returns invalid JSON | `json.loads` in try; on failure log + return empty glossary; agent continues without B2 hints. No `repair` retry — keeps cost predictable. |
| Doc > 20 KB | Heading-aligned chunking; per-chunk LLM call; merge by `(name, source_section)` de-dup. Hard cap at 4 chunks → graceful degradation if doc is huge. |
| LLM fixates on illustrative SQL examples (like `term_binding.py`'s problem) | Prompt explicitly lists Example / Use Case / SQL: / Explanation as skip-categories. Post-extract: drop any term whose `source_section` matches `(?i)example\|use\s*case\|sample`. |
| Cost spike on Phase 2 (~400 tasks) | One call per task → 400 × ≈$0.04 = ~$16 per submission. Cache hits free on retries. Within submission Docker budget (no network at runtime — caches must be pre-warmed; see §5 global cache). |
| KDD submission has no network | Pre-warm cache during build. Add a `scripts/warm_doc_glossary.py` step in `Dockerfile` build stage that runs B2 on all phase-1 / phase-2 task dirs and bakes the cache into the image. Runtime hits cache only. |
| Question-matcher false positives drown signal | Threshold `score ≥ 1.0` + 12-line cap + compressible section. Marked compressible so over-broad hints get trimmed under budget. |
| Schema drift between calls | `schema_version` constant in prompt + cache key; bumping forces cleanest refresh. |
| Glossary entries override correct deterministic schema | Section priority places harness_feedback (96) and judge_guidance (94) above doc_glossary_hints (92). Manifest is non-compressible at 90, so it survives even when glossary is trimmed. Anchors guide; manifest decides. |
| Concurrent task runs corrupt cache | Atomic `tempfile + os.replace`; reads are O_RDONLY. Verified in unit test. |
| Privacy: cache files contain doc text echoes | `.dataline_cache/` is `.gitignore`d. Global cache lives under user-controlled path. No external transmission beyond the configured MODEL_API_URL. |

## 9. Open questions

1. **Should B2 emit hints when the doc carries ONLY rules / formulas and
   zero defined terms?** (e.g., docs that are pure best-practice prose.)
   Current design: yes — formulas and rules surface independently. Risk:
   noise on docs like the back half of `manual.md` (best-practice prose).
   Mitigation already in place via `rules` filtering, but worth measuring.

2. **Multi-file relative-path resolution.** When the doc says
   "see `fees.json`" but the manifest holds the file under a nested
   path, should the matcher resolve and surface the file path? Proposed:
   yes — match by basename, surface full manifest path in the hint.

3. **Synonym injection.** Should glossary synonyms be merged into the
   focus_hints aliasing layer (so e.g. `extract_entities` knows
   `Disease ≡ Diagnosis`)? Proposed: yes, but as a follow-up — keeps B2
   landing PR-sized.

4. **Sectional priority vs. inline injection.** Today B2 is its own
   section. Alternative: extracted terms feed directly into the existing
   `domain_rules` summary so the LLM sees them inline with prose. Risk:
   bloats domain_rules section, harder to debug. Proposed: keep separate
   section now; revisit after measurement.

5. **When the manifest is empty (doc-only tasks).** The LLM still gets
   the doc, but `data_field` bindings will all be `null`. Hints will
   carry definitions and value enums only — still useful for
   reading-comprehension-style questions. Confirm via the held-out
   synthetic set.

6. **Should the matcher emit value-enum mismatch flags?** e.g., if the
   question asks about "POS transactions" and the glossary says
   `shopper_interaction` enum includes `POS`, today we emit a term hint;
   we could also emit a stronger anchor that says "filter
   `shopper_interaction = 'POS'`". This crosses into PlannerCoder
   territory — proposed: leave the planner to make that leap; the hint
   provides the materials.

7. **Cache key sensitivity to model swap.** Current key includes
   `model_name`. If we hot-swap models mid-submission, caches invalidate
   per-task on first run. Acceptable cost given submission cadence; flag
   if this becomes a problem.
