# Context Stage 2 配对比较评测（阶段二 Step 7）

对 **50 篇评分 + 40 个 draft/chat 场景**执行 legacy（旧上下文路径）vs stage2
（ContextBridge + ContextPlan）配对比较，覆盖事实、引用、红线、token、时延五类断言。

## 数据集

- **文件**: `dataset.v1.jsonl`（90 条）
- **分布**: score 50 / draft 20 / chat 20
- **字段**:
  - `expected_facts`: 输出必须覆盖的事实关键词（双方）
  - `expected_reference`: stage2 注入文本必须携带的来源标记（overview/market-brief/sales-brief）
  - `red_line_forbidden`: 输出禁止出现的词（红线禁词）
  - `red_line_required`: 输出必须出现的词（合规/来源等红线要求）
  - `token_max` / `latency_p95_ms`: 未来真实采集的阈值参考

## 评测流程

1. **确定性检查**（`deterministic_checks.py`）：逐条配对断言
   - 事实覆盖（legacy 与 stage2 都覆盖）
   - 引用存在（stage2）
   - 红线（禁词不出现 / 必含词出现）
   - token 不劣化（stage2 ≤ legacy）
   - 时延不劣化（stage2 ≤ legacy）
2. **聚合门禁**（`evaluator.py`）：
   - 评分关键维度一致率 ≥98%
   - 核心知识漏载率 0（引用命中率 100%）
   - 合规红线召回率 100%
   - 平均知识 token 下降 ≥30%
   - p95 时延下降 ≥20%

## 运行

```bash
cd pr-agent-demo-v2
python -m pytest tests/agent_evals/context_stage2/test_eval.py -v

# 评测报告
python -m tests.agent_evals.context_stage2.evaluator
```

## 真实数据接入

当前 evaluator 使用 mock 配对结果验证检查逻辑。接入真实双轨数据时，将
`_generate_mock_pair` 替换为 legacy/stage2 实际运行结果：

- legacy: 旧 resolver 注入后的输出 + `context_tokens` + `latency_ms`
- stage2: `ContextBridge.build_plan(purpose=...).plan.rendered()` + `total_tokens` + 实测时延

## 灰度回滚条件（硬门禁）

- Skill 校验 100% 通过
- 核心知识漏载率 0
- 评分关键维度一致率 ≥98%
- 合规红线召回率 100%
- 平均知识 token 下降 ≥30%、p95 下降 ≥20%
- 无跨用户、未发布知识或路径泄漏事件
