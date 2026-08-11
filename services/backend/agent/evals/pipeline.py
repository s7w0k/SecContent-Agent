"""Eval 流水线编排 -- 阶段2 §7（WBS 2.6 入口）。

run_eval_pipeline 串联：真实双轨执行 -> 确定性评分 -> LLM-as-Judge
（含人工校准，如提供人工分数）-> paired 统计报告 -> 三层门禁评估。

返回结构化结果供 CLI / CI 消费：
    {
      "level": "pr" | "nightly" | "release",
      "report": paired_report 结构,
      "gates": {门禁名: {...}},
      "passed": bool,           # 硬门禁全部通过
      "dataset_coverage": {...}, # 数据集类别覆盖
    }
"""

from __future__ import annotations

from typing import Any

from agent.evals.contracts import EvalCase, PairedEvalResult
from agent.evals.gates import GateViolation, PipelineLevel, evaluate_gates
from agent.evals.judge import Judge, calibration_stats, flag_disagreements
from agent.evals.report import paired_report
from agent.evals.runner import EvalRunner
from agent.evals.scorers import run_deterministic_scores

VALID_LEVELS = {level.value for level in PipelineLevel}


async def run_eval_pipeline(
    cases: list[EvalCase],
    *,
    level: str = "pr",
    llm_backend: str = "mock",
    n_runs: int = 3,
    model_name: str = "deepseek-chat",
    db: Any = None,
    judge: Judge | None = None,
    human_scores: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """执行完整评测流水线（真实双轨 + 评分 + 门禁）。

    Args:
        cases: 评测用例（应含显式 split 或由调用方传入分组）
        level: "pr" | "nightly" | "release"
        llm_backend: "mock" | "real"
        n_runs: 每个 case 每后端重复次数
        model_name: 模型名（成本计价与 manifest）
        db: 可选 MongoDB（AgentLoop 事件落库）
        judge: 可选 Judge（None 时默认 mock judge）
        human_scores: 可选人工评分 {case_id: {dim: score}}（计算校准一致率）

    Returns:
        评测结果 dict（report + gates + passed）
    """
    if level not in VALID_LEVELS:
        raise ValueError(f"非法流水线层级: {level}，合法值: {sorted(VALID_LEVELS)}")

    runner = EvalRunner(llm_backend=llm_backend, model_name=model_name, n_runs=n_runs, db=db)
    pairs = await runner.run_pairs(cases)

    # 确定性评分（回填到 EvalResult）
    for pair in pairs:
        case = _find_case(cases, pair.case_id)
        if case is None:
            continue
        pair.legacy.deterministic_scores = run_deterministic_scores(case, pair.legacy)
        pair.candidate.deterministic_scores = run_deterministic_scores(case, pair.candidate)

    # LLM-as-Judge（含 A/B 位置偏差），可选人工校准
    judge_obj = judge or Judge(rubric_version="v1", mock=True)
    judge_scores_by_case: dict[str, dict[str, Any]] = {}
    deterministic_by_case: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        case = _find_case(cases, pair.case_id)
        if case is None:
            continue
        jp = await judge_obj.judge_pair(
            case.question,
            pair.legacy.actual_output,
            pair.candidate.actual_output,
        )
        pair.legacy.judge_scores = jp["legacy"]
        pair.candidate.judge_scores = jp["candidate"]
        pair.position_bias = jp["position_bias"]
        judge_scores_by_case[pair.case_id] = {
            "total": jp["candidate"].get("total", 0),
            "max_total": sum(judge_obj.rubric["dimensions"].values()),
        }
        cand_pass = all(
            c["pass"] for c in pair.candidate.deterministic_scores.values()
        )
        deterministic_by_case[pair.case_id] = {"pass": cand_pass}

    disagreements = flag_disagreements(judge_scores_by_case, deterministic_by_case)
    calibration = (
        calibration_stats(judge_scores_by_case, human_scores)
        if human_scores
        else {"calibrated": False, "agreement_rate": 0.0, "compared_dimension_pairs": 0}
    )

    # paired 报告
    dataset_version = cases[0].dataset_version if cases else ""
    report = paired_report(
        pairs,
        dataset_version=dataset_version,
        judge_calibration={
            **calibration,
            "judge_model": judge_obj.judge_model,
            "rubric_version": judge_obj.rubric["version"],
            "disagreement_cases": disagreements,
            "position_bias_summary": _sum_position_bias(pairs),
        },
    )

    # 门禁评估（release 需要 split 信息）
    case_splits = {c.case_id: c.split for c in cases}
    categories = {c.case_id: c.category for c in cases}
    gates: dict[str, Any] = {}
    passed = False
    try:
        gates = evaluate_gates(
            report,
            level,
            case_splits=case_splits,
            categories=categories,
        )
        passed = True
    except GateViolation as exc:
        gates = {"_violation": str(exc)}

    return {
        "level": level,
        "report": report,
        "gates": gates,
        "passed": passed,
        "dataset_version": dataset_version,
    }


def _find_case(cases: list[EvalCase], case_id: str) -> EvalCase | None:
    for case in cases:
        if case.case_id == case_id:
            return case
    return None


def _sum_position_bias(pairs: list[PairedEvalResult]) -> dict[str, float]:
    """汇总配对运行的位置偏差（均值）。"""
    legacy_biases = [p.position_bias.get("legacy_bias", 0.0) for p in pairs]
    cand_biases = [p.position_bias.get("candidate_bias", 0.0) for p in pairs]
    return {
        "legacy_mean_bias": round(sum(legacy_biases) / len(legacy_biases), 4)
        if legacy_biases
        else 0.0,
        "candidate_mean_bias": round(sum(cand_biases) / len(cand_biases), 4)
        if cand_biases
        else 0.0,
    }
