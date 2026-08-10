---
name: scoring-knowledge
description: 用于产品相关度评分（score purpose）时的知识资料选择与事实边界。仅在评分器构建产品上下文时使用，指导如何按 doc_type 优先级挑选 overview/market-brief 等已发布知识，并在 required 缺失时记录 knowledge_missing 而不是推断能力。不用于对话问答或稿件生成；不承载产品事实，产品事实始终来自知识库文件。
---

# 评分知识选择 Skill

## 用途

当打分器需要对一篇外部文章做产品相关度评分时，用本 Skill 决定「读取哪些知识、按什么顺序、缺失时怎么办」。

适用场景：
- 单产品/多产品相关度双维度评分（relevance + event_impact）
- 全局产品与用户级产品混合评分

不适用场景：
- 对话问答（chat）：交给 chat 路径按需检索，不在此规则内
- PR 稿件生成（draft）：读取 draft-writing / compliance-review

## 核心规则

1. 知识文件按 `PURPOSE_DOC_TYPES["score"]` 的 required 集合选择，score 的 required 固定为 `overview` 与 `market-brief`。
2. 读取顺序：required 优先 → 显式 sort_order → updated_at → source_id（稳定排序）。
3. 任何 required 缺失时，在结果中记录 `knowledge_missing`，不得推断产品具备缺失文件所描述的能力。
4. 用户知识严格按 user_id 隔离；只读取 `enabled=true` 且（若模型有）发布态匹配的条目。
5. 预算在追加前计算；超出部分整段丢弃，不允许先超额再标 truncated。
6. 缺失/非法 doc_type 的历史条目读取时映射为 `custom`，仅在本次读取视图生效，不写回数据库。

## 文件选择表（score）

| doc_type | required? | 说明 |
|---|---|---|
| overview | required | 产品简介、定位、核心能力，必须优先保证 |
| market-brief | required | 市场与传播角度，必须优先保证 |
| sales-brief | optional | score 下不注入 |
| custom | optional | score 下不注入 |

全局共享文件（0-产品全景、shared/）仅在预算剩余时追加，属于低优先级。

## 事实边界

- 产品能力、指标、案例只以知识文件原文为准。
- 知识库未覆盖的内容如实说明「资料未覆盖」，不脑补。
- 判断「是否与该产品相关」时，基于 overview 的产品定位，而非文件名或标题猜测。

## 参考材料

- 详细评分规则：`references/scoring-rules.md`
- 文件选择与优先级对照：`references/file-selection.md`
