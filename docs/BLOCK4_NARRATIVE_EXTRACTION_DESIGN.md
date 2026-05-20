# Block 4 — Narrative Markdown → Structured Table Adapter

**Status**: DESIGN (no code yet)
**Author**: dataline iteration session 2026-05-20
**Goal**: Make narrative-document tasks (task_418, 349, 173, 379) work by giving Planner pre-extracted structured DataFrames instead of forcing it to write fragile regex.
**Why now**: v95 trace audit found 4 capability blocks. Blocks 1/2/3 require prompt-class changes (v94+G2 proved high-risk on Qwen 3B-active). Block 4 is architectural-deterministic, no LLM-behavior path shift risk.

---

## 1. Problem statement

In v95 the planner had to regex-extract structured values from narrative markdown:

```
The case file for Patient 763253 presents a male subject born on January 2nd, 1941...
His creatinine level was measured at 1.5 mg/dL on a recent visit.
```

For `task_418` ("how many patients with abnormal creatinine aren't 70 yet?") the Planner wrote:

```python
for match in re.finditer(
    r'patient\s+(\d+).*?(?:Creatinine|CR|cr)[\s]*[:=]?\s*([\d.]+)\s*(?:mg/dL|mg/DL|mgdl)',
    lab_content, re.IGNORECASE | re.DOTALL):
```

This regex is brittle. Different sentence structures, different unit abbreviations, line breaks — all blow it up. **Result: 49 vs gold 1.**

Same shape in task_349 (find major from doc), task_173 (countries from gas-station narrative), task_379 (4th atom of molecules).

**Common pattern across all 4 failures**: narrative document → planner writes ad-hoc regex → extraction fails.

---

## 2. Solution space

### Option A — Pre-extract structured records via LLM at Profiler stage (RECOMMENDED)

Profiler detects narrative-shaped documents (no markdown tables, prose paragraphs with repeated entity mentions) → one LLM call → emit a structured DataFrame as a virtual data source registered in DuckDB.

**Pros**:
- Universal — works for any domain (patients, products, schools, molecules)
- One-shot extraction cost amortized across all tasks using that doc
- Planner sees it as a regular table; no special API needed
- Cached by content hash (like B2 doc_glossary)
- LLM extraction is the ONE place we accept LLM variance — but it's deterministic-cached, not in the planning hot path

**Cons**:
- Additional LLM call (~$0.10 per narrative doc, cached)
- LLM extraction may miss edge fields
- Requires LLM to infer schema (column names + types) from prose

### Option B — Pattern library of helpers

Add `data_helpers.extract_patient_records(text)`, `extract_lab_values(text, analytes)` etc.

**Cons**:
- Each pattern is domain-specific (anti-universal)
- Planner has to discover and use them
- Doesn't generalize beyond the patterns we hand-write

### Option C — Hybrid hint

Profiler detects narrative shape, emits a hint like "this doc contains records of type X with fields Y, Z" to Planner. Planner still writes the regex but with better targeting.

**Cons**:
- Still leaves regex authorship to Planner (the failing step)
- Hint is prompt-class content → same LLM variance risk as G2

**Recommendation**: **Option A**. Universal, cacheable, removes regex authorship from Planner.

---

## 3. Adapter API

### New profiler module: `dataline/profiler/narrative_extractor.py`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ExtractedSchema:
    table_name: str           # e.g. "patient_records_from_Patient_md"
    columns: tuple[str, ...]  # e.g. ("patient_id", "sex", "birthday", "creatinine_mg_dl")
    row_count: int
    csv_path: str             # path under TEMP_DIR where extracted CSV is written


def detect_narrative_shape(md_text: str) -> bool:
    """True if doc looks like prose with repeated entities, NOT a glossary
    or a markdown table-heavy doc.

    Heuristic checks (all must hold):
    - No markdown tables (or table-row ratio < 5%)
    - ≥ 3 paragraphs of prose (consecutive non-empty lines without ** or `)
    - At least one repeating entity reference (e.g. 'patient X', 'molecule Y',
      'transaction Z') — detected by pattern (entity_word \\d+).
    """
    ...


def extract_records(md_text: str, llm) -> ExtractedSchema | None:
    """One LLM call to extract structured records as JSON, then write to CSV.

    Prompt asks LLM to:
    - identify the repeating entity (patient / molecule / etc.)
    - list the fields available per entity
    - emit one row per entity instance as JSON

    Cached by content hash (same as B2 _cache_key pattern).
    Returns None if extraction fails (e.g. doc isn't actually narrative).
    """
    ...
```

### Integration into manifest scan

In `dataline/profiler/manifest.py:read_markdown()`:

```python
def read_markdown(file_path: str) -> ManifestEntry:
    text = open(file_path).read()
    base_entry = ... # existing headings/key_terms extraction

    if detect_narrative_shape(text):
        extracted = extract_records(text, llm)  # ONE LLM call, cached
        if extracted:
            # Augment summary with virtual table info
            base_entry.summary["narrative_extracted_as"] = {
                "table": extracted.table_name,
                "columns": extracted.columns,
                "rows": extracted.row_count,
                "csv_path": extracted.csv_path,
            }
            # Register the CSV with DuckDB so Planner can SELECT from it
            ...
    return base_entry
```

### Planner-side change

**Minimal**: the new virtual table appears in the manifest just like any CSV. Planner already knows how to SELECT from a CSV view in DuckDB. No prompt change required.

**Optional follow-on**: extend Profiler to inject a one-line hint in the manifest summary: "Patient.md was extracted into virtual table `patient_records_from_patient_md` (5 rows, columns: patient_id, sex, birthday, creatinine)". This is still in the manifest body, not the planner prompt itself.

---

## 4. Extraction prompt design

The narrative-extraction LLM call is the new LLM hop. Universal prompt:

```
You are extracting structured records from a narrative document.

The document below describes multiple instances of a single entity type
(e.g., patients, products, transactions, molecules). Your task:

1. Identify the entity type (one word, e.g., "patient", "molecule").
2. List the fields documented for each instance. Include ONLY fields that
   appear with concrete values (skip generic descriptions).
3. For each instance, emit one JSON object with the fields you identified.
   Use null for missing values.

Output ONLY valid JSON with this shape:
{
  "entity_type": "patient",
  "schema": [{"name": "patient_id", "type": "integer"}, ...],
  "records": [{"patient_id": 43003, "sex": "male", ...}, ...]
}

Document:
<text>
```

**Properties**:
- Universal — no domain-specific instructions
- Deterministic-shaped output (JSON schema for downstream parsing)
- One-shot — no multi-turn

**Failure mode handling**: If LLM produces invalid JSON or zero records → skip extraction, fall back to current text-only manifest entry. Logged but not surfaced to Planner.

---

## 5. Paired-test plan

Same shape as G2 but for an architectural change, not a prompt change. Variance risk is much lower because the LLM call is deterministic-cached.

### Target set (5 tasks — should improve)

| task | difficulty | v95 score | Failure mode |
|------|-----------|-----------|--------------|
| task_418 | extreme | 0.00 | Creatinine extraction from Patient.md / Laboratory.md |
| task_349 | hard | 0.00 | "major" extraction from doc |
| task_173 | medium | 0.00 | Countries from gas-station narrative |
| task_379 | hard | 0.00 | 4th atom per molecule from molecule doc |
| task_344 | hard | 0.00 | Indirect: uses Laboratory.md for thresholds (partial benefit) |

**Targeted lift expected**: 2-3 tasks (extraction quality drives this — LLM might miss edge fields). Less optimistic than G2's 2-3 because some failures are about downstream reasoning, not extraction.

### Control set (5 tasks — must stay perfect)

Tasks where narratives don't apply (pure-SQL tasks):
- task_11, task_25, task_38, task_140 (if present), task_283 — same as G2

**Failure criterion**: more than 1 control task drops below v95 baseline by ≥0.5.

---

## 6. Predictions (predict-then-verify receipt)

| Metric | Predicted | Falsification threshold |
|--------|-----------|-------------------------|
| Target net delta | +2.0 to +3.5 (out of 5 possible) | < +1.0 → adapter not helping |
| Control net delta | -0.5 to +0.5 (within noise) | < -1.0 → architectural change broke something |
| New LLM cost per narrative doc | $0.05-$0.15 (cached after first run) | > $0.50 → prompt too long |
| Profiler runtime impact | +5-10s per task with narratives | > 30s → optimize extraction prompt |

---

## 7. Risk assessment

| Risk | Mitigation |
|------|------------|
| LLM extraction misses fields the task needs | Schema discovery is LLM-driven; if extracted columns don't match task need, manifest summary lists available columns so Planner can fall back to text |
| Profiler now depends on LLM (more failure surface) | Failure path falls back gracefully to current text-only manifest entry; never blocks task |
| LLM hallucinates rows | Conservative prompt; we cache only successful extractions; manifest entry includes provenance ("extracted from Patient.md") |
| Cost balloon if narrative doc is huge | Truncate input to ~10K chars; emit partial extraction with explicit warning column |
| Hash collision in cache (B2-style fix) | Use content-only SHA256 hash, no path; same fix as B2 v95 commit |

---

## 8. Implementation steps

1. **Module skeleton** (~2h): create `dataline/profiler/narrative_extractor.py` with `detect_narrative_shape()` + `extract_records()` + cache. Unit tests for shape detection.
2. **LLM prompt + JSON parsing** (~2h): write extraction prompt, validate JSON, write CSV output, register with DuckDB.
3. **Integrate into `markdown_reader.py`** (~1h): hook into existing `read_markdown()`, augment summary, fall back on failure.
4. **Unit tests on 3 synthetic narratives** (~1h): patient-style, molecule-style, gas-station-style.
5. **Paired-test execution** (~1h, $16): per Section 5.
6. **If pass**: full v97 KDD eval to verify scale (~25min, $22).

Total: ~7-8 hours of work, ~$40 LLM cost (paired + full eval).

---

## 9. Decision matrix

Same shape as G2:

| Target net (5 tasks) | Control net (5 tasks) | Decision |
|----------------------|----------------------|----------|
| ≥ +2.0 | ≥ -0.5 | SHIP → full v97 |
| +1.0 to +2.0 | ≥ -0.5 | AMBIGUOUS — run paired with different seed before ship |
| < +1.0 | any | REJECT — adapter doesn't help, revert |
| any | < -0.5 | REJECT — architectural change broke control |

---

## 10. Why this differs from prompt-class interventions (v94, G2)

| Dimension | Prompt-class (v94, G2) | This (Block 4 adapter) |
|-----------|------------------------|------------------------|
| Where the change lives | Planner's prompt at every iter | Profiler's one-shot extraction at task start |
| Iteration-path impact | Direct: shifts Planner reasoning chain | None: Planner sees same data shape, just fuller |
| LLM variance amplification | High (Planner re-reasons every iter) | Minimal (cached, deterministic at task scope) |
| Falsification cost | $22 per full eval | Same |
| Rollback cost | git revert prompt | git revert + delete cached CSVs |
| Why v94/G2 failed | Prompt structure shift on small model | N/A — no prompt change here |

This is structurally a SAFER class of intervention than prompt-tweaking, even though it adds one LLM call.

---

## 11. Open questions for user review

1. **Caching scope**: cache per (doc_content_hash + extraction_prompt_version)? OK as designed?
2. **DuckDB virtual table naming**: auto-generated from file basename, or LLM-suggested? Auto safer.
3. **Schema inference**: should the LLM also emit suggested types (int/float/date/text)? Yes — DuckDB CSV inference is robust but explicit hints help.
4. **Failure visibility**: should extraction failures be flagged to user / logged loudly, or silent-fallback? Silent-fallback recommended to avoid noise on docs that genuinely shouldn't be extracted (e.g. knowledge.md).

---

## 12. Next step after this design

Two options:

**A**: Implement steps 1-4 (~6h), then paired-test (~1h, $16). Decision per Section 9.
**B**: Skip implementation; run a clean **v97 full eval on current ship** (`ef02ebd`) to lock in the current baseline measurement before any new work. Then decide whether to invest the 6h.

User signaled "需要完整的测试 才知道where we are" → option B is the right immediate next step. The design above documents what we'd do IF v97 confirms there's still ROI to chase.
