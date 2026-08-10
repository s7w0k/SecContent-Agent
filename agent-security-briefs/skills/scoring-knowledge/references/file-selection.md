# 文件选择与优先级（scoring-knowledge）

## PURPOSE_DOC_TYPES["score"]

```python
"score": {
    "required": ["overview", "market-brief"],
    "optional": [],
}
```

## 文件类型映射

| doc_type | 对应文件/条目 | 是否注入 score |
|---|---|---|
| overview | `{product_root}/overview.md`；用户级 `doc_type=overview` | 是（required） |
| market-brief | `{product_root}/market-brief.md`；用户级 `doc_type=market-brief` | 是（required） |
| sales-brief | `{product_root}/sales-brief.md`；用户级 `doc_type=sales-brief` | 否 |
| custom | 用户自定义条目 | 否 |

## 全局产品 vs 用户级产品

- **全局产品**：走 `product_catalog` 白名单文件，产品级文件按 required 顺序注入。
- **用户级产品**：走 `user_knowledge_entries`（按 user_id 隔离、enabled=true），required 优先、其余按 sort_order→updated_at→source_id 稳定排序。
- 用户对全局产品的补充条目（`product_scope=global`）：视为 optional，仅预算剩余时注入。

## 排序键

1. required 分组在前
2. 显式 sort_order（小在前）
3. updated_at（旧在前）
4. source_id / entry_id（稳定兜底，保证同内容 hash 稳定）

## 预算规则

- 预算以 token 为单位（由 ContextManager 从模型窗口推导）。
- 追加前计算：`(当前已用 + 候选内容) > budget` 则丢弃该候选，不打 truncation 标记。
- required 来源不可被挤出；不足时缩减 optional 或回退整个评分上下文（不生成残缺上下文）。
