# 阶段二基线：v2 知识与评测基线

> 工作目录：`pr-agent-demo-v2/`
> 创建时间：2026-08-10
> 状态：已固化（Step 0 产出，阶段二实施参照）

---

## 1. 基线测试（2026-08-10）

执行命令：

```powershell
python -m pytest tests/unit/test_agent_knowledge.py tests/unit/test_knowledge_integration.py tests/unit/test_agent_scorer_v2.py tests/unit/test_draft_chat.py -q
```

结果：

```
collected 114 items
tests\unit\test_agent_knowledge.py ..................................... [ 32%]
..                                                                       [ 34%]
tests\unit\test_knowledge_integration.py ........                        [ 41%]
tests\unit\test_agent_scorer_v2.py ..................................... [ 73%]
.                                                                        [ 74%]
tests\unit\test_draft_chat.py .............................             [100%]

============================= 114 passed in 1.29s ==============================
```

**结论**：114/114 通过，0 失败，知识/评分/对话基线可复现。

## 2. 全局产品目录盘点

来源：[product_catalog.py](file:///d:/亚信安全工作/Project/智能体PR流水线/pr-agent-demo-v2/services/backend/agent/product_catalog.py)

| product_id | 名称 | 发布态 | knowledge_root | sort_order |
|---|---|---|---|---|
| agent-identity-security | 智能体身份安全 | published=True | 1-智能体身份安全 | 10 |
| agent-security | 智能体安全 | published=True | 2-智能体安全 | 20 |
| ai-bom | AI-BOM | published=True | 3-AI-BOM | 30 |
| agent-security-gateway | 智能体安全网关 | published=False | 4-智能体安全网关 | 40 |
| ans | ANS | published=False | 5-ANS | 50 |

## 3. purpose 文件选择与 source hash（旧路径）

`_PURPOSE_FILES`：

| purpose | 产品文件（knowledge_root 下） | 全局共享（固定） |
|---|---|---|
| score | overview.md, market-brief.md | 0-产品全景/overview.md, product-map.md, glossary.md + shared/hot-event-playbook.md, competitor-brief.md |
| draft | overview.md, market-brief.md, sales-brief.md | 同上 |
| chat | overview.md, market-brief.md, sales-brief.md | 同上 |

统一排除：原始文档/、tasks.md、qa-log.md、CLAUDE.md、AGENTS.md、README.md、.git、海外版、architecture-brief.md。

**source hash 规则**（`knowledge_slice.py::_compute_hash`）：`sha256( content + "|" + sorted(product_ids) )`，覆盖全局 + 用户级合并后的最终 content。

## 4. 旧路径知识字符/token 基线（实测）

`KnowledgeSliceResolver` 预算 `DEFAULT_MAX_CHARS=8000`（字符），每文件截断 `MAX_FILE_CHARS=2500`。中文估算 1 token ≈ 4 字符。

### 4.1 score 单产品（含共享）

| product_id | chars | est_tokens | truncated | 命中文件数 |
|---|---|---|---|---|
| agent-identity-security | 8576 | 2144 | **True** | 7 |
| agent-security | 8887 | 2221 | **True** | 7 |
| ai-bom | 8803 | 2200 | **True** | 7 |

### 4.2 score 多产品（2 个，含共享）

| 组合 | chars | est_tokens | truncated |
|---|---|---|---|
| agent-identity-security + ai-bom | 8052 | 2013 | **True** |

命中文件：`1-智能体身份安全/overview.md`、`market-brief.md`、`3-AI-BOM/overview.md`、`market-brief.md`、`0-产品全景/overview.md`、`product-map.md`（glossary/competitor-brief/hot-event-playbook 因预算被挤出）。

### 4.3 draft 单产品（含共享）

| product_id | chars | est_tokens | truncated | 命中文件数 |
|---|---|---|---|---|
| agent-identity-security | 8877 | 2219 | **True** | 7 |

命中文件：`1-智能体身份安全/overview.md`、`market-brief.md`、`sales-brief.md`、`0-产品全景/overview.md`、`product-map.md`、`glossary.md`、`shared/hot-event-playbook.md`。

### 4.4 各用途共享文件体量

| 项 | 值 |
|---|---|
| 共享文件数 | 5（0-产品全景 3 + shared 2） |
| 共享总字符 | 5566 |

## 5. 已知缺陷盘点（Step 3 待修正）

1. **doc_type 优先级未实现**：[knowledge_slice.py](file:///d:/亚信安全工作/Project/智能体PR流水线/pr-agent-demo-v2/services/backend/agent/knowledge_slice.py#L146) 中 `# doc_type 优先级` 之后无实现，用户级知识全部按 `sort_order` 平等竞争 8000 字符预算，存在 required（overview/market-brief）被 optional（sales-brief/custom）挤出的风险。
2. **字符预算 ≠ token 预算**：硬编码 8000 字符，未感知模型 token 窗口。
3. **required 缺失无语义记录**：overview 缺失时无法区分「未配置」与「产品确实无该能力」，可能误推断产品能力。
4. **预算先超额后标 truncated**：`sum(len(p) for p in parts) > budget` 判断在 append 之后，单次可能超额 2500 字符。
5. **用户级产品补充条目**（product_scope=global）在预算已满时被跳过，且无 required 保障。

## 6. Evals 现状

| 数据集 | 位置 | 规模 |
|---|---|---|
| chat_stage1 | tests/agent_evals/chat_stage1/ | 40 条（no_tool 10 / product_knowledge 10 / article 6 / memory 4 / multi_turn 4 / failure 3 / security 3） |
| 评分数据集 | 无 | 阶段二 Step 7 需新增 50 篇评分样本 |

## 7. 模型与 token 基线

| 项 | 值 |
|---|---|
| Provider | DeepSeek（`https://api.deepseek.com/v1`） |
| Model | deepseek-chat |
| 已知模型窗口 | 64k（动态推导将在 ContextManager 中按模型映射实现，`CONTEXT_MAX_INPUT_TOKENS=0` 时启用） |
| 旧路径评分知识 | ≈2100-2250 est_tokens（含共享），已达 8000 字符上限 |

## 8. 阶段二目标对照

| 目标 | 基线（旧路径） | 目标（新路径） |
|---|---|---|
| 评分知识输入 token 平均下降 | ≈2144-2221（score 单产品） | 下降 ≥30% 且事实/引用/合规不退化 |
| 知识/token 分配 | 8000 字符硬编码 | 模型窗口动态分配 |
| 用户知识分层 | 无 doc_type 优先级 | PURPOSE_DOC_TYPES required/optional |

## 9. 现有数据问题（仅记录，读取盘点不修改）

- `user_knowledge_entries` 历史数据可能含缺失/非法 doc_type（模型层 `KnowledgeDocType` 限定 overview/market-brief/sales-brief/custom）；迁移脚本在 Step 3 提供 `--dry-run` 与统计。
- `user_products` 中 enabled=False 的产品与知识条目是否一致待迁移时核对。
