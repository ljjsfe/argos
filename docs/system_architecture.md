# Dataline System Architecture

> 生成于 2026-04-14，基于当前 codebase 分析

---

## 整体 Pipeline

```
INPUT: task_dir/ + question
  │
  ├── Phase 1: Setup       (deterministic, 0-1 LLM call)
  ├── Phase 2: 问题理解     (2 LLM calls)
  ├── Phase 3: 增量循环     (max 8 iterations × ~3 LLM calls)
  └── Phase 4: Finalize    (1 LLM call, 或 0 走 fast path)
```

---

## Phase 1: Setup

**Profiler**（zero LLM cost）
- 扫描 task_dir，识别 10+ 文件格式（csv/sqlite/json/md/pdf/docx/excel/image/parquet）
- 输出 `Manifest`：每个文件的 path、type、size、schema summary
- 自动发现跨数据源关系（`cross_source_relations`）

**Analyzer**（LLM + code execution）
- 对每个文件生成并执行 profiling script
- 输出 `data_profile`：column stats、distributions、sample rows
- 提取 `domain_rules`：从 manual/README/knowledge 文件中抽取业务规则
- 如果 domain_rules 太长，触发 `compile_domain_rules()`（一次 LLM call 压缩）

**写入 Workspace：**
- `workspace/DOMAIN_RULES.md`
- `workspace/DATA_PROFILE.md`

---

## Phase 2: 问题理解

**Decomposer**（1 LLM call）
- 将问题拆解为 sub_questions[]
- 输出 output_type、candidate_columns
- 有 validation_warnings 机制

**QuestionAnalyzer**（1 LLM call）
- 接受 decomposition 作为输入
- 输出：strategy plan、data source mapping、failure modes
- 简单问题（≤1 sub-question）只存 decomposition，不存 QA plan

**QA Fade 机制：**
- iteration 0-1：planner 同时看 decomposition + strategy plan
- iteration 2+：strategy plan 被丢弃，只保留 decomposition JSON
- 原因：strategy plan 在看到真实数据后往往过时，继续注入会干扰 planner

---

## Phase 3: 增量循环（最核心部分）

```
for iteration in range(max_iterations=8):

  judge_guidance ──► Planner ──► PlanStep
                                    │
                                  Coder ──► Python code
                                    │
                              CodeValidator (deterministic)
                              列名引用检查，无 LLM
                                    │
                                Sandbox
                                subprocess, TASK_DIR + TEMP_DIR
                                timeout=120s, max_memory=1GB
                                    │
                               rc != 0?
                                    │
                                Debugger (max 2 retries)
                                traceback + schema context
                                    │
                              SandboxResult
                              {stdout, stderr, rc, structured_json}
                                    │
                            SanityChecker (deterministic, pre-LLM)
                            3 个通用检查：
                            - zero_rows (过滤后0行)
                            - magnitude (ratio>100, avg太大)
                            - filter_no_effect (过滤无效)
                                    │ flags[]
                                  Judge (1 LLM call)
                                    │
                          ┌─────────┼─────────┐
                        finish   continue   backtrack
                                           truncate_to=N
```

**Orchestrator 控制逻辑：**
- `min_iterations=2`：即使 judge 说 finish，iteration 0 时强制 continue
- `guidance_repeated`：judge 连续两轮给出相同 guidance → 数据不存在，直接终止
- `stagnation`：code 连续失败 N 次 → 强制策略切换（清空 QA plan，注入强制换策略指令）
- `backtrack_limit=3`：最多回退 3 次

**关键参数（config.yaml）：**
```
max_iterations: 8
min_iterations: 2
max_retries: 2       # debugger retries
backtrack_limit: 3
stagnation_threshold: 2
```

---

## Phase 4: Finalize

三条路径，按顺序尝试：

| Path | 条件 | LLM | 实际使用率 |
|------|------|-----|-----------|
| Path 1 | save_result() 写了 structured_json | 无 | ~3/50 |
| Path 2 | stdout 是明确的 JSON dict-of-lists | 无 | 少数 |
| Path 3 | 上述均不满足 | 有（1 call）| ~47/50 |

**Path 3 的 ContextManager sections：**
```
priority=98  required_column_structure (no compress)
priority=95  last_step_result (no compress)
priority=85  earlier_step_results (compressible)
priority=80  domain_rules
priority=72  question_analysis
priority=70  key_findings
```

---

## Context Management Layer

每个 LLM call（Planner/Coder/Judge/Finalizer）都经过 `ContextManager`：

```python
caller 构建 Section[] → ContextManager.assemble() → prompt string
```

**Budget 计算：**
```
budget = context_window × 0.70 - 8000
# 262K window → ~175K usable tokens
```

**当超出 budget 时，按 priority 从低到高压缩：**
1. LLM summarization（目标压缩到 40%）
2. smart truncation（heading-aware，按信息密度评分）
3. hard truncate（safety net）

**全局 Section Priority 体系：**
```
100  question (never compress)
 98  required_column_structure (no compress)
 95  last_step_result (no compress)
 92  sanity_flags (no compress)
 90  step code (no compress)
 85  latest step output / earlier steps
 80  domain_rules
 75  coder question_analysis
 72  finalizer question_analysis
 70  key_findings / data_sources
 65  planner question_analysis
 60  completed_steps
 58  data_profile_summary (judge)
 55  judge question_analysis
 50  earlier_step_results (compressible)
```

---

## AnalysisState 层次结构

不可变 frozen dataclass，每步替换整个对象：

```
Layer 1 - Task definition (永不变):
  question, manifest_summary

Layer 2 - Domain knowledge (task 开始后不变):
  domain_rules

Layer 3 - Data understanding (analyzer 后不变):
  data_profile_summary

Layer 4 - Strategy (iter 2 后 fade):
  question_analysis

Layer 5 - Control signal (每轮更新):
  judge_guidance, completed_steps, key_findings, variables_in_scope

Layer 6 - Raw execution history:
  full_step_details (tuple[StepRecord])
```

---

## Workspace（文件系统，观测用）

```
workspace/
├── DOMAIN_RULES.md      ← analyzer 写入，一次
├── DATA_PROFILE.md      ← analyzer 写入，一次
├── ANALYSIS_PLAN.md     ← question_analyzer 写入，一次
├── PROGRESS.md          ← 每步 append
├── JUDGE_GUIDANCE.md    ← 每轮覆盖
└── steps/
    ├── step_0_code.py
    ├── step_0_output.txt
    └── ...
```

主要用途：post-run 调试观测。  
Agent 间通信走的是 `AnalysisState`（内存）+ `ContextManager`（token 管理）。

---

## LLM Call 统计（典型任务）

| Phase | 组件 | Call 数 |
|-------|------|---------|
| Setup | Analyzer（每文件） | 1-3 |
| Setup | domain_compiler（可选） | 0-1 |
| 问题理解 | Decomposer | 1 |
| 问题理解 | QuestionAnalyzer | 1 |
| Loop × N | Planner | N |
| Loop × N | Coder | N |
| Loop × N | Debugger（可选） | 0-2N |
| Loop × N | Judge | N |
| Finalize | Finalizer（Path 3） | 0-1 |
| **总计** | **典型（N=3）** | **~12-15** |

---

## 系统层面的观察（待思考）

1. **两条平行状态通道存在重复**  
   `AnalysisState`（内存）和 `Workspace`（文件）存了大量相同内容，职责边界不清晰。

2. **Setup 阶段 LLM 成本高**  
   进 loop 前已有 4-5 个 LLM call，对简单任务是 overhead。

3. **Finalizer Path 1/2 几乎不走**  
   save_result() 使用率 3/50，Path 3（LLM）是实际主路径，成本高且容易出错。

4. **Loop 每轮 3 个串行 LLM call**  
   Planner → Coder → Judge，8 轮最多 24 个 call，错误累积明显。

5. **Judge 是最重的决策节点**  
   每轮都要看完整 context，但 regression 历史显示 judge 决策质量不稳定。

6. **对纯 tabular 任务（CSV/SQLite）没有 fast path**  
   对比同类系统（SQL-first，2 LLM call，86% 准确率），我们对这类任务的 overhead 过大。

---

## 关键文件索引

```
dataline/agents/orchestrator.py    主循环，463 行
dataline/agents/planner.py         计划下一步，147 行
dataline/agents/coder.py           生成代码，136 行
dataline/agents/judge.py           评估进展，206 行
dataline/agents/finalizer.py       格式化答案，448 行
dataline/agents/analyzer.py        深度 profiling，370 行
dataline/agents/debugger.py        修复代码，120 行
dataline/agents/decomposer.py      拆解问题
dataline/agents/question_analyzer.py  策略分析
dataline/agents/sanity_checker.py  确定性检查
dataline/core/context_manager.py   Token budget 管理，402 行
dataline/core/types.py             不可变数据类型，178 行
dataline/core/workspace.py         文件系统状态，124 行
dataline/core/sandbox.py           代码执行沙箱，126 行
dataline/prompts/                  每个 agent 的 prompt 模板
```
