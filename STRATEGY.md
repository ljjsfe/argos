# dataline — Development Strategy

Research findings and development priorities for KDD Cup 2026 competition.
Last updated: 2026-04-25

---

## Priority Roadmap

| Priority | Item | Status | Impact |
|----------|------|--------|--------|
| P0 | Difficulty adapter (per-level iteration/timeout/skip) | TODO | 12h/400题的根本解法 |
| P0 | Write-early pattern (首次 pass 即写 prediction.csv) | TODO | 超时保底，~5 行改动 |
| P0 | Env var override (MODEL_API_URL/KEY/NAME) | TODO | 评测环境必须 |
| P1 | Hierarchical question decomposition (多源题拆子问题) | TODO | 异构数据准确率最大杠杆 |
| P1 | Structured failure memory (Judge guidance → 结构化记录) | TODO | 低成本减少重复错误 |
| P1 | Docker packaging (all deps pre-installed) | TODO | 提交必须 |
| P1 | Prompt simplification for Qwen3.5-35B-A3B | TODO | 3B active params 需要极简 prompt |
| P2 | Image/PDF extraction stack (RapidOCR + pdfplumber) | TODO | Phase 2 图像模态 |
| P2 | Scorer 货币符号清洗修复 | TODO | 本地评测准确性 |

---

## 1. 评测环境约束 (source: dataagent.top/rules, 2026-04-25)

详见 CLAUDE.md "Official Eval Constraints" 章节。核心数字：

- **12 小时 / ~400 题** → 平均 ~108s/题
- 16 vCPU, 64 GB RAM, **无 GPU**, **无外网**
- Docker ≤ 10 GB, 所有依赖预装
- 模型: Qwen3.5-35B-A3B (MoE, 35B total / 3B active, 262K context)
- 已写的 prediction.csv 即使后续崩溃/超时仍计分

---

## 2. 难度适配器设计

`task.json` 中的 `difficulty` 字段在评测时可用 (easy|medium|hard|extreme)。
本地 50 题分布: 15 easy / 23 medium / 11 hard / 1 extreme。

### 分级策略

| Difficulty | max_iter | Skip Analyzer | Skip Judge | Sandbox timeout | 预算/题 |
|---|---|---|---|---|---|
| easy | 2 | Yes (lite profile) | Yes (HarnessGate pass = done) | 30s | ~25s |
| medium | 4 | No | No | 60s | ~75s |
| hard | 6 | No | No | 120s | ~150s |
| extreme | 8 | No | No | 180s | ~240s |

### 时间预算分配 (估算 400 题)

| Difficulty | Count | Per-task | Subtotal |
|---|---|---|---|
| easy | 120 | 25s | 0.83h |
| medium | 120 | 75s | 2.50h |
| hard | 100 | 150s | 4.17h |
| extreme | 60 | 240s | 4.00h |
| **Total** | **400** | | **11.5h** (0.5h buffer) |

### 三个关键机制

1. **按难度排序执行**: easy 先跑 → 快速 bank points。超时只丢 extreme。
2. **Write-early**: 首次 HarnessGate pass 后立即写 prediction.csv，后续迭代覆盖更优结果。当前只在 Finalizer 最后写——超时 = 0 分。
3. **压力模式**: 当 `剩余时间 / 剩余题数 < 45s` 时，切 quick-guess 模式 (1 iter, no Judge, SQL-only)。

### 改动文件
- `main.py`: 读 difficulty, 按难度排序, 传给 orchestrator
- `orchestrator.py`: 接收 difficulty, 动态设置迭代/超时/跳过
- `config.yaml`: 新增 `difficulty_budgets` 配置节

---

## 3. 架构增强方向

### 当前架构 (保持)
Iterative plan-code-verify loop: Profiler → Analyzer → QuestionAnalyzer → Loop(PlannerCoder → Sandbox → HarnessGate → Judge) → Finalizer

调研了 LATS (树搜索), Reflexion, CodeAct, TableGPT2, DACO, multi-agent debate。
**结论: 不换架构。** 当前循环就是主流赢家模式，官方 starter kit 也是线性 ReAct。

### 增强 A: 层级问题分解 (最高 ROI)

在 QuestionAnalyzer 之后加 decompose step。对多源问题拆成子问题:
- "比较 CSV 销售额和 SQLite 退货率" → 子问题1: 查 CSV 销售额, 子问题2: 查 SQLite 退货率, 子问题3: 合并比较
- 仍在线性循环内执行，PlannerCoder 每次只关注一个子问题
- 成本: 1 个额外 LLM call
- 对异构多源任务提升最大

### 增强 B: 结构化失败记忆 (Reflexion-lite)

Judge guidance 从自由文本 → 结构化记录:
```
{attempt_id, code_type, error_class, root_cause, constraint_for_next}
```
作为列表传给下一轮 PlannerCoder。比自由文本更精准，防止重复同类错误。近零成本。

### 不做的
- **LATS 树搜索**: 3-5x LLM 调用, 3B 模型扛不住
- **Multi-agent debate**: 小模型 debate 质量差
- **DAG 执行引擎**: 官方 DAG 是题目复杂度分类法, 不是要求的 agent 架构。线性循环 + 好的 prompt 能隐式处理分支-合并任务

---

## 4. 文档/图像提取工具栈

CPU-only, Docker ≤ 10 GB 约束下的最优组合:

| Layer | Tool | Size | Speed | Purpose |
|-------|------|------|-------|---------|
| PDF 表格 | **pdfplumber + PyMuPDF** | ~15 MB | <300ms/page | pdfplumber 精准表格结构, PyMuPDF C 实现极快提取 |
| 图像 OCR | **RapidOCR** (ONNX) | ~50 MB | 200-800ms/image | PaddleOCR 同等精度, 无 PaddlePaddle 依赖, 体积省 250MB+ |
| 图表兜底 | RapidOCR + 启发式 | 0 extra | 复用 OCR | 读坐标轴标签, 几何推理映射数据点 |
| **Total** | | **~65 MB** | | |

### 为什么不用本地 VLM
- CPU 上 moondream2 (1.8B) 每张图 15-45s — 比 OCR 慢 10-30x
- VLM 擅长描述图表, 但我们需要精确数值提取 — OCR 更可靠
- 省 3-5 GB Docker 空间
- 提取文本后交给外部 Qwen3.5-35B-A3B 做推理, 效果更好

### 为什么 RapidOCR 而不是 PaddleOCR
同一套模型权重 (PaddleOCR 精度), 但用 ONNX Runtime 推理。无 PaddlePaddle 框架依赖, 体积从 300-500MB → ~50MB, RAM 减半, Docker 打包更干净。

---

## 5. Scorer 差异分析

本地 scorer (`dataline/eval/scorer.py`) 与官方规则大体一致, 但有两个风险:

| Issue | Risk | Action |
|-------|------|--------|
| `_try_numeric` strip `$€£¥,%` 后再 parse | 如果 gold answer 是 `"$1,234"` 字符串, 本地变成 `"1234.00"` → mismatch | 考虑移除或条件化 |
| 整数格式化为 `"42.00"` | 官方大概率也这样做, 但未确认 | 低风险, 保持现状 |

---

## 6. 异构数据已知短板

| Gap | Severity | Notes |
|-----|----------|-------|
| PDF/DOCX 嵌入表格 → 可查询格式 | High | 当前 reader 可能只提取纯文本, hard 题需要表格数据做计算 |
| knowledge.md 注入 PlannerCoder | Medium | 需验证 prompt 是否有效利用业务规则 |
| Extreme >128K tokens | Medium | 3B active 模型在超长上下文下退化, 需 chunk/summarize |
| 跨模态推理链 (PDF→SQL→merge) | High | 层级分解可解决 (见增强 A) |

---

## 7. Phase 2 准备

- 新增图像和视频模态
- 无 GPU — 必须 CPU-only 处理
- 工具栈: RapidOCR (图像) + pdfplumber (PDF) + 外部 Qwen (推理)
- 视频: 可能需要关键帧提取 + OCR, 待 Phase 2 数据发布后再设计
