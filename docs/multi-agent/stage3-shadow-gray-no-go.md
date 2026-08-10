# 阶段三 影子、灰度与 No-Go（Stage 3 Shadow / Gray / No-Go）

> 配套：`stage3-baseline.md`（基线/自适应集）、`stage3-runbook.md`（故障/容量/演练）。
> 代码根目录：`pr-agent-demo-v2`；日期：2026-08-10。

## 1. 上线顺序

按固定顺序推进，每级通过硬门禁后才进入下一级：

```text
flag 关 → 影子计划（不执行）→ 影子执行（不写正式产物）→ 1% → 10% → 50% → 100%
```

| 阶段 | 配置 | 行为 |
|---|---|---|
| flag 关 | `MULTI_AGENT_ENABLED=false` | 全部走默认 v2 DAG；Planner/Orchestrator 不生效 |
| 影子计划 | `MULTI_AGENT_SHADOW_ENABLED=true`（可选预检） | 仅运行 `Planner.plan()` 产出计划，不执行 Worker |
| 影子执行 | 影子开 + 影子编排器 | 完整执行但经 `_ShadowDBProxy` 拦截业务写，只写脱敏差异到 `planned_artifact_diffs` |
| 1% → 10% → 50% → 100% | `MULTI_AGENT_ROLLOUT_PERCENT=1/10/50/100` | `decide_execution_mode` 按 user_id 确定性分流到 `planned`；违规计划自动回退默认 DAG |

关键点：

- `MULTI_AGENT_ROLLOUT_PERCENT` 为 0 时恒 `current`，为 100 时全量 `planned`；
- 影子执行**不接 ledger、不回填真实账本**（`orchestrator_for(shadow=True)` 无 ledger）；
- 违规 Plan 100% fallback：Planner 校验失败写 `plan_rejected` 并回退确定性默认计划。

## 2. 灰度硬门禁（每级放量前验证）

- [ ] 违规 Plan 100% fallback（`test_violation_falls_back_100pct` 等安全套件全绿）；
- [ ] 恢复后重复业务写入 0（`test_completed_run_recovery_has_nothing_to_execute`）；
- [ ] 单 Worker 失败不重跑无依赖已完成步骤（`test_single_worker_failure_does_not_rerun_completed_steps`）；
- [ ] review / 白名单 / user 隔离 / 人工重放授权测试 100% 通过（`test_multi_agent_security.py`）；
- [ ] 自适应价值门禁：质量提升 ≥5pp，或质量不退化且成本/p95 至少一项改善 ≥15%
      （基于 `docs/multi-agent/adaptive-tasks.v1.jsonl` 对照基线评测）；
- [ ] 全量回归：`tests/unit` 1631 passed、`tests/integration` 56 passed。

## 3. No-Go 判定与处置

出现任一条件即 No-Go：

1. 未达到质量/性能/成本价值条件（见上）；
2. 发生重复业务产物（幂等键/CAS 未兜住）；
3. Plan 绕过 review / 权限 / 白名单（validator 或 schema 层未拦截）；
4. checkpoint / ledger 不一致无法自动修复（reconciliation 仅入修复队列，人工未闭环）；
5. 并发导致 provider 限流或队列饥饿不可控（llm/crawl 配额未收敛）。

处置（与 runbook 第 4 节一致）：

```text
关闭 Planner/Orchestrator 路径：
  MULTI_AGENT_ENABLED=false 或 ROLLOUT_PERCENT=0
保留（不回滚）：
  Worker Contract、幂等键、CAS/lease/fencing、恢复（recover_run/reconcile）、观测（pipeline_events）
验证：
  默认 v2 DAG 回归全绿；No-Go 原因与回滚时间写入本目录演练记录留证
```

## 4. 最终验收清单（阶段三）

- [ ] `pr-agent-demo/` 无修改；
- [ ] 默认 v2 LangGraph DAG 回归全绿；
- [ ] Plan Schema、Validator、白名单和安全必经完整；
- [ ] Worker Contract、幂等键、CAS、lease/fencing 测试通过；
- [ ] Checkpointer 与 Step Ledger 职责分离且可对账；
- [ ] 每个 Worker 边界 kill/recover 无重复产物；
- [ ] dead-letter、人工重放和 cancel 授权通过；
- [ ] 事件/UI 不泄露参数、正文和私有推理；
- [ ] 自适应价值门禁通过，否则执行 No-Go；
- [ ] 灰度、自动回滚和 runbook 演练留证。
