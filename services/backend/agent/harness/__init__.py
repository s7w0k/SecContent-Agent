"""阶段4 Harness 工程包（WBS 4.1-4.5）。

统一 Tool / Model / Context / Eval-Replay / Fault 五种 Harness，
以及生产观测（observability）、自动回滚（rollout_controller）与容量模型（capacity）。

模块：
  - tool_harness：ToolRegistry + fake/recorded/sandbox/production 适配器 + 净化 + 录制重放
  - model_harness：模型适配/限流/allowlist + 路由 + fallback + 熔断
  - context_harness：RunManifest 驱动可重复上下文构建 + legacy/candidate diff + token 偏差
  - eval_harness：Eval 快照 / 矩阵比较 / 最小复现包
  - fault_harness：11 类按步骤故障注入 + 演练
  - observability：指标聚合 / SLI/SLO / 告警规则（对齐阶段4 §2/§3）
  - rollout_controller：灰度档位追踪 + 自动回滚决策 + 审计
  - capacity：容量模型 + 场景化负载测试
"""
