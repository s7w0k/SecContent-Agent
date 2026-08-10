# 评分规则参考（scoring-knowledge）

## 1. 双维度评分定义

- **product_relevance（0-100）**：文章内容与产品定位、能力、适用场景的相关程度。
- **event_impact（0-100）**：事件本身的影响力、传播价值、时效性。

总分 = relevance + event_impact（范围 0-200）。`pr_total >= threshold` 判定为 PR 候选。

## 2. 知识注入顺序（score）

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 产品 overview | 定位、核心能力、适用场景 |
| 2 | 产品 market-brief | 市场角度、传播素材 |
| 3 | 用户级 required 条目 | 仅该用户可见，按 doc_type 过滤 |
| 4 | 全局共享文件 | 0-产品全景、shared/，预算剩余时追加 |

## 3. 判定约束

1. 知识库未覆盖的能力，不得作为「相关」依据。
2. 不因缺少 sales-brief/custom 而降低相关性（它们不在 score required 内）。
3. 全局产品与用户级产品统一按 required 集合保证 overview/market-brief 优先。

## 4. 缺失语义

- 产品目录中存在但 overview.md 缺失：记录 `knowledge_missing: overview`。
- 用户级产品无任何 enabled 条目：记录 `knowledge_missing: user_product_entries`。
- 缺失必须显式暴露给上层（如写入 plan/日志），禁止静默按「无能力」处理。
