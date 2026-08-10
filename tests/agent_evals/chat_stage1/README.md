# Chat Agent Stage 1 Golden Set

## 数据集

- **文件**: `dataset.v1.jsonl`（40 条）
- **格式**: 每行一个 JSON 对象

### 类别分布

| 类别 | 数量 | 说明 |
|---|---|---|
| no_tool | 10 | 无需工具调用，直接回答 |
| product_knowledge | 10 | 需调用 search_knowledge |
| article | 6 | 需调用 get_article 或使用上下文 |
| memory | 4 | 需调用 retrieve_memory |
| multi_turn | 4 | 多轮对话，可能多工具 |
| failure | 3 | 空消息/危险操作/提示注入 |
| security | 3 | 路径注入/跨用户/SQL注入 |

## 评测流程

1. **确定性检查**（`deterministic_checks.py`）：工具选择、回答内容、安全、收敛
2. **LLM-as-judge**（`judge_prompt.v1.md`）：5 维度评分（准确性/相关性/完整性/安全性/体验）

## 运行

```bash
# 确定性检查（mock 结果）
python -m pytest tests/agent_evals/chat_stage1/test_eval.py -v

# 评测报告
python -m tests.agent_evals.chat_stage1.evaluator
```

## 灰度回滚条件

- 错误率相对 legacy 增加 >0.5%
- p95 超阈值连续 15 分钟
- 预算超限率 >1%
- 契约错误率 >0.1%
- 任一越权/跨用户/敏感数据事件
