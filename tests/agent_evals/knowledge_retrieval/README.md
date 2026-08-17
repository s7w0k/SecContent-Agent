# 知识库检索策略改造 阶段0 离线评测集

依据《知识库检索策略改造逐步实施方案》阶段0（基线、数据契约和评测集）建立，
阶段1 起评测切换至新的 `ProductRoutingService` 统一解析入口。

## 数据集

- **文件**: `dataset.v1.jsonl`（83 条）
- **分布**:
  - 三个已发布产品（智能体身份安全 / 智能体安全 / AI-BOM）各 15 条正例 = 45
  - 10 条产品间歧义文章（两产品命名易混淆）
  - 5 条跨产品文章（三产品协同）
  - 10 条无产品命中文章（事件型/行业动态，禁止编造产品关联）
  - 10 条需要原始文档章节支持的问题（`requires_expansion=true`）
- **字段**:
  - `case_id`: 用例唯一 ID
  - `article`: 文章（title / summary_cn / content_md / category_v2 / tags / source）
  - `mode`: `selected` / `auto` / `none`
  - `expected_product_ids`: 期望路由到的产品 ID 列表
  - `required_doc_ids`: 需要注入的知识文档 ID（含原始文档时用于章节展开）
  - `forbidden_product_ids`: 禁止路由到的产品（跨产品串扰红线）
  - `requires_expansion`: 是否需要章节级证据展开
  - `allowed_product_claims`: 允许出现在 PR 中的产品事实声明

## 检查逻辑（`deterministic_checks.py`）

对每条用例给定路由预测结果执行确定性断言：

1. `top1`：预测 Top1 命中期望产品
2. `top2_recall`：预测 Top1-2 至少一个命中
3. `forbidden`：不落入禁止产品
4. `no_hit`：期望为空时预测必须为空（不编造产品关联）
5. `expansion`：需要展开的用例必须带 `required_doc_ids`

## 旧链路基线（`evaluator.py`）

阶段0 曾对 `auto` 用例运行 legacy 规则式 `ProductMatcher` 作为对照基线
（报告见 `reports/knowledge-retrieval-baseline.json`）。阶段1 后 `evaluator.py`
已切换为调用 `ProductRoutingService.resolve(mode="auto")`（S1-5），聚合以下可重复指标：

| 指标 | 门禁 |
|---|---|
| Top1 准确率 | ≥90% |
| Top2 召回率 | ≥97% |
| 禁止产品命中率 | 0% |
| 无命中误报率 | 0% |
| 章节展开 coverage | 100% |

`.report` 同时写入 `reports/knowledge-retrieval-baseline.json`，
满足阶段0 退出条件"旧链路指标可重复运行"（S0-4）。

## 运行

```bash
cd pr-agent-demo-v2
python -m pytest tests/agent_evals/knowledge_retrieval/test_eval.py -v

# 阶段1 路由评测（写入 reports/knowledge-retrieval-baseline.json）
python -m tests.agent_evals.knowledge_retrieval.evaluator --report
```

## 用途

- 阶段0：冻结当前产品路由行为，作为后续阶段改善的对照基线；
- 阶段1 起：接入 `ProductRoutingService.resolve()`（S1-5），
  复用阶段0 数据集量化产品路由契约（`ProductRoutingSnapshot`）是否改善。

## 阶段1 结果

在 83 条用例上，`ProductRoutingService` auto 模式：

- Top1 准确率 100%（门禁 ≥90%）
- Top2 召回率 100%（门禁 ≥97%）
- 无命中误报 0%、章节展开 coverage 100%

相对阶段0 旧链路基线（Top1/Top2 均为 80.6%）的主要提升来自
空白归一化（修复 "AI 资产" vs "AI资产" 召回断链）与目录关键词收敛（S1-2）。

> 跟踪性（非门禁）指标存在 2 条共享术语的真实歧义泄漏
> （`route-003` 经"智能体运行时"、`route-005` 经"多智能体"），
> 由 S1-4 LLM 重排器在生产链路中应对。