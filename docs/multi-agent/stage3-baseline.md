# 阶段三基线：MultiAgent 编排（Stage 3 Baseline）

> 冻结 v2 默认 LangGraph DAG 与自适应任务集，为受约束 Multi-agent 编排提供对照基线。
> 代码根目录：`pr-agent-demo-v2`；日期：2026-08-10。

## 1. 基线测试

```powershell
Set-Location 'D:\亚信安全工作\Project\智能体PR流水线\pr-agent-demo-v2'
python -m pytest tests/unit/test_pipeline_v2.py tests/unit/test_checkpointer.py -q   # 28 passed
python -m pytest tests/unit/test_agent_pipeline.py tests/integration/test_e2e_pipeline.py -q  # 39 passed
```

基线全绿：**67 passed**（Step 0 门禁）。

## 2. 默认 v2 DAG 快照

固定拓扑（`agent/pipeline_v2.py`）：

```text
crawl → (enrich?) → classify_v2 → filter → score_v2 → (score_v2 retry?) → draft
      → quality_check → (rewrite?) → review → END
```

| 节点 | 职责 | 失败语义 |
|---|---|---|
| crawl | 复用 V1 crawl_node，统计 needs_enrich | 失败→整体 failed |
| enrich | 补爬正文 <200 字文章，最多一次 | 失败仅记录 error，继续 |
| classify_v2 | ClassifierV2 六分类 + PR 候选标记 | 失败记 error，继续 |
| filter | 统计 is_pr_eligible 数量 | 纯计数 |
| score_v2 | ScoringAgentV2 双维度评分，异常重试一次 | 重试一次后继续 |
| draft | 对 ≥threshold 文章生成 PR 草稿（upsert user_drafts） | 失败记 error，继续 |
| quality_check | 启发式标记缺字段/<300 字草稿 | 失败记 error |
| rewrite | 重写不达标草稿 | 失败保留原稿 |
| review | 并发检查最终稿（Semaphore(2)），content_hash 复用 | 单篇失败标 failed |

条件路由：`route_after_crawl` / `route_after_score`（score_anomaly 重试一次）/ `route_after_quality_check`。

状态字段：`create_state_v2()` 定义 9 阶段 phases、各计数、score_threshold、
`needs_enrich/score_anomaly/needs_rewrite/frozen_templates/errors/status/current_phase` 等。

检查点：`MongoDBSaver`（`pipeline_checkpoints` + `pipeline_checkpoint_writes`），
thread_id=`thread-{task_id}`；`PipelineStateManager` 写 `pipeline_tasks`（task_id 唯一索引）。

## 3. 自适应任务集

`docs/multi-agent/adaptive-tasks.v1.jsonl`（56 条），字段：
`task_id / scenario / articles / products / needs_enrich / breaking_event /
score_anomaly / worker_failure / expected_status / expected_drafts / note`。

覆盖矩阵：

| 维度 | 条目 |
|---|---|
| 全流程（单/多文章、单/多产品） | AT001-AT004, AT034, AT035 |
| 空输入 / 无候选 / 无产品 | AT005, AT006, AT041, AT042, AT052 |
| 需补全文（enrich） | AT007-AT009, AT050, AT055 |
| 重点事件（breaking_event） | AT010, AT011, AT050, AT054 |
| 异常摘要（score_anomaly） | AT012, AT013, AT051, AT054 |
| Worker 失败（enrich/classify/score/draft/qc/rewrite/review/crawl） | AT009, AT017-AT027, AT032, AT043, AT046 |
| 部分成功 | AT018-AT023, AT026, AT027, AT048, AT049 |
| 恢复/重复/取消 | AT030-AT033 |
| 并发配额 | AT044, AT045, AT047 |
| 边界（50 步骤） | AT056 |

## 4. 故障注入矩阵

按 Worker × 故障类型；`F` 表示在阶段三 Orchestrator 层注入验证。

| 故障类型 | crawl | enrich | classify | filter | score | draft | quality_check | rewrite | review | 全局 |
|---|---|---|---|---|---|---|---|---|---|---|
| timeout | F | F | F | - | F | F | F | F | F | F |
| LLM 异常（retryable） | - | - | - | - | F | F | - | F | F | - |
| 契约错误（不可重试） | - | - | - | - | F | - | - | - | - | - |
| fallback 全量 | - | - | F | - | F | - | - | - | - | - |
| 部分失败 | F | F | - | - | - | F | - | - | F | - |
| Mongo 中断 | F | F | F | F | F | F | F | F | F | F |
| Redis/ARQ 中断 | - | - | - | - | - | - | - | - | - | F |
| 进程 kill | F | F | F | F | F | F | F | F | F | F |
| 网络分区（provider） | - | - | - | - | F | F | - | F | F | - |
| 日志写失败 | F | F | F | F | F | F | F | F | F | F |
| 重复触发 | - | - | - | - | - | - | - | - | - | F |
| 租约接管 | - | - | - | - | F | F | - | F | F | F |
| 迟到写（fencing） | - | - | - | - | F | F | - | F | F | - |
| 取消竞态 | - | - | - | - | - | - | - | - | - | F |

恢复/死信语义（对照阶段三 Step 8）：

| 失败 Worker | 保留 | 动作 |
|---|---|---|
| enrich | 原摘要 | 标记 enrich_failed，按 policy 继续 |
| score | 分类 | 重试；耗尽后 dead-letter |
| draft | 分类/评分 | run=partial，单独重试 draft |
| review | 未发布草稿 | pending_review，禁止发布 |
| cancel | 已提交幂等产物 | 停止新步骤，不删除已确认事实 |

## 5. 基线指标（记录模板）

| 指标 | 默认 DAG 记录值 | 阶段三目标 |
|---|---|---|
| 质量（评分一致率） | 待采集 | ≥98% 或提升 ≥5pp |
| 平均知识 token | 待采集 | 下降 ≥30% |
| 成本（USD/run） | 待采集 | 不劣化 |
| p50 / p95 时延 | 待采集 | p95 下降 ≥20% |
| 失败率 | 待采集 | ≤0.5% 增量 |
| 重复业务写入 | 0（基线） | 0（硬门禁） |
| 恢复时长 | 待采集 | 有界 |

> 采集方法：shadow 模式双轨运行自适应任务集，LLM 日志 `context_meta` +
> `execution_runs/execution_events` + `pipeline_tasks` 聚合；待阶段三 Orchestrator
> 上线后由 `docs/multi-agent/stage3-shadow-gradual-rollback.md` 门禁复核。
