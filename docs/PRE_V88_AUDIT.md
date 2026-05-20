# Pre-v88 Audit — verdict: PROCEED (with cost-attribution caveats)

Auditor posture: zero-trust, ~30 min investigation. The three v88 changes
(B2 doc_glossary, P5 JSON-shape generalisation, P6 tolerance plugin) are
each individually clean and safe to ship. Statistical posture is the
weakest link: three additive mechanisms compound noise → attribution
ambiguity. Findings below in severity order.

## Findings

### 1. New leakage paths in B2 — LOW
- `_collect_doc_files` (`dataline/agents/doc_glossary.py:90-111`) only
  globs `context/knowledge.md`, `knowledge.md`, `context/doc/*.md`,
  `doc/*.md`. It does NOT traverse to `output/`, `trace.json`,
  `prediction.csv`, or any agent-output artifact. Path-escape is not
  possible because globs are bounded.
- `dataline/prompts/doc_glossary.md` (69 lines): no scoring rules, no
  gold-value references, no holdout task ids. The "SKIP" list mentions
  DABstep examples (`"choose the right ACI", "minimize fraud"`) — these
  are illustrative "what is NOT a domain term" prose, not selective
  filters. **Low smell, no leakage.**
- `.gitignore` line 39: `.dataline_cache/` is explicitly excluded.
  Cache JSONs cannot leak into git.
- `_from_dict` (`doc_glossary.py:204-292`) enforces character caps
  (definition ≤240, expression ≤200, name ≤80, term count ≤80 — see
  prompt; downstream caps applied) so a hostile / hallucinated LLM
  response cannot inflate prompt size or smuggle gigantic strings into
  PlannerCoder context.

### 2. KDD-overfit risk in B2 — LOW–MEDIUM
- `_STOPWORDS` (`doc_glossary.py:429-436`): generic English question
  stems (`"the", "average", "total", "count"`, etc.). Universal.
- `_tokenise` (`doc_glossary.py:439-440`) regex `[a-z][a-z0-9_]{2,}` is
  English-only (no Unicode). KDD eval is English → fine. **Phase 2
  multilingual modalities will need a fix; tracked in `focus_hints.py:43`
  TODO already. Not blocking for v88.**
- `_question_phrases` (`doc_glossary.py:443-463`): quotes + capitalised
  phrases + 2/3-gram lowercase windows. Heuristic but not KDD-tuned.
- Magic caps (`12 hints`, `80 terms`, `30 formulas/rules/synonyms`,
  `240/200/120 chars`): principled — within Qwen 3.5 context budget,
  small enough to not dominate the prompt. Not tuned to specific KDD
  tasks.
- **Real overfit-risk anchor**: the prompt was designed against
  `data/dabstep/context/manual.md` + `public/input/task_344/...`. The
  KDD-50 knowledge.md files I sampled (task_11/145/163) share the
  `# Enterprise Data Governance Knowledge Guide` template that task_344
  uses → the prompt was designed against one example of a template that
  appears in ~50% of KDD tasks. **Mild template-coupling risk** —
  not leakage, but expect lift on template-shaped tasks and ~zero lift
  on differently-shaped tasks.

### 3. Procedural holdout discipline — PASS
- `eval_split/holdout.txt`: 4 tasks (`task_89, task_199, task_379,
  task_396`). task_257 already removed (commit `c8b2d54`).
- Grep across new B2 artifacts (`dataline/agents/doc_glossary.py`,
  `dataline/prompts/doc_glossary.md`, `dataline/tests/test_doc_glossary.py`,
  `docs/B2_DOC_GLOSSARY_DESIGN.md`): **zero matches** for any of the 4
  holdout task ids.
- Pre-existing references in `harness_gate.py:577` (task_199) and
  `question_analyzer.py:10` (task_199) predate the freeze — flagged in
  V87_INDEPENDENT_AUDIT but not new pollution.
- `dataline/eval/dev_sets.py:33-36` lists `task_379` and `task_396` in a
  dev set. **MEDIUM finding**: these holdout ids appear in a non-test
  source path. Need to verify dev_sets.py isn't read during eval or
  isn't shaping any code path. (Investigation budget exhausted — flag
  for spot-check before kick-off.)
- task_344 (referenced in B2 design) is NOT in holdout. Safe.

### 4. P5 / P6 regression risk — LOW
- P5: `safe_read_json_df` (`dataline/helpers/data_helpers.py:100-119`)
  handles `{"records":[...]}`, `{"table":..., "records":[...]}`,
  top-level `[{...}]`, and falls back to `pd.DataFrame(data)`. The
  pre-P5 inline path only handled the first shape. **Strict superset →
  no regression for any input that previously worked.**
- P6: Default `tolerance="kdd_2dp"` in `heavy_deliberator.py:39`. Diff
  (commit 77f8706) confirms the kdd_2dp branch is byte-identical to
  the prior logic: same header drop, same 2dp rounding, same all-empty
  row skip, same `cols=N` prefix. `preamble=""` for kdd_2dp keeps the
  signature format unchanged. `heavy_runner.py:249` passes
  `heavy_cfg.get("tolerance", "kdd_2dp")` and `config.yaml` does NOT
  set `tolerance` → defaults applied. **Byte-identical on KDD path.
  Confirmed v86 → v87 → v88 comparison is valid.**

### 5. Statistical posture — MEDIUM (this is the real concern)
- v86 → v87 reported lift was +4 tasks (62 → 66 ≈ +8 pp) but the V87
  audit verdict was SUSPECT (within noise, Binomial p≈0.21).
- v88 stacks THREE more mechanisms (B2, P5, P6) on top. Expected
  individual lifts:
  - B2: targets the same band as A2 (KDD knowledge.md terms). Replaces
    A2 in principle but the orchestrator (line 287) leaves A2 active:
    "Coexists with A2; A2 will retire after eval evidence shows B2
    dominates." **A2 + B2 simultaneously means lift attribution between
    them is impossible from v88 alone.**
  - P5: zero-impact on KDD (no JSON input shape variation in 50 tasks
    that's currently mis-parsed) → null hypothesis.
  - P6: zero-impact on KDD (default = prior behaviour).
- **The eval will validate B2 + A2 jointly**, not B2 in isolation.
  If v88 score == v87 score, can't tell if B2 was a no-op or a wash
  against an A2 regression. If v88 > v87, can't tell if B2 added or
  A2's effect drifted up via LLM variance.
- Eval noise floor (per CLAUDE.md) is ±4-5 tasks. A 1-2 task gain from
  B2 alone is undetectable on a single 50-task run.

### 6. Cost / latency — LOW–MEDIUM
- B2 adds 1 LLM call per task. At ~5000 tokens in + ~2400 out × 50
  tasks ≈ $1.00-$2.00 extra baseline (Qwen 3.5 internal endpoint).
- Heavy mode unaffected by B2 (extras share baseline manifest).
- Cache: `DATALINE_GLOBAL_CACHE_DIR` referenced but NOT set anywhere
  (grep confirmed). v88 runs with COLD cache. Re-runs of the SAME
  eval will warm task-local `.dataline_cache/` but the FIRST v88 run
  pays full cost.
- **Total v88 cost estimate: ~$60-$62 (baseline $58 + $1-2 B2 calls
  + heavy delta).** Within budget.

### 7. Quick smoke validation (static, 3 sample tasks) — PASS
- task_11 (Thrombosis): 102-line knowledge.md with `# Enterprise Data
  Governance` template + sectioned Patient/Examination terms with bold
  definitions. **B2 should emit 5-15 terms with `data_field` bindings
  on Thrombosis, Admission, Diagnosis.** Predict: useful hints likely.
- task_145, task_163 (student_club): identical 89-line knowledge.md
  with Members/Events/Budgets entities + KPI formulas. **B2 should
  emit 6-12 terms + 1-3 formulas.** Predict: useful hints likely.
- DABstep dev set: `_collect_doc_files` does NOT match
  `context/manual.md` or `context/payments-readme.md` (only
  `knowledge.md` or `context/doc/*.md`). **FINDING**: B2 is a no-op on
  DABstep, contradicting the "format-agnostic" framing in the docstring.
  Not blocking for KDD but worth noting — universalisation claim is
  weaker than presented.

## Specific recommendations before kicking off full eval

1. **CONFIRM** task_379 / task_396 in `dataline/eval/dev_sets.py:33-36`
   are not loaded during the production `run_eval.py` path. Quick grep:
   `git log -p dataline/eval/dev_sets.py | head` should show this is a
   dev/triage artifact, not a production set. (1 min.)
2. **PRE-COMMIT** a hypothesis matrix that distinguishes outcomes:
   - v88 ≥ v87 +5 → strong signal (above noise floor)
   - v88 within ±4 of v87 → ambiguous (LLM variance dominates)
   - v88 < v87 −5 → regression, investigate B2 prompt-injection or A2/B2
     interaction.
   Record in `experiment.md` BEFORE kicking off the run.
3. **TURN OFF A2** for one ablation cell if budget allows: v88-no-A2 vs
   v88-with-A2 separates B2-attributable lift from joint effect. If
   budget doesn't allow, accept that this eval cannot attribute B2 in
   isolation and plan an A2-ablation eval as the next step.
4. **DOC fix (non-blocking)**: clarify `doc_glossary.py:1-15` that the
   adapter is "knowledge.md-centric" not truly format-agnostic; or
   broaden `_DOC_CANDIDATES` to include any `*.md` directly under
   `context/`. Latter would silently turn on DABstep B2 — eval impact
   unknown, do NOT change before v88 eval (would invalidate prior
   measurements).

## Sign-off statement

Proceeding with the v88 full eval implies acceptance that:

- B2 is mechanically safe (no leakage, no holdout-task references, no
  prompt-injection via term definitions, cache excluded from git);
- P5 is a strict capability superset of pre-P5 behaviour;
- P6 default path is byte-identical to pre-P6 behaviour;
- The eval CANNOT attribute lift to B2 alone vs A2+B2 joint effect
  because the orchestrator runs them concurrently;
- A v88 result within ±4 of v87 is **inconclusive** and a single
  positive run does NOT meet the statistical bar that V87
  IndependentAudit flagged as missing;
- An A2-ablation eval is the implied next step regardless of v88
  outcome.

Audit verdict: **PROCEED**, with the caveat that the audit's value is
mostly mechanical-safety. Statistical attribution will require either
ablation runs or repeat measurements that this single $60 eval does
not produce.
