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

**Current: 3-5 LLM calls per task (PlannerCoder + Judge + Finalizer, +retries).**

### Context Management

ContextManager (CM) assembles the PlannerCoder prompt within token budget:
- Sections ranked by priority: question (100) > harness_feedback (96) > judge_guidance (94) > manifest (90) > domain_rules (80) > prior_steps (60)
- Over-budget sections are compressed (smart_truncate or LLM summarize)
- Domain rules compiled when exceeding budget fraction (large docs)
- Scales to large manifests and multi-iteration history without manual tuning

### Extension Points

The architecture extends by adding **adapters**, not LLM complexity:
- Today: CSV, SQLite, JSON, Parquet, Markdown, PDF, DOCX, Excel, Image
- Future: OCR for scanned tables, API connectors, streaming data, graph data
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

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Infrastructure > LLM calls | Fixing infra bugs was worth more than adding LLM calls — deterministic quality dominates |
| Rich deterministic profiling | DISTINCT values, sample rows, cardinality in schema → LLM doesn't need to "explore" data |
| DuckDB as unified query layer | CSV, JSON, SQLite, Parquet all registered as views → single SQL dialect for everything |
| SQL-first for structured data | Declarative and precise; LLM generates correct SQL at higher rate than pandas |
| PlannerCoder merged | Same reasoning process shouldn't be split — avoids info loss between plan→code |
| Multi-candidate output | LLM outputs 2-3 code candidates; try in order, first success wins (free) |
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
├── agents/        # orchestrator + agent roles (planner_coder, harness_gate, debugger, finalizer)
├── synthesizer/   # base.py, normalizer.py
├── prompts/       # .md prompt templates per agent
├── eval/          # scorer, run_eval, diagnostics, failure_analysis
└── tests/

public/            # KDD Cup Phase 1, 50 tasks (gold answers in public/output/)
data/
└── dabstep/       # DABstep benchmark (Adyen payments)

config.yaml        # LLM + agent + sandbox + eval config
main.py            # CLI entry point
```

---

## Evaluation Benchmarks

| Benchmark | Tasks | Key Challenge |
|-----------|-------|---------------|
| KDD Cup 2026 | 50 demo + Phase 2 | Multi-format, cross-source joins |
| DABstep | 10 dev + full test | Financial payments, scalar answers |

Scoring: `Score = Recall − λ × (Extra Columns / Predicted Columns)`. Extra columns ARE penalized. Column names ignored; values matched by content (sorted), case-sensitive, ROUND_HALF_UP 2dp.

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
12h / 400 tasks = ~108s per task average. Current v16 architecture averages ~11s/task (2-3 LLM calls), well within budget. Room for adaptive complexity: simple tasks stay fast, complex tasks can use more iterations.

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
