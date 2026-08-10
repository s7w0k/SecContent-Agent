# 阶段二：影子、灰度与回滚手册

> 文档目的：为「知识 Skills 化与上下文工程」阶段二的发布提供可执行的
> 影子（shadow）→ 灰度（gradual rollout）→ 回滚（rollback）流程与门禁。
> 对应 spec：阶段二 Step 8 与最终验收。

## 1. 配置开关总览

全部位于 `services/backend/config.py` / `.env.example`，**默认关闭**，不影响旧路径：

| 配置键 | 默认 | 含义 |
|---|---|---|
| `KNOWLEDGE_SKILLS_ENABLED` | `false` | 总开关。false = 完全走旧 resolver 路径 |
| `KNOWLEDGE_SKILLS_SHADOW_ENABLED` | `false` | 影子模式：后台构建 ContextPlan 仅记录差异，LLM 仍用旧上下文 |
| `KNOWLEDGE_SKILLS_ROLLOUT_PERCENT` | `0` | 灰度百分比（0-100），按 `sha256(user_id)` 前 8 位确定性分流 |
| `CONTEXT_MAX_INPUT_TOKENS` | `0` | 输入 token 上限；0 = 按模型窗口动态推导 |
| `CONTEXT_CACHE_TTL_SECONDS` | `300` | 上下文缓存 TTL（兜底，主动失效优先） |
| `CONTEXT_OFFLINE_COMPRESSION_ENABLED` | `false` | 离线上下文压缩（预留，默认关） |

分流规则（`agent/context_bridge.py`）：

- `off`：不构建 ContextPlan，完全走旧路径；
- `shadow`：构建 ContextPlan 并输出 `plan_hash / tokens / source_ids` 到 LLM 日志，**不注入**新内容；
- `active`：注入 `plan.rendered()`（Skill 指令 + 产品知识），不再并行注入旧知识块；
- active 且未命中灰度的用户：`effective_mode()` 归一为 `off`，回退旧路径。

## 2. 发布顺序（循序渐进）

按以下阶段推进，每个阶段停留期间收集指标并核对硬门禁，全部通过才进入下一阶段。

| 阶段 | 配置 | 观测点 | 退出条件 |
|---|---|---|---|
| 0. flag 关（当前） | `ENABLED=false` | 基线回归 | unit+integration 全绿，无告警 |
| 1. Context 影子 | `ENABLED=true, SHADOW=true` | LLM 日志 shadow 记录；legacy 结果不变 | 影子 plan_hash 稳定、无 skill 缺失、无异常来源 |
| 2. 单产品/用户 1% | `ENABLED=true, SHADOW=false, ROLLOUT_PERCENT=1` | active 用户输出与 legacy 对比 | 事实/红线/引用门禁通过 |
| 3. 10% | `ROLLOUT_PERCENT=10` | 错误率、时延、token | 全部门禁通过 |
| 4. 50% | `ROLLOUT_PERCENT=50` | 同上 | 全部门禁通过 |
| 5. 100% | `ROLLOUT_PERCENT=100` | 全量 | 稳定运行 N 天无回归 |

灰度分流示例：`sha256(user_id)` 前 8 位十六进制 `% 100 < PERCENT` 即命中。
同一用户全程稳定命中，避免体验抖动。

## 3. 硬门禁

逐项核对，任一不通过立即回滚：

| 指标 | 阈值 | 检查方式 |
|---|---|---|
| Skill 校验通过率 | 100% | `SkillRegistry.load()` 无 `SkillResolutionError` |
| 核心知识漏载率 | 0 | 评估包 `reference_hit_rate == 1.0` |
| 评分关键维度一致率 | ≥98% | 评估包 `scoring_fact_consistency` |
| 合规红线召回率 | 100% | 评估包 `red_line_recall` |
| 平均知识 token 下降 | ≥30% | 评估包 `token_reduction_pct` |
| p95 时延下降 | ≥20% | 评估包 `latency_p95_reduction_pct` |
| 跨用户/未发布/路径泄漏 | 0 事件 | 安全测试 + 日志审计（`SkillSecurityError` 计数） |

评估命令（`tests/agent_evals/context_stage2/`）：

```powershell
Set-Location 'D:\亚信安全工作\Project\智能体PR流水线\pr-agent-demo-v2'
python -m pytest tests/agent_evals/context_stage2/test_eval.py -q
python -m tests.agent_evals.context_stage2.evaluator
```

安全回归：

```powershell
python -m pytest tests/unit/test_context_security.py tests/unit/test_context_cache.py -q
```

## 4. 回滚策略

回滚 **只关闭新 Context flag**，恢复旧 resolver；不执行破坏性反向迁移。

```powershell
# .env / 环境变量
KNOWLEDGE_SKILLS_ENABLED=false
KNOWLEDGE_SKILLS_SHADOW_ENABLED=false
KNOWLEDGE_SKILLS_ROLLOUT_PERCENT=0
```

回滚保留（不清理）：

- 新增元数据（`user_knowledge_entries` 的 doc_type/sort_order 等）；
- Mongo 索引；
- ContextCache 数据与事件日志（仅 key hash/status，无正文）；
- 迁移脚本产物。

触发回滚条件（任一）：

- 任一硬门禁不满足；
- 错误率相对 legacy 增加 >0.5%；
- p95 超阈值连续 15 分钟；
- 预算超限率 >1%；
- 契约错误率 >0.1%；
- 任一越权/跨用户/敏感数据事件。

## 5. 双轨证据采集

影子/灰度期间，LLM 调用日志（`llm_wrapper.invoke_structured` 的 `context_meta`）记录：

- `context_plan_hash`：ContextPlan 指纹（稳定可对比）；
- `mode`：off / shadow / active；
- `skill_versions`：各 Skill name=version:hash；
- `knowledge_snapshot`：用户知识版本指纹；
- `source_ids`：注入来源列表；
- `budget_tokens` / `total_tokens`：预算与实际占用；
- `dropped` / `conflicts`：丢弃与冲突原因。

日志不包含知识正文与用户消息正文。

## 6. 验收清单

- [ ] 三个 Skill 包合规，必需包失败可回退；
- [ ] Skills 与事实知识模型分离；
- [ ] purpose/doc_type、发布态、user 隔离测试通过；
- [ ] ContextPlan token/冲突/丢弃/溯源完整；
- [ ] scorer/draft/chat 无双重注入；
- [ ] 缓存主动失效、防串用户通过；
- [ ] 50 篇评分 + 40 场景配对比较门禁通过；
- [ ] 灰度与回滚演练留有证据（本手册 + 配置变更记录）。
