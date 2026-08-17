# 全链路对话式 Agent 阶段 4-12 验收与发布 Runbook

版本：1.0  
日期：2026-08-16  
范围：Skill、Planner/Runtime、核心旅程、工作台、受控学习、Harness、灰度迁移

## 1. 交付边界

| 阶段 | 生产实现 | 自动验收入口 |
|---|---|---|
| 4 | 9 个 Skill v2 包、严格注册表、版本快照、发布状态机 | `test_stage4_stage5_production.py` |
| 5 | Plan v2、Rule Planner、Observation、Validator、统一 Runtime | `test_stage4_stage5_production.py` |
| 6 | 追问/回答、0/N 候选、产品歧义、低分策略 | `test_stage6_stage8_journeys.py` |
| 7 | 一句话已知文章主旅程、固定 DAG 标识、最终结果 | `test_stage6_stage8_journeys.py` |
| 8 | 不可变稿件 DAG、比较、主指针、回滚、自动复核 | `test_stage6_stage8_journeys.py` |
| 9 | Agent 工作台、SSE/刷新、候选、审批、产物动作、旧入口回退 | `AgentWorkspace.test.tsx`、`App.test.tsx` |
| 10 | 三层记忆、反馈账本、草稿候选、paired eval 硬门禁 | `test_stage10_stage12_production.py` |
| 11 | 65 条冻结数据集、核心 60 Runner、Planner mutation、质量/故障/安全/容量矩阵 | `test_full_loop_stage0.py`、`test_stage10_stage12_production.py` |
| 12 | sandbox/shadow/internal/1/10/50/100 路由、只读生产适配器、版本冻结、自动回滚 | `test_stage10_stage12_production.py` |

## 2. G0 本地门禁

在仓库根目录执行：

```powershell
$env:PYTHONPATH='services/backend'
ruff check --no-cache services/ tests/
pytest -q tests/unit/test_stage4_stage5_production.py tests/unit/test_stage6_stage8_journeys.py tests/unit/test_stage10_stage12_production.py tests/unit/test_full_loop_stage0.py
Set-Location frontend
npm.cmd test -- --run
npx.cmd tsc -b
npx.cmd vite build --outDir dist-agent-acceptance
```

通过条件：静态检查和测试均为 0 失败；生产构建成功；Planner 非法 mutation 拦截率 100%；安全硬门禁失败为 0。

## 3. G1 合并门禁

CI 必须执行全量 Python/前端测试，并保留以下工件：

- pytest 与前端组件测试报告；
- 生产构建摘要；
- paired eval snapshot 和最小复现包；
- legacy/candidate domain 指标及 delta；
- fault/security/capacity 报告；
- Skill、Tool、Planner、Runtime、Knowledge 版本快照。

任一事实、安全、多租户、审批、重复写或僵尸运行门禁失败时禁止合并和放量。

## 4. 灰度步骤

1. `sandbox`：仅 fake/sandbox Tool，跑完核心旅程和人工验收。
2. `shadow`：使用 `production_readonly`，读取真实输入，L2/L3 写 Tool 一律拒绝。
3. `internal`：仅内部白名单；L0/L1 自动执行，保存主稿仍需审批。
4. `1%`、`10%`、`50%`：按 `tenant_id + user_id` 稳定分桶；每档满足样本量与观察窗口。
5. `100%`：Agent 工作台为默认入口；仪表盘、按钮流水线和旧 Chat 保留在降级周期内。

每次运行在启动前冻结 Skill、Tool、Planner、Runtime 和输入快照。灰度期间不得同时发布大型 Skill、模型和 Runtime 变更。

## 5. 自动回滚

以下任一条件触发回滚：不安全动作、重复副作用、错误率显著上升、p95 超 SLO、单位成功成本超预算、质量抽检失败、预算耗尽异常或卡死运行。

回滚动作只关闭新 Agent 流量/写能力并恢复旧入口，不自动修改 Prompt、模型、知识或业务数据。已启动 run 继续使用冻结版本；新 run 使用回滚后的稳定版本。所有动作写入不可抵赖 Rollback Ledger。

## 6. 人工审批点

- Skill 候选从 `ready_for_review` 进入 shadow；
- Skill shadow 进入 canary/active；
- 保存主稿、扩大抓取和未来发布动作；
- 每个灰度档位晋级；
- 主观稿件质量基线的双人复核与阈值冻结。

自动化门禁不能代替最后一项业务人工签字。未签字时可以完成工程验收和 shadow，但不能宣称生产全量发布完成。

## 7. 故障处置

1. 用 `run_id` 查询统一 trace，确认 manifest、task、plan、tool、checkpoint、approval 和 artifact 时间线。
2. 若存在越权或重复写，立即清零流量并停写 Tool。
3. 若为 provider/队列故障，停止新任务，保留 checkpoint，待恢复后由 fencing token 续跑。
4. 导出脱敏最小复现包；禁止写入正文、Prompt、密钥、用户标识等高基数字段。
5. 修复后从 sandbox/shadow 重新晋级，不跨档恢复。
