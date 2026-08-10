# 阶段三 Runbook：故障注入、容量与演练（Stage 3 Runbook）

> 配套：`stage3-baseline.md`（基线/自适应集）、`stage3-shadow-gray-no-go.md`（上线顺序与 No-Go）。
> 代码根目录：`pr-agent-demo-v2`；日期：2026-08-10。

## 1. 故障矩阵与自动化覆盖

自动化（`tests/integration/test_multi_agent_fault_injection.py`）已覆盖：

| 故障 | 注入方式 | 期望行为 | 对应测试 |
|---|---|---|---|
| Worker timeout | adapter 挂起 + spec.timeout_s | 重试耗尽 → `dead_lettered` → run=`partial`；optional 步骤按策略 `skipped` | `test_worker_timeout_becomes_dead_lettered_partial`（8 Worker 参数化） |
| Worker 非重试异常 | adapter 返回 retryable=False | `failed` → run=`failed`；optional 跳过 | `test_worker_non_retryable_failure_fails_run` |
| 重试后成功 | 前 N 次失败 | `succeeded`，attempt/fencing 递增 | `test_retry_then_success` |
| 租约接管 / 迟到写 | 过期 running + 旧 owner/fencing | 接管后 fencing 递增，旧 owner 迟到写被 CAS 拒绝 | `test_stale_write_rejected_after_takeover` |
| 终态重复写入 | 已 complete 再 complete | `LeaseConflictError` | `test_late_complete_after_terminal_rejected` |
| 取消竞态 | cancel_event 首波置位 | 后续波不调度，run=`canceled` | `test_cancel_event_stops_later_waves` |
| deadline | 过期 deadline_at | 立即取消，0 执行 | `test_deadline_cancels_immediately` |
| 重复触发 | 重复 init_run / 重跑已完成 run | init_run 幂等；终态领不到租约 → 0 重复业务写入 | `test_init_run_idempotent_*`、`test_completed_run_rerun_*` |

恢复（`tests/integration/test_pipeline_recovery.py`）：恢复后重复业务写入 0、
单 Worker 失败不重跑无依赖已完成步骤、succeeded 校验后跳过、reconciliation
不一致入 `ledger_repair_queue` 不盲目重跑。

## 2. 人工演练（kill / recover 留证）

每轮演练按「记录基线 → 注入 → 验证 → 留证」执行，产出写入本目录演练记录。

### 2.1 Worker 进程 kill / 恢复

1. 发起 planned run（`POST /api/pipeline/runs`，`execution_mode=planned`）；
2. 在某个 required Worker（如 score）执行中 kill 工作进程；
3. 重启服务（startup 会 `build_multi_agent_runtime`）；
4. 执行恢复：读取 `execution_step_ledger`，对 `status=running` 且租约过期步骤
   `takeover_expired`（fencing 递增），仅执行 `pending/failed/dead_lettered`；
5. 验证：
   - 已完成步骤业务产物不重复（幂等键 + CAS 拒绝迟到写）；
   - `execution_step_ledger` 无 `duplicate` 记录；
   - `pipeline_events` 出现 `step_skipped/retrying/replayed` 事件。
6. 留证：事件序列截图 + 账本状态导出。

### 2.2 Mongo / Redis / ARQ 中断

| 中断 | 验证点 | 处置 |
|---|---|---|
| Mongo 短暂不可用 | 步骤租约/记账失败仅日志告警，不产生重复写入 | 恢复后 `recover_run` 接管过期 running |
| Redis/ARQ 队列中断 | 任务不丢失（checkpoint 持久化） | 队列恢复后重放 pending |
| 网络分区 | Worker 超时重试 → 死信 | 分区恢复后人工 replay |

### 2.3 日志失败 / 事件写入失败

`EventEmitter.emit` 为 fire-and-forget，失败只记日志，不影响流水线终态
（`test_events.py` 覆盖 emit 失败静默）。

## 3. 容量控制

| 维度 | 机制 | 配置键 |
|---|---|---|
| 全局并发 | `asyncio.Semaphore` | `ORCHESTRATOR_MAX_CONCURRENCY` |
| 用户级并发 | 按 user_id 信号量 | `ORCHESTRATOR_USER_CONCURRENCY` |
| provider 并发（llm/crawl/local） | 并发组信号量 | `_DEFAULT_SPEC[].concurrency_group` |
| Worker 级并发 | 按 worker 信号量 | `ORCHESTRATOR_MAX_CONCURRENCY` |
| 计划规模 | 步骤/深度/扇出/总超时预算 | `PLAN_MAX_STEPS` / `PLAN_MAX_DEPTH` |
| 租约与重试 | 租约秒数 / 最大尝试 | `WORKER_LEASE_SECONDS` / `WORKER_MAX_ATTEMPTS` |
| 事件容量 | TTL 90 天 + 聚合指标；成功事件不采样 | `pipeline_events` TTL 索引 |
| 差异日志 | 影子模式只写脱敏差异 | `planned_artifact_diffs` |

## 4. 回滚步骤（No-Go 时）

1. 关闭灰度：`MULTI_AGENT_ROLLOUT_PERCENT=0` 或 `MULTI_AGENT_ENABLED=false`；
2. 重启 worker → `decide_execution_mode` 恒返回 `current`，所有请求走默认 v2 LangGraph DAG；
3. 保留（不回滚）：Worker Contract、幂等键、CAS/lease/fencing、恢复与观测改造——
   这些不改变默认路径行为，且为回滚后提供一致性保护；
4. 验证：默认 DAG 回归 `python -m pytest tests/unit/test_pipeline_v2.py tests/unit/test_checkpointer.py -q` 全绿；
5. 清理：停用影子执行（`MULTI_AGENT_SHADOW_ENABLED=false`），按 TTL 归档事件/账本。
