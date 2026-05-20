# Doc Format Audit — KDD vs DABstep

**Captured**: 2026-05-19
**Purpose**: assess whether A2's bold-prefix markdown parser is universal
or KDD-knowledge.md-specific.

## KDD 50-task knowledge.md format census

Scan: `public/input/task_*/context/knowledge.md` + `context/doc/*.md`.

| Pattern | Tasks containing it | % |
|---|---|---|
| `## N. Heading` (heading-section + prose) | 50 | **100%** |
| `- **term**:` (bold-prefix term defs) — what A2 parses | 35 | 70% |
| `term — definition` (em-dash glossary) | 2 | 4% |
| `- \`column_name\`:` (backtick code) | 1 | 2% |
| `- **term (type):**` (paren-type) | 0 | 0% |
| Markdown table glossary `| term | def |` | 0 | 0% |

- Tasks where A2's pattern (`- **term**:`) covers entire doc: **0**
- Tasks with mixed patterns: 36/50
- Tasks where A2 finds NOTHING in doc: **15 (30%)**

## DABstep doc format

- `context/manual.md`: heading-section + prose. NO bold-prefix terms. ~3k words of glossary embedded in flowing text.
- `context/payments-readme.md`: hybrid — has `- **Description**:` (which A2 currently filters out!) and `- \`col_name\`:` (which A2 doesn't match).

A2 on DABstep predicted behavior: **near-zero useful bindings, plus a false-negative on the one legitimate `**Description**:` term it would have wrongly filtered.**

## Verdict

**The universal cross-benchmark pattern is heading-section + prose.** Bold-prefix term defs are a KDD-style convention used by ~70% of KDD tasks and almost no other benchmark.

A2's current implementation is fundamentally style-tuned to KDD's 70% subset. It cannot generalize to:
- The 30% of KDD tasks that don't use bold-prefix terms
- DABstep's heading + prose style
- Any benchmark using a different doc convention

## Recommended path forward

Replace A2's pattern-matching with a doc-format-agnostic mechanism. Options
ranked by recommendation strength:

1. **B2 (chosen)**: One LLM call per task to extract a structured glossary
   from raw doc text. Works across any doc format. Cost ~$0.05/task =
   $2.5 per 50-task eval.
2. **B4**: Profiler-doc integration — Profiler reads doc + binds each
   column to its description. Zero LLM, but bigger code change.
3. **B3 (deferred)**: RAG with embeddings. Needs embedding infra; deferred.
4. **B1**: Drop A2 entirely. Accept that doc parsing is too brittle.

B2 will be designed by an independent agent (no exposure to KDD eval
results or task data) to avoid the same KDD-style bias that the current
A2 has.
