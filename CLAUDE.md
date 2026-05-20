# dataline — Project Context for Claude

## What This Is

`dataline` is a general-purpose data analytics agent. Given heterogeneous data files (any format) and a natural language question, it reasons across those files and returns a structured answer.

```bash
python main.py --task ./data/demo/input/task_11 --output ./results/task_11
```

Output: `prediction.csv` + `trace.json`

**Core goal**: A general data agent that handles complex heterogeneous data scenarios — not tied to any single benchmark or approach.

**Proving grounds**: KDD Cup 2026 (structured tabular) + DABstep (financial payments). These validate the core loop but don't cover the full vision (multi-modal, large-scale, dirty data).

---

## Architecture: Infrastructure-First, Thin LLM

**Core principle: push complexity from LLM to infrastructure.** Every piece of data understanding that can be done deterministically should be. The LLM only handles what requires natural language reasoning.

This principle emerged organically: v13 had 11 LLM calls where Analyzer/Judge/QuestionAnalyzer were compensating for infrastructure gaps (Profiler computed DISTINCT values but compress_manifest discarded them). Fixing the infrastructure (rich schema passthrough, JSON registration, SQL detection) made those LLM calls redundant — each removal was a consequence of the layer below getting better, not a design copy from elsewhere.

```
Data Sources (any format)
    │
Adapter Layer (pluggable readers per format)
    │  ← detect → parse → register → profile
    │
Profiler (deterministic, zero LLM cost)
    │  → rich schema: columns, types, DISTINCT values, sample rows, relationships
    │  → cross-name FK discovery via value overlap (join_graph)
    │  → domain rules from documentation files
    │
QuestionSpec (deterministic, zero LLM cost)
    │  → regex/heuristic → answer_type, row_count, computation_type, tie_possible
    │
TaskRouter (deterministic, zero LLM cost)
    │  → single_sql / multi_sql / python_extract / document_needed / general
    │  → hint injected into PlannerCoder context
    │
PlannerCoder (1 LLM call)
    │  → question + rich schema + task_mode + judge_guidance → executable code
    │  → multi-candidate: 2-3 candidates, first success wins
    │
Sandbox (dual-engine execution)
    │  → Structured data: DuckDB — CSV/JSON/SQLite/Parquet as views, unified SQL
    │  → Non-structured data: Python — pandas, specialized libraries
    │  → Language auto-detected; PlannerCoder chooses, Sandbox dispatches
    │  → Code failure (rc!=0) → skip to next iteration (never accept failed code)
    │
HarnessGate (deterministic verification, zero LLM cost)
    │  ├─ block → feedback to PlannerCoder, retry (skip Judge)
    │  ├─ warn (first iteration) → soft retry once with guidance
    │  ├─ repeated WARN ≥3x (whitelisted rules) → escalate to BLOCK
    │  ├─ SQL static analysis via sqlglot (join keys, WHERE values, column count)
    │  └─ pass/warn → continue to Judge
    │
Judge (1 LLM call, semantic verification)
    │  ├─ finish → accept result
    │  ├─ continue → retry with guidance
    │  └─ backtrack → restart from earlier step
    │
Finalizer (1 LLM call) → prediction.csv + trace.json
```

**LLM calls per task:**
- **Baseline mode**: 3-5 (PlannerCoder + Judge + Finalizer, +retries)
- **Heavy mode (failure-triggered)**: 3K + 1 deliberator, where K=3 trajectories run in parallel at temperatures `[0.0, 0.7, 0.7]`. Triggered when baseline `is_confident()` returns False. See "Heavy Mode" below.

### Context Management

ContextManager (CM) assembles the PlannerCoder prompt within token budget:
- Sections ranked by priority: question (100) > harness_feedback (96) > judge_guidance (94) > manifest (90) > domain_rules (80) > prior_steps (60)
- Over-budget sections are compressed (smart_truncate or LLM summarize)
- Domain rules compiled when exceeding budget fraction (large docs)
- Scales to large manifests and multi-iteration history without manual tuning

### Heavy Mode (failure-triggered K-trajectory ensemble)

Wraps `orchestrator.run_task` in `heavy_runner.run_task_heavy` (`dataline/agents/heavy_runner.py`).

1. **Baseline trajectory** runs at temp=0.0.
2. **Confidence check** — `heavy_confidence.is_confident()` reads judge action + harness state. If confident, return baseline result; no extras spawned.
3. **K-1 extra trajectories** spawn in parallel via `ThreadPoolExecutor` (default K=3, temps `[0.0, 0.7, 0.7]`). They share the baseline's manifest + compiled domain_rules to save tokens.
4. **Deliberator** (`heavy_deliberator.deliberate`) is 1 LLM call that picks among the K answers. Fallback when LLM parse fails: choose trajectory with longest non-empty CSV.

Config in `config.yaml` under `heavy_mode`. CLI flag `--heavy-mode`. Per-trajectory artifacts persisted under `<output_dir>__heavy_t<id>/` for debugging.

### Playbook (v59 framework, currently empty)

`dataline/playbook/data_analysis.yaml` holds question-pattern → guidance entries (id, type, trigger, action, evidence). `dataline/playbook/loader.py` retrieves by keyword overlap, formats as advisory hints into PlannerCoder context (priority between manifest and domain_rules).

**Currently `entries: []`.** Framework is fail-soft. We do NOT populate from external sources (e.g., KDD `learnings.json`) because most "universal-looking" rules are demo-shaped — see "Reverted Experiments" below. Only fill from our own eval-validated patterns.

### Extension Points

The architecture extends by adding **adapters**, not LLM complexity:
- Today: CSV, SQLite, JSON, Parquet, Markdown, PDF, DOCX, Excel, Image (with OCR fallback)
- Future: API connectors, streaming data, graph data
- Structured adapters register data as DuckDB views; non-structured adapters provide Python-accessible objects
- The core loop (profile → reason → execute → verify) stays the same

---

## Agent Roles

| Agent | File | Role |
|-------|------|------|
| Profiler | `dataline/profiler/manifest.py` | Deterministic scanning + rich schema extraction (zero LLM) |
| DomainRules | `dataline/agents/analyzer.py` | Extract business rules from docs (deterministic + optional LLM compilation) |
| PlannerCoder | `dataline/agents/planner_coder.py` | Plans + generates code (SQL/Python) candidates in ONE call |
| HarnessGate | `dataline/agents/harness_gate.py` | Deterministic verification (13 rules, zero LLM cost) |
| Debugger | `dataline/agents/debugger.py` | Fixes code using traceback + data context |
| Finalizer | `dataline/agents/finalizer.py` | Formats results → prediction.csv |
| Orchestrator | `dataline/agents/orchestrator.py` | Unified loop wiring all agents |
| HeavyRunner | `dataline/agents/heavy_runner.py` | Failure-triggered K-trajectory wrapper around `run_task` |
| HeavyConfidence | `dataline/agents/heavy_confidence.py` | Deterministic confidence check on baseline (judge action + harness signals) |
| HeavyDeliberator | `dataline/agents/heavy_deliberator.py` | 1 LLM call picking among K trajectories; longest-CSV fallback |
| Playbook | `dataline/playbook/loader.py` | Keyword-match retrieval of question-pattern guidance (entries currently empty) |
| CodeValidator | `dataline/agents/code_validator.py` | Pre-execution static checks on planner output |
| MetricMatcher | `dataline/agents/metric_matcher.py` | Match question to domain metrics/formulas (zero LLM) |

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Infrastructure > LLM calls | Fixing infra bugs was worth more than adding LLM calls — deterministic quality dominates |
| Rich deterministic profiling | DISTINCT values, sample rows, cardinality in schema → LLM doesn't need to "explore" data |
| DuckDB as unified query layer | CSV, JSON, SQLite, Parquet all registered as views → single SQL dialect for everything |
| SQL-first for structured data | Declarative and precise; LLM generates correct SQL at higher rate than pandas |
| PlannerCoder merged | Same reasoning process shouldn't be split — avoids info loss between plan→code |
| Multi-candidate output | LLM outputs 2-3 code candidates from ONE call; sandbox tries in order, first rc=0 wins. Cheap fallback for parse failures — NOT a true diversity mechanism (same reasoning, same temp). v6 disagreement-signal experiment proved candidates ~never disagree when both succeed |
| Heavy mode > planner multi-candidate | True diversity needs independent LLM calls. Heavy spawns K full pipelines at different temperatures, each with independent Plan + Code + Judge + Finalize. Triggered on baseline low-confidence to cap cost |
| Playbook stays empty until eval-validated | We do not seed it from external "universal rules" — most are demo-shaped. Only add entries with evidence from our own eval runs |
| HarnessGate (deterministic) | 16+ rules: BLOCK (nan per-column, empty, dict, embellishment, error, excuse) + WARN (shape, agg_type, columns, SQL static) + escalation |
| SQL static verifier (sqlglot) | AST analysis: check JOIN keys, WHERE literals vs DISTINCT, column count — 100% parse rate on DuckDB SQL |
| NaN per-column severity | All-null/key-null → BLOCK, non-key partial → WARN — fixes false-positive BLOCKs on legitimate NULL data |
| WARN escalation whitelist | Same WARN ≥3x → BLOCK for agg_type/extra_columns/join/qa_column/where_value only |
| Cross-name FK detection | Value overlap between differently-named ID columns → join_graph in manifest |
| Deterministic task routing | single_sql/multi_sql/python_extract/document_needed — hint injected, zero LLM |
| Judge (lightweight LLM) | 1 LLM call for semantic verification after HarnessGate PASS — data shows ~4% catch rate on 3B model, but architecturally correct |
| QuestionSpec (deterministic) | Regex/heuristic shape inference + column count estimation, zero LLM — enables HarnessGate shape rules |
| Code failure → retry | rc!=0 skips to next iteration — never accepts failed code output |
| No framework (no LangChain) | ~3000 lines of Python, no overhead |
| Immutable data types | Frozen dataclasses only, no mutation |
| Pluggable adapter layer | New data formats = new reader, not new agent logic |

---

## LLM Configuration

- **Eval model**: Qwen3.5-35B-A3B (MoE, 35B total / 3B active params, 262K context)
- **Env vars**: `MODEL_API_URL`, `MODEL_API_KEY`, `MODEL_NAME` (env vars override `config.yaml`)
- **Dev access**: DashScope — see `config.yaml`
- **Prompt constraint**: 3B active params = small model. Prompts must be concise, explicit, low-ambiguity. Complex multi-step instructions degrade fast.

---

## Development Workflow: Eval-First

The improvement loop is:
```
run eval → read diagnostics → identify bottleneck agent → fix → re-run eval
```

**Never** optimize without running eval first. The diagnostic output (`eval/diagnostics.py`) tells you exactly which agent is failing and why.

**Always commit before each eval run.** Every eval result must correspond to a known git SHA so we can attribute score changes to specific code versions and revert cleanly. Workflow:
1. Finish code change + tests pass
2. `git commit` with a descriptive message (`feat:` / `fix:` / `refactor:`)
3. Run eval with output dir tagged by version (e.g. `eval_v21_<topic>_<date>`)
4. Compare vs prior tagged run

No exceptions — even quick experimental runs commit first (use `wip:` prefix if uncertain).

Key eval commands:
```bash
# Run single task
python main.py --task ./public/input/task_11 --output ./results/task_11

# Run full KDD eval (50 tasks)
python eval/run_eval.py --data public --output results/eval_$(date +%Y%m%d)

# Compare two runs
python eval/compare.py results/eval_A results/eval_B
```

---

## File Structure

```
dataline/
├── core/          # types.py, llm_client.py, sandbox.py
├── profiler/      # manifest.py + readers (csv, sqlite, json, md, pdf, docx, excel, image, parquet)
├── agents/        # orchestrator, planner_coder, harness_gate, judge, debugger, finalizer,
│                  # heavy_runner, heavy_confidence, heavy_deliberator,
│                  # question_analyzer, metric_matcher, code_validator, analyzer
├── playbook/      # loader.py, tracker.py, data_analysis.yaml (entries: [] — see CLAUDE.md)
├── synthesizer/   # base.py, normalizer.py
├── prompts/       # .md prompt templates per agent
├── eval/          # scorer, run_eval, diagnostics, failure_analysis
└── tests/

public/            # KDD Cup Phase 1, 50 tasks (gold answers in public/output/)
data/
└── dabstep/       # DABstep benchmark (Adyen payments)

config.yaml        # LLM + agent + sandbox + heavy_mode + eval config
main.py            # CLI entry point (supports --heavy-mode)
submit_main.py     # KDD Docker submission entry point (LPT-scheduled)
Dockerfile         # Submission image (≤10 GB, no network at runtime)
```

---

## Evaluation Benchmarks

| Benchmark | Tasks | Key Challenge |
|-----------|-------|---------------|
| KDD Cup 2026 | 50 demo + Phase 2 | Multi-format, cross-source joins |
| DABstep | 10 dev + full test | Financial payments, scalar answers |

Scoring: `Score = Recall − λ × (Extra Columns / Predicted Columns)`. Extra columns ARE penalized. Column names ignored; values matched by content (sorted), case-sensitive, ROUND_HALF_UP 2dp.

### Current Baselines (last updated 2026-05-20)

Two scoring axes: **mean** (sum of per-task scores incl. partial credit ÷ 50) and **perfect** (count of score==1.0). Mean is the headline number; perfect is the strict pass-rate.

| Run | Mode | Mean | Perfect | Cost | SHA | Notes |
|-----|------|------|---------|------|-----|-------|
| **v93b** | Heavy auto | **0.677** | **33/50** | $22.40 | `26bc301` | P1+P2 verification — confirms v93 isn't lucky |
| **v93** | Heavy auto | **0.703** | **34/50** | $21.72 | `26bc301` | First P1+P2 run — filter_no_effect + internal_id BLOCK |
| **v92** | Heavy auto | 0.648 | 32/50 | $27.26 | `a5e7d48` | Clean-input baseline post-hygiene fixes |
| v91 | Heavy auto | 0.660 | ~33/50 | — | pre `a5e7d48` | Polluted inputs (cache self-injection) |
| v90 | Heavy auto | 0.707 | ~35/50 | — | pre `a5e7d48` | Polluted inputs + lucky variance |
| v80 | Baseline (no heavy) | — | 33/50 | — | — | Pre-heavy reference |
| v77 | Baseline (no heavy) | — | 33-34/50 | — | — | Stable peak before v78 regression |
| Heavy v70b | Heavy auto | — | ~35/50 | — | — | Reference peak with stateful REPL (reverted) |

**Current ship baseline (post-P1+P2, content-clean inputs)**: mean ≈ **0.69** (avg of v93+v93b), perfect = **33-34**.

**Eval noise floor: ±4-5 tasks at temp=0** (≈ ±0.08-0.10 mean). Any change targeting <5 tasks needs ≥2 verification runs.

**Cluster-A semantic-error tasks** (8/15 partials in v92) remain the dominant failure mode — code runs successfully, returns plausibly-shaped scalar/list, but the SELECT/WHERE/aggregation chose the wrong filter or aggregation. P1 catches the scalar-zero shape but Planner often can't recover within budget. Next levers: better Planner-Harness coupling, deeper QuestionSpec, or domain-rule extraction.

### Reverted Experiments — Do Not Re-Attempt (without new approach)

| Experiment | When | Why it failed |
|------------|------|---------------|
| Multi-candidate disagreement signal (v6) | 2026-04-24 | Candidates from same LLM call almost never disagree when both succeed. Only 3/47 tasks had ≥2 succeeding candidates. Adds cost, zero signal. To revive: need truly independent calls (i.e., that's heavy mode) |
| Adaptive voting (gated by complexity+uncertainty) | `a5f5ab2` → `c7db47a` | Same root cause as v6 — no real diversity to vote over |
| EXTRACT_MAPPINGS pre-call | 2026-05-01 `1c47a1e` → `b9690a2` | Reverted on 1-task validation only, NO full eval. Status: open question — worth re-trying with proper eval before judging |
| Stateful Python REPL (v44, v45 variants) | 2026-04-30 | Race conditions in parallel runs; pandas C ext SIGSEGV under thread parallelism. `65a5d12` disabled in-process REPL |
| Heavy deliberator trust-baseline bias | `743c30e` → `b424ea8` | Biased deliberator toward baseline even when extras were better — defeats the point |
| Heavy answer-sanity confidence signals | `f2ae46e` → `63995fc` | Added false-low-confidence triggers, over-spawned heavy mode |
| Heavy empty-list low-confidence | `288a9a9` → `f2dede0` | Same family of over-triggering issues |
| Judge "trust which HarnessGate WARN rules" | `728c8a3` → `09fff68` | Made Judge inconsistent; rule trust list ossified prompt |
| Prompt compression (planner 90→55, judge 111→63) | `ee9aab8` → `48ed740` | Lost critical guidance, score dropped |
| Cardinality+COUNT(DISTINCT) usage rule | `f036b06` → `b059246` | Over-constrained planner, regressed |
| Narrow RATIO hint injection (v27) | `dbec8c9` → `0ee9377` | Overfitted to ratio questions, broke others |
| Qwen 3.6 switch | `6258078` → `0299a9f` | Hard task accuracy collapsed 63% → 18% — Qwen 3.5 is the eval model |
| Judge B3 broad format/unit check (v5d P1a) | 2026-04-23 | Double-edged: helped first-iter false-finishes but broke correct first-iter answers |
| identify-X-and-Y multi-column inference | `97490ea` → `6377e0b` | Over-inferred 2-column output for single-column questions |
| Skeptic agent (pre-v5) | 2026-04 | 0% effective on full eval, just wasted tokens |
| Rubric Judge wholesale swap (v81) | 2026-05-17 `3be9341` → `cb45772` | Replay catch +9pp but full eval -14pp; FP on clean tasks consumed iteration budget |
| Rubric Judge + selective routing + magnitude rule (v82) | 2026-05-17 `8b8603b`+`a5a95f3` → `e0fb29b`+`a2a950e` | Replay catch +14pp + clean FP 5% (passed gates) but full eval -8pp; rubric still triggers Mode B on whitelisted shapes (task_250 aggregate, task_420 ratio) |
| D7 stuck-loop detection in orchestrator (v83) | 2026-05-17 `f137381` → `7266760` | Audit-validated direction (5 failed evidence) but -6pp on full eval; D7 fired correctly but "force pivot" guidance didn't help Planner find new path |
| B-fix 1+2 validator enforcement (v84) | 2026-05-18 `4c86aca`+`8299b75` → reverted | Enforce-existing-infra approach (close-match BLOCK + json helper recipe). -5.5pp full eval. Audit targets 0/4 recovered — Planner produced different code in v84 vs v80, validator pattern-matchers didn't fire on expected targets. Confirms audit-driven N=1 design fails to predict intervention effect across runs |

**Methodology rule (forced by 3 failed v81/v82/v83 experiments)**: before proposing any new direction, run `scripts/audit_directions.py` and require ≥5 v80-task evidence. Even with that bar, full eval still gates ship — replay catch rate and clean FP rate are necessary but insufficient. Diffuse-failure pattern (LLM variance amplification) means any system change risks losing previously-passing tasks via tail-case shifts.

**Rule**: before proposing any of the above shapes of change, read `experiment.md` for the original failure analysis.

---

## Official Eval Constraints (KDD Cup 2026)

Source: https://dataagent.top/rules (retrieved 2026-04-25)

### Hardware & Runtime
| Resource | Limit |
|----------|-------|
| CPU | 16 vCPU (x86-64) |
| RAM | 64 GB (OOM kill) |
| GPU | **None** |
| Total runtime | **12 hours for ~400 tasks** (~108s avg/task) |
| Network | **No external internet**; only internal MODEL_API_URL |

### Time Budget
12h / 400 tasks = ~108s per task average. Baseline mode averages well under budget. Heavy mode (K=3 parallel) adds ~1× wall time per triggered task. Submission entry point uses LPT scheduling — see `scripts/` and `submit_main.py`. Docker image built via `Dockerfile` (v3ce23b3 onwards).

### Environment Variables (injected at eval)
```
MODEL_API_URL   — internal Qwen3.5-35B-A3B endpoint
MODEL_API_KEY   — auth key
MODEL_NAME      — "qwen3.5-35b-a3b"
```
`llm_client.py` MUST read these from env, falling back to config.yaml for dev.

### Input Directory Structure
```
/input/task_<id>/
├── task.json          # {"task_id", "difficulty", "question"}
└── context/
    ├── csv/           # optional
    ├── db/            # optional (SQLite)
    ├── json/          # optional
    ├── doc/           # optional (markdown/docs)
    └── knowledge.md   # optional (business rules)
```
**Subdirectories are NOT fixed** — agent must dynamically detect what exists.

### Output
`/output/task_<id>/prediction.csv` — UTF-8, header row, column names ignored by scorer.

### Submission
- Docker image ≤ 10 GB, all dependencies pre-installed (no network at runtime)
- Max 1 submission/day, 30 total for Phase 1
- Already-written prediction.csv files score even if agent crashes/times out later

### Phase 2 Changes
- Harder data + **image and video modalities** added
- Still no GPU — image handling must be CPU-based (e.g., pytesseract OCR)

---

## Coding Standards for This Project

- **Immutable only**: frozen dataclasses, never mutate in-place
- **Small files**: 200-400 lines typical, 800 max
- **No silent errors**: every agent logs its reasoning to trace.json
- **Test coverage**: `dataline/tests/` — run with `pytest dataline/tests/`
- **Python 3.11+**

---

## Hygiene Audit Trigger (MANDATORY for new write paths)

Any new component that writes to the filesystem **MUST** enumerate its write
paths against the input-hygiene threat model BEFORE shipping. This includes:

- New caches (LLM output, manifests, profiler outputs)
- New intermediate artifact paths
- Anything created by Sandbox, agents, or helpers at runtime

**The check** — for every write path the new code can produce, answer:

1. Does it land inside `task_dir`? If yes → **STOP**. Move to `<repo_root>/.dataline_cache/<feature>/` or `TEMP_DIR`. Profiler will re-scan `task_dir` on the next run and treat the artifact as input.
2. Does Sandbox `_build_scratch()` need to skip it? If the write path is a dot-dir or matches `RESERVED_DIR_BASENAMES` / `RESERVED_FILENAMES` in `profiler/manifest.py`, the existing filter handles it — but verify by adding a test.
3. Does the L2 guard (`_strip_task_dir_writes`) catch it as a fallback? L2 is defense-in-depth, not the primary defense.

**Why this rule exists** — 2026-05-19 audit found B2 doc-glossary cache (#64, shipped 2026-05-17) wrote to `task_dir/.dataline_cache/`. Profiler re-scanned the cache JSONs as inputs across 50 KDD task dirs for ~3 eval runs (v90/v91/v91b). Earlier hygiene audits (#55/#57/#58) couldn't find this because B2 didn't exist yet, and no rule forced re-audit when a new write path shipped. Same class of bug WILL recur if this rule is skipped.

**How to apply** — when adding any new feature that touches the filesystem, add a unit test asserting the write does NOT land in `task_dir` (see `test_doc_glossary.py::TestCacheLocation` for the pattern: monkeypatch the cache root to `tmp_path`, run the feature, assert `list(task_dir.rglob(".dataline_cache")) == []`).
