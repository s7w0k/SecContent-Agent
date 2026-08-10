---
name: draft-writing
description: 用于 PR 稿件生成（draft purpose）时的写作流程与结构规范。在稿件生成器构建上下文时使用，指导如何按 doc_type 选择 overview/market-brief/sales-brief 知识、按模板组织稿件、保持事实边界。不承载产品事实（事实始终来自知识库文件）；不与 compliance-review 冲突时二者按先后顺序共同作用于 draft。不用于评分或对话问答。
---

# PR 稿件写作 Skill

## 用途

当系统需要为新闻事件生成 PR 初稿时，用本 Skill 决定「读取哪些知识、如何组织稿件、如何保持事实边界」。

适用场景：
- 从事件/文章生成 PR 初稿（overview + 视角/模板）
- 多产品组合稿件

不适用场景：
- 评分（score）：用 scoring-knowledge
- 合规红线检查：用 compliance-review（生成后执行）
- 对话问答（chat）：按需检索

## 核心规则

1. 知识文件按 `PURPOSE_DOC_TYPES["draft"]` 选择：required = overview + market-brief，optional = sales-brief + custom。
2. 读取顺序：required 优先 → sort_order → updated_at → source_id。
3. required 缺失记录 `knowledge_missing`，不得推断产品能力。
4. 稿件必须保留事实边界：不编造数据、客户、产品能力、交付时间。
5. 结构遵循模板（`assets/templates/`），模板未命中时使用默认结构。
6. 生成完成后必须执行 compliance-review（红线检查），两者是先后关系而非并行注入。

## 文件选择表（draft）

| doc_type | required? | 说明 |
|---|---|---|
| overview | required | 产品定位、核心能力 |
| market-brief | required | 市场角度、传播素材、金句 |
| sales-brief | optional | 售前卖点，预算剩余时注入 |
| custom | optional | 用户自定义补充，预算剩余时注入 |

## 稿件结构（默认模板）

```md
# 标题（新闻钩子 + 产品角度）

导语：事件一句话 + 与产品关联点

## 背景
事件背景、影响面

## 产品视角
产品能力如何回应事件（基于 overview/market-brief）

## 市场价值
行业意义、用户价值

## 结语
观点收束
```

## 事实边界

- 产品版本、指标、案例、交付时间以知识文件为准；无法确认时标注「需产品确认」。
- 不直接点名攻击竞品。
- 不预测未发生事件。
- 金句与传播角度只从 market-brief 已有素材中提取。

## 参考材料

- 写作规范：`references/writing-guidelines.md`
- 模板：`assets/templates/pr-template.md`
