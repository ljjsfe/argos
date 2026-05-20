You extract a STRUCTURED GLOSSARY from one or more domain documentation files.

Goal: produce JSON usable by a downstream code generator that answers
questions over the dataset described by these docs.

What to include:
- Domain terms with their definitions (one entry per term).
- When the doc names a data column / field / table for a term, record it
  under `data_field`.
- When the doc enumerates allowed values for a term, record each value
  and its meaning under `value_enum`.
- When the doc gives a numeric range / unit, record `value_range`.
- Business / computation rules and named formulas: record under `rules`
  and `formulas`. Inputs must reference term names you also emitted.
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
- Follow the schema exactly. Use `null` / `[]` for missing fields, never
  omit a key.
- Term `name` should be the canonical form used in the doc.
- Do NOT invent columns or tables the doc does not mention.
- Cap: at most 80 terms, 30 formulas, 30 rules, 30 synonyms.
- Each `definition` ≤ 240 chars. Each `expression` ≤ 200 chars.
- If the doc contains no domain content (only contact info /
  boilerplate), return:
  `{"schema_version":"b2.v1","source_files":[...],"terms":[],"formulas":[],"rules":[],"synonyms":[]}`.

Schema:

```json
{
  "schema_version": "b2.v1",
  "source_files": ["context/knowledge.md"],
  "terms": [
    {
      "name": "string",
      "aliases": ["string", "..."],
      "definition": "string ≤ 240 chars",
      "data_field": {"table_or_file": "string or null", "column": "string or null"},
      "value_enum": [{"value": "string", "meaning": "string"}],
      "value_range": {"min": 0, "max": 0, "unit": "string"} ,
      "source_section": "string"
    }
  ],
  "formulas": [
    {"name": "string", "expression": "string ≤ 200 chars",
     "inputs": ["term_name", "..."], "source_section": "string"}
  ],
  "rules": [
    {"statement": "string", "applies_to": ["term_name", "..."],
     "source_section": "string"}
  ],
  "synonyms": [
    {"a": "string", "b": "string", "note": "string or null"}
  ]
}
```

Output the JSON object now.
