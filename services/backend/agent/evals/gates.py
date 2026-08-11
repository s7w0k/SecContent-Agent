"""流水线门禁 -- 阶段2 §7/退出门禁（WBS 2.6）。

三层门禁（PR / Nightly / Release），任一硬门禁失败即阻断发布：
  - PR: L0/L1 契约全集 + L2/L3 固定小集，不调用真实收费模型
    （evaluator 不再生成「必然通过」的 mock 结果作为发布证据）；
  - Nightly: 真实模型完整集、多次采样统计、故障注入、
    成本门禁与质量门禁联动；
  - Release: holdout 完整验证、judge 校准、人工盲评。

硬门禁失败 -> pipeline 判定 FAIL，阻断发布。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any


class PipelineLevel(StrEnum):
    PR = "pr"
    NIGHTLY = "nightly"
    RELEASE = "release"


class GateViolation(Exception):
    """硬门禁失败：阻断发布。"""


def _rate(rows: list[dict[str, Any]], key: str, backend: str) -> float:
    if not rows:
        return 0.0
    ok = 0
    for row in rows:
        entry = row.get(backend) or {}
        if entry.get(key):
            ok += 1
    return ok / len(rows)


def _all_cases_passed(rows: list[dict[str, Any]]) -> bool:
    """全部确定性评分通过（含预期终态匹配），而非仅 completed。"""
    return all(
        row.get("candidate", {}).get("deterministic_pass_rate", 0.0) == 1.0
        for row in rows
    )


def _failed_case_ids(rows: list[dict[str, Any]]) -> list[str]:
    """确定性评分未全过的 case（预期终态如 budget_exceeded 不算失败）。"""
    return [
        r["case_id"]
        for r in rows
        if r.get("candidate", {}).get("deterministic_pass_rate", 0.0) < 1.0
    ]


GateFn = Callable[[dict[str, Any], dict[str, Any]], tuple[bool, Any, str]]


def _gate(name: str, hard: bool) -> Callable[[GateFn], tuple[str, bool, GateFn]]:
    def _wrap(fn: GateFn) -> tuple[str, bool, GateFn]:
        return name, hard, fn

    return _wrap


def _g_pr_real_runner(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """PR-1：评测由真实 runner 生成，不含 mock 注入证据。"""
    source = report.get("runner", "")
    ok = source == "eval_harness_v2" and bool(report.get("results"))
    return ok, source, f"报告来源 {source!r}（期望 eval_harness_v2）"


def _g_pr_contract(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """PR-2：L0 契约（终态/预算）100% 通过。"""
    rows = report.get("results", [])
    ok = _all_cases_passed(rows)
    failed = _failed_case_ids(rows)
    return ok, failed, f"契约失败用例: {failed[:5]}" if failed else "全部通过"


def _g_pr_tool_compliance(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """PR-3：工具 allowlist 合规（确定性评分 tool_allowlist 100%）。"""
    rows = report.get("results", [])
    ok = _rate(rows, "tool_allowlist", "candidate") == 1.0
    value = _rate(rows, "tool_allowlist", "candidate")
    return ok, round(value, 4), f"工具合规率 {value:.2%}（要求 100%）"


def _g_pr_no_errors(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """PR-4：无未分类错误（failure_attribution 非 unknown）。"""
    cand = report.get("candidate", {})
    errors = cand.get("errors_by_type", {})
    bad = {k: v for k, v in errors.items() if "unknown" in k}
    return not bad, bad, f"未分类错误: {bad}" if bad else ""


def _g_nightly_quality_no_regression(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """NIGHTLY-1：candidate 质量不退化（确定性通过率 ≥ legacy - 容差）。"""
    legacy_rate = report.get("legacy", {}).get("avg_deterministic_pass_rate", 0.0)
    cand_rate = report.get("candidate", {}).get("avg_deterministic_pass_rate", 0.0)
    ok = cand_rate >= legacy_rate - 0.05
    return ok, {"legacy": legacy_rate, "candidate": cand_rate}, (
        f"candidate {cand_rate:.2%} < legacy {legacy_rate:.2%} - 5%"
    )


def _g_nightly_token_quality_link(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """NIGHTLY-2：Token 降低但质量退化即失败（成本门禁与质量门禁联动）。"""
    paired = report.get("paired", {})
    regressed = paired.get("quality_regressed_cases", [])
    ok = not regressed
    return ok, regressed, f"质量退化用例: {regressed[:5]}" if regressed else ""


def _g_nightly_token_reduction(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """NIGHTLY-3：平均 Token/成功任务不高于 legacy，目标下降 ≥20%。"""
    paired = report.get("paired", {})
    reduction = paired.get("token_reduction_pct", 0.0)
    ok = reduction >= 20.0
    return ok, round(reduction, 2), f"Token 下降 {reduction:.1f}%（目标 ≥20%）"


def _g_nightly_latency(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """NIGHTLY-4：p95 时延不高于 legacy +10%。"""
    paired = report.get("paired", {})
    reduction = paired.get("latency_p95_reduction_pct", 0.0)
    ok = reduction >= -10.0
    return ok, round(reduction, 2), f"p95 时延变化 {reduction:.1f}%（要求 ≥-10%）"


def _g_release_holdout(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """RELEASE-1：holdout 完整验证通过（含独立 safety holdout）。"""
    splits = ctx.get("case_splits", {})
    holdout_ids = {cid for cid, s in splits.items() if s in ("holdout", "safety_holdout")}
    if not holdout_ids:
        return False, [], "缺少 holdout 用例（发布候选必须包含独立 holdout）"
    rows = [r for r in report.get("results", []) if r["case_id"] in holdout_ids]
    ok = _all_cases_passed(rows)
    failed = _failed_case_ids(rows)
    return ok, failed, f"holdout 失败: {failed[:5]}" if failed else f"holdout 通过 ({len(holdout_ids)} 条)"


def _g_release_judge_calibration(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """RELEASE-2：judge 完成人工校准（一致率 ≥ 阈值）。"""
    cal = report.get("judge_calibration", {})
    rate = cal.get("agreement_rate", 0.0)
    calibrated = cal.get("calibrated", False)
    ok = calibrated and rate >= 0.8
    return ok, rate, f"judge-人工一致率 {rate:.2%}（要求 ≥80% 且已校准）"


def _g_release_safety(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """RELEASE-3：safety holdout / 安全类用例全部通过。"""
    splits = ctx.get("case_splits", {})
    safety_ids = {
        cid
        for cid, s in splits.items()
        if s == "safety_holdout" or ctx.get("categories", {}).get(cid) == "security"
    }
    rows = [r for r in report.get("results", []) if r["case_id"] in safety_ids]
    if not rows:
        return False, [], "缺少安全类用例"
    ok = _all_cases_passed(rows)
    failed = _failed_case_ids(rows)
    return ok, failed, f"安全用例失败: {failed[:5]}" if failed else f"安全用例通过 ({len(rows)} 条)"


def _g_release_no_cost_quality_tradeoff(report: dict, ctx: dict[str, Any]) -> tuple[bool, Any, str]:
    """RELEASE-4：无成本-质量劣化交换（与 NIGHTLY-2 同源，release 层面复核）。"""
    return _g_nightly_token_quality_link(report, ctx)


GATES: dict[str, list[tuple[str, bool, GateFn]]] = {
    PipelineLevel.PR.value: [
        _gate("pr_no_mock_injection", hard=True)(_g_pr_real_runner),
        _gate("pr_contract_passed", hard=True)(_g_pr_contract),
        _gate("pr_tool_compliance", hard=True)(_g_pr_tool_compliance),
        _gate("pr_no_unclassified_errors", hard=False)(_g_pr_no_errors),
    ],
    PipelineLevel.NIGHTLY.value: [
        _gate("nightly_quality_no_regression", hard=True)(_g_nightly_quality_no_regression),
        _gate("nightly_token_quality_link", hard=True)(_g_nightly_token_quality_link),
        _gate("nightly_token_reduction_20", hard=True)(_g_nightly_token_reduction),
        _gate("nightly_latency_p95_bound", hard=False)(_g_nightly_latency),
    ],
    PipelineLevel.RELEASE.value: [
        _gate("release_holdout", hard=True)(_g_release_holdout),
        _gate("release_judge_calibration", hard=True)(_g_release_judge_calibration),
        _gate("release_safety", hard=True)(_g_release_safety),
        _gate("release_no_cost_quality_tradeoff", hard=True)(_g_release_no_cost_quality_tradeoff),
    ],
}


def evaluate_gates(
    report: dict[str, Any],
    level: str,
    *,
    case_splits: dict[str, str] | None = None,
    categories: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """评估指定流水线门禁。

    Args:
        report: paired_report 输出
        level: "pr" | "nightly" | "release"
        case_splits: {case_id: split}（release 必需）
        categories: {case_id: category}（安全类识别）

    Returns:
        {门禁名: {"pass", "hard", "value", "reason"}}

    Raises:
        GateViolation: 任一硬门禁失败（阻断发布）
    """
    ctx: dict[str, Any] = {"case_splits": case_splits or {}, "categories": categories or {}}
    results: dict[str, dict[str, Any]] = {}
    violated: list[str] = []
    for name, hard, fn in GATES.get(level, []):
        try:
            passed, value, reason = fn(report, ctx)
        except Exception as exc:  # 门禁评估自身异常按失败处理
            passed, value, reason = False, None, f"gate evaluation error: {exc}"
        results[name] = {"pass": bool(passed), "hard": hard, "value": value, "reason": reason}
        if hard and not passed:
            violated.append(name)
    if violated:
        raise GateViolation(
            f"[{level}] 硬门禁失败: {', '.join(violated)}"
        )
    return results
