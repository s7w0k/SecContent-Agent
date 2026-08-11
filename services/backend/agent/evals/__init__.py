"""真实 Agent Eval Harness -- 阶段2（WBS 2.1-2.6）。

将既有 mock evaluator 升级为真实、可重复、可统计、可阻断发布的评测体系：
  - contracts: EvalCase / EvalResult / PairedEvalResult（阶段2 §3）
  - dataset: 版本化数据集 + 无泄漏 group split（§2.2）
  - runner: legacy vs candidate 双轨真实执行（§4）
  - scorers: 确定性评分器（§5.1）
  - judge: LLM-as-Judge + 位置偏差 + 人工校准（§5.2）
  - report: paired comparison 统计与 bootstrap CI（§6）
  - gates: PR/nightly/release 三层门禁（§7）

对外主入口：
    from agent.evals import run_eval_pipeline

不生成「必然通过」的 mock 结果作为发布证据；
即使使用模拟器后端，candidate 也真实执行 AgentLoop 状态机。
"""

from __future__ import annotations

from agent.evals.contracts import EvalCase, EvalResult, PairedEvalResult
from agent.evals.dataset import (
    DatasetError,
    coverage_report,
    dataset_fingerprint,
    group_split,
    load_dataset,
)
from agent.evals.gates import (
    GateViolation,
    PipelineLevel,
    evaluate_gates,
)
from agent.evals.pipeline import run_eval_pipeline
from agent.evals.runner import EvalRunner

__all__ = [
    "DatasetError",
    "EvalCase",
    "EvalResult",
    "EvalRunner",
    "GateViolation",
    "PairedEvalResult",
    "PipelineLevel",
    "coverage_report",
    "dataset_fingerprint",
    "evaluate_gates",
    "group_split",
    "load_dataset",
    "run_eval_pipeline",
]
