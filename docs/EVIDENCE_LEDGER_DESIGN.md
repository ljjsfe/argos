# Evidence Ledger — Architecture Design (#1)

**Status**: DESIGN (no code yet). Pre-implementation review doc.
**Author**: dataline iteration session 2026-05-20
**Type**: Architectural refactor — no LLM-behavior change, no direct KDD score lift expected.
**Inspired by**: Tianfu-style multi-tool architecture; user-recommended "highest-leverage deepening opportunity".

---

## 1. Problem statement

Currently, every "evidence emitter" that augments the Planner's prompt
is bespoke. Adding new tools makes it worse. Concrete snapshot from
`dataline/agents/orchestrator.py` (lines 297-352):

| Emitter | Output shape | Injection point | Trace channel |
|---------|--------------|-----------------|---------------|
| **A1 focus_hints** | newline-joined string of `entity → manifest hit` lines | `state.focus_hints`, eventually pasted into manifest_json text | `obs["focus_hints_lines"]` |
| **A2 term_binding** | newline-joined `DOMAIN_BINDINGS` lines | `state.domain_bindings` | `obs["domain_bindings_lines"]` |
| **B2 doc_glossary hints** | compact glossary text (terms + formulas + rules) | `state.doc_glossary_hints` | `obs["doc_glossary_terms"]` |
| **Block 4 narrative virtual tables** | summary lines appended to `manifest_json` body | inline string concat | `obs["profiler"]["narrative_extracted"]` |

Observed problems:

1. **N+1 problem on every new tool**: a hypothetical SQL multi-candidate scorer, an XSV header sniffer, or any future Tianfu-style tool would add a 5th, 6th, 7th `set_*` field + state attribute + trace key + injection site.
2. **No common metadata**: each emitter independently decides whether to log token cost, whether to expose confidence, whether to participate in budget compression. There is no Evidence-level handle for "this hint cost $0.02 and the LLM didn't actually use it" telemetry.
3. **Ablation requires per-emitter flags**: today only A2 has `DATALINE_DISABLE_A2=1`. Adding ablation for the others means adding more env vars.
4. **Prompt priority is implicit**: where each emitter slots into the final prompt is hardcoded by injection ordering, not by an explicit priority the ContextManager can re-rank.
5. **Trace attribution is shallow**: post-eval, asking "did the focus_hints text get truncated when budget tight?" requires re-parsing trace JSON across multiple keys.

`ContextManager.Section` is the existing abstraction, but it only models *the final assembled prompt blocks*, not the *evidence pipeline* that produces them. Evidence Ledger sits one layer above: it collects evidence, then *renders* evidence into Sections.

---

## 2. Goal & non-goals

### Goal

A single typed pipeline for any agent that emits prompt-facing hints:

```
emitter → Evidence record → EvidenceLedger → ContextManager.Section → prompt
                                  ↓
                              trace + ablation
```

### Non-goals (explicitly out of scope for this commit)

- **Changing LLM behavior**. Initial migration must produce byte-identical prompts (modulo cosmetic ordering) so paired-test risk = near-zero.
- **Adding new evidence types**. Only A1 / A2 / B2 / narrative_extracted get ported.
- **Cost/confidence-driven dynamic selection** (that's #4 Planner Context Policy — depends on this commit but lives in a separate PR).
- **Trace telemetry overhaul**. We expose the hooks; we don't redesign dashboards.

---

## 3. Type design

```python
@dataclass(frozen=True)
class Evidence:
    """One piece of evidence to optionally inject into the Planner prompt.

    Emitted by an evidence source (A1/A2/B2/Block-4/future tools), held
    by EvidenceLedger, eventually rendered as a ContextManager.Section
    or discarded under budget pressure.
    """
    source: str                # "focus_hints" / "domain_bindings" / "doc_glossary" / "narrative_tables" / ...
    payload: str               # human-readable text to embed in prompt
    priority: int = 50         # 0-100, higher = preserved longer under budget pressure
    confidence: float = 1.0    # 0.0-1.0, emitter's self-rated confidence
    cost_tokens: int = 0       # tokens spent producing this (0 if deterministic)
    risk_class: str = "additive"  # "additive" (safe) | "subtractive" (variance risk)
    trace_tag: str = ""        # extra info for trace (e.g. "n_hits=3, fuzzy=0")
    section_name: str = ""     # explicit Section name (defaults to source)
    section_heading: str = ""  # optional markdown heading override
    compressible: bool = True  # passthrough to Section
```

**Invariants**:
- `source` must be one of the registered emitters (validated at ingest).
- `payload` non-empty after `.strip()` — empty evidence is silently dropped (avoids cluttering ledger).
- `priority ∈ [0, 100]`. Default 50 matches today's `Section` default.
- `risk_class` defaults to `"additive"`; any emitter that mutates Planner reasoning paths (none today) must explicitly set `"subtractive"`. Future cost/confidence policy can use this to gate.

```python
class EvidenceLedger:
    """Per-task collection of Evidence. Single source of truth between
    emitters and ContextManager.

    Workflow:
      1. Orchestrator constructs ledger at task start (empty).
      2. Each evidence emitter (A1/A2/B2/Block-4) calls
         `ledger.add(Evidence(...))` instead of mutating state fields.
      3. Before Planner invocation, orchestrator calls
         `ledger.to_sections()` to get a list[Section] for ContextManager.
      4. Trace observability via `ledger.summary()` (counts, costs, what
         got compressed). Stored in obs["evidence_ledger"].
    """

    def __init__(self) -> None: ...
    def add(self, ev: Evidence) -> None: ...
    def get(self, source: str) -> list[Evidence]: ...
    def to_sections(self) -> list[Section]: ...
    def summary(self) -> dict[str, Any]: ...
    def is_disabled(self, source: str) -> bool: ...  # checks DATALINE_DISABLE_<SOURCE>=1
```

---

## 4. Migration map

Each existing emitter migrates to the new API:

| Today | Tomorrow |
|-------|----------|
| `state = set_focus_hints(state, _fh)` | `ledger.add(Evidence(source="focus_hints", payload=_fh, priority=70))` |
| `state = set_domain_bindings(state, _db)` | `ledger.add(Evidence(source="domain_bindings", payload=_db, priority=72))` |
| `state = set_doc_glossary_hints(state, _dg)` | `ledger.add(Evidence(source="doc_glossary", payload=_dg, priority=80, cost_tokens=spent))` |
| Block 4: `manifest_json += "\n\n## Virtual tables\n..."` | `ledger.add(Evidence(source="narrative_tables", payload="...", priority=90))` |

PlannerCoder's prompt assembly switches from "read state.* fields and string-concat" to "read sections produced by ledger.to_sections() and pass to ContextManager.assemble()".

**state.py keeps the legacy fields** (focus_hints / domain_bindings / doc_glossary_hints) for the first commit — they get populated AS WELL AS the ledger. This dual-write phase lets us prove byte-identical prompts before removing the legacy path. Phase-out happens in a follow-up commit.

---

## 5. Priority normalization

Current priorities are scattered across modules. New canonical scale (just documentation, no behavior change):

| Source | priority | risk_class | compressible |
|--------|---------|------------|--------------|
| `narrative_tables` | 90 | additive | True |
| `doc_glossary` | 80 | additive | True |
| `domain_bindings` (A2) | 72 | additive | True |
| `focus_hints` (A1) | 70 | additive | True |
| _(future)_ `sql_value_search` | 75 | additive | True |
| _(future)_ `metric_lookup` | 78 | additive | True |

This sits between current `data_profile` (50) and `domain_rules` (80). Locks an explicit ordering doc; emitters cite the canonical priority.

---

## 6. Backward compatibility & rollout

### Phase 1 (this design's scope — single commit)

- Add `Evidence` + `EvidenceLedger` to `dataline/core/evidence_ledger.py` (new file).
- Wire ledger into orchestrator. Each emitter call also pushes to ledger.
- PlannerCoder reads from ledger if present, falls back to state fields otherwise.
- Test: assemble byte-identical prompt with and without ledger active (legacy path still feeds state too).
- Trace: `obs["evidence_ledger"] = ledger.summary()`.
- Gate: `DATALINE_EVIDENCE_LEDGER=1` env var enables the new path. **Default OFF** initially.

### Phase 2 (separate PR after Phase 1 paired test passes)

- Default ON.
- Remove dual-write — emitters only push to ledger.
- Remove legacy state fields after one full eval verification.

### Phase 3 (future, not in this design's scope)

- #4 Planner Context Policy: ledger consumer that selects evidence by confidence + cost.
- #3 Tool Adapter Layer: input-side analog (adapters produce Evidence directly).
- New tools (SQL multi-candidate, metric lookup, etc.) plug in via Evidence emit.

---

## 7. Test plan

### Unit tests (`test_evidence_ledger.py`, ~15 cases)

- Evidence dataclass invariants (priority bounds, empty payload drop, etc.)
- Ledger add / get / to_sections happy path
- Multiple sources merged correctly
- DATALINE_DISABLE_<SOURCE> respected
- summary() returns expected shape
- Section conversion preserves heading + compressible + priority

### Integration tests (extend `test_state.py`)

- Orchestrator path A: dual-write produces identical state + ledger contents
- Orchestrator path B: Planner prompt assembled from ledger ≡ Planner prompt assembled from state (byte-identical modulo whitespace)

### Smoke test (5-task subset)

Standard mixed subset (`task_11, task_330, task_169, task_257, task_38`):
- Run with `DATALINE_EVIDENCE_LEDGER=0` (legacy). Baseline.
- Run with `DATALINE_EVIDENCE_LEDGER=1` (new path). Compare per-task scores.

**Pass gate**: per-task scores match within ±0.5 (LLM variance). No regression > 0.5 per task. Both arms produce same prompt bytes for ≥4 tasks (sanity check on assembly equivalence).

**Fail gate**: any per-task score regresses ≥1.0, OR prompt byte-difference seen on >1 task that's not pure whitespace.

### No paired test required for this phase

Because the new path is gated OFF by default and the dual-write keeps old behavior identical — production is byte-identical. The smoke test above is sufficient.

---

## 8. Predictions (predict-then-verify receipt)

| Metric | Predicted | Falsification threshold |
|--------|-----------|-------------------------|
| Smoke test per-task delta (legacy vs new, gate=ON) | 0 ± 0.5 LLM variance | ≥1.0 regression on any task |
| Prompt byte-equivalence (legacy ≡ new) | 4-5/5 tasks identical | <3/5 → assembly bug |
| Test count growth | +15 unit tests, +2 integration | — |
| File diff size | ~400 lines (+evidence_ledger.py, +tests, ~50-line orchestrator touchups) | >800 → over-scoped |
| KDD full eval impact (if we ran v100) | 0 ± 0.04 (gate off) / 0 ± 0.06 (gate on) | regression >0.08 → bug |

---

## 9. Risk register

| Risk | Mitigation |
|------|------------|
| Refactor breaks existing trace structure | Dual-write preserves all existing `obs["focus_hints_lines"]` etc.; add `obs["evidence_ledger"]` alongside |
| Section priority shift accidentally re-orders prompts | Pin priorities to current values (see §5); add prompt-byte test |
| New ledger code adds latency | Pure-Python, no LLM calls, no I/O — overhead is negligible (<1ms per task) |
| Future emitter authors misuse priority | Section §5 documents canonical scale; CLAUDE.md Rule-design discipline gets an addendum |
| Phase 2 (default ON) introduces regression | Phase 2 will run full v101 paired test before flipping default |

---

## 10. Why this design vs alternatives considered

### Alternative A: Skip ledger, just refactor existing fields into one big dict

Faster but reproduces the N+1 problem in a different shape. Doesn't enable confidence/cost gating or future tool plug-in. Rejected.

### Alternative B: Make ContextManager.Section the evidence carrier

`Section` lives in `core/`, used by callers other than evidence (e.g., `manifest_json`, `domain_rules` body). Coupling evidence-specific metadata (confidence, cost, risk_class) into Section pollutes the general API. Keep Section as the *render output* of the ledger; Evidence as the *input record*. Standard separation.

### Alternative C: Full event-bus + middleware stack

Tianfu / NexAU style. Too much for our 3000-line codebase. Premature. Ledger is the *minimal* version of this pattern that gives us the plug-in extensibility without the framework weight.

---

## 11. Open questions (for user review BEFORE implement)

1. **Phase 1 gate default OFF?** — Reduces ship risk to zero (legacy unchanged). Acceptable to leave OFF for 1-2 sessions while we accumulate evidence, then flip ON?
2. **Drop `state.focus_hints / domain_bindings / doc_glossary_hints` fields in Phase 2?** — Yes/no.
3. **`risk_class` enum or string?** — Current proposal: string with `{"additive", "subtractive"}` only. Future-proof; not strongly typed but matches our other style.
4. **Confidence default 1.0?** — Or default `0.0` to force emitters to explicitly set it? 1.0 is friendlier for migration (existing emitters don't break). I'd vote 1.0.
5. **One ledger per task or per task+iteration?** — Per task (today's behavior). Iter-level lives in PlannerCoder iter state, not the ledger.

---

## 12. Next step

If approved:
1. Create `dataline/core/evidence_ledger.py` with Evidence + EvidenceLedger types.
2. Add `test_evidence_ledger.py` (~15 unit tests).
3. Orchestrator dual-write integration.
4. PlannerCoder ledger-reader path.
5. Smoke test on 5-task subset.
6. Commit + push + 2 sentence note to user.

If rejected:
- Drop. We stay at current ship floor 0.69, current architecture, and continue adding string emitters case-by-case. (Acceptable in short term; gets painful when 3+ new tools land.)
