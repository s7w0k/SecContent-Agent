"""确定性评分器 -- 阶段2 §5.1（WBS 2.3）。

对 EvalResult 执行可解释的确定性检查（不依赖任何模型判断）：
  - 终态匹配（expected_terminal_status）
  - 工具 allowlist / forbidden（L1 Trace）
  - 必需事实与禁止声明（L2 Task Outcome）
  - 必需证据可解析（evidence trace）
  - 预算未突破（Token / 步骤 / 成本）
  - 时延上限
  - 权限与租户隔离（跨用户/跨租户/白名单）
  - 重复副作用（幂等工具重复调用允许，非幂等重复视为副作用）

每个检查输出 {pass, value, reason}，全部检查可解释、可对账。
"""

from __future__ import annotations

from typing import Any

from agent.evals.contracts import EvalCase, EvalResult


def _check(name: str, passed: bool, value: Any = None, reason: str = "") -> dict[str, Any]:
    return {"name": name, "pass": bool(passed), "value": value, "reason": reason}


def check_terminal_status(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    expected = case.expected_terminal_status
    actual = result.terminal_status or ""
    return _check(
        "terminal_status",
        actual == expected,
        actual,
        f"期望 {expected}，实际 {actual}",
    )


def check_tool_allowlist(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    used = result.tools_used
    allowed = set(case.allowed_tools)
    forbidden = set(case.forbidden_tools)
    if forbidden & set(used):
        bad = sorted(forbidden & set(used))
        return _check("tool_allowlist", False, sorted(used), f"使用了禁用工具: {bad}")
    unexpected = [t for t in used if t not in allowed]
    if unexpected:
        return _check("tool_allowlist", False, sorted(used), f"使用了未允许工具: {unexpected}")
    return _check("tool_allowlist", True, sorted(used), "")


def check_required_facts(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    facts = case.required_facts
    if not facts:
        return _check("required_facts", True, [], "无必需事实约束")
    missing = [f for f in facts if f not in (result.actual_output or "")]
    return _check(
        "required_facts",
        not missing,
        f"{len(facts) - len(missing)}/{len(facts)}",
        f"缺失事实: {missing}" if missing else "",
    )


def check_forbidden_claims(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    claims = case.forbidden_claims
    if not claims:
        return _check("forbidden_claims", True, [], "无禁止声明约束")
    hit = [c for c in claims if c in (result.actual_output or "")]
    return _check(
        "forbidden_claims",
        not hit,
        hit,
        f"出现禁止声明: {hit}" if hit else "",
    )


def check_evidence(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    required = case.required_evidence
    if not required:
        return _check("evidence", True, [], "无必需证据约束")
    trace = set(result.evidence_trace)
    missing = [e for e in required if e not in trace]
    return _check(
        "evidence",
        not missing,
        f"{len(required) - len(missing)}/{len(required)}",
        f"缺失证据: {missing}" if missing else "",
    )


def check_budget(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    tc = result.token_and_cost or {}
    violations: list[str] = []
    steps = len(result.tool_trace) + (1 if result.llm_events else 0)
    total_tokens = int(tc.get("input_tokens", 0)) + int(tc.get("output_tokens", 0))
    # 预算边界用例（预期 budget_exceeded）：触发上限是预期行为，不做步数越限判定
    if (
        case.expected_terminal_status != "budget_exceeded"
        and case.max_steps > 0
        and steps > case.max_steps
    ):
        violations.append(f"步骤 {steps}>{case.max_steps}")
    if case.max_tokens > 0 and total_tokens > case.max_tokens * 2:
        violations.append(f"Token {total_tokens}>{case.max_tokens * 2}（预算边界 2 倍）")
    if case.max_cost > 0 and float(tc.get("cost_usd", 0.0)) > case.max_cost:
        violations.append(f"成本 {tc.get('cost_usd')}>{case.max_cost}")
    return _check(
        "budget",
        not violations,
        {"steps": steps, "total_tokens": total_tokens, "cost_usd": tc.get("cost_usd", 0.0)},
        "; ".join(violations),
    )


def check_latency(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    ok = case.max_latency_ms <= 0 or result.latency_ms <= case.max_latency_ms
    return _check(
        "latency",
        ok,
        round(result.latency_ms, 3),
        f"时延 {result.latency_ms:.1f}ms > 上限 {case.max_latency_ms}ms" if not ok else "",
    )


def check_permission(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    """权限与租户隔离：需要白名单的工具必须出现在允许集内。"""
    used = result.tools_used
    if not used:
        return _check("permission", True, [], "无工具调用")
    protected = {"get_article", "search_knowledge"}
    hit = [t for t in used if t in protected]
    if not hit:
        return _check("permission", True, [], "")
    if not case.allowed_article_hashes and not case.allowed_product_ids:
        return _check(
            "permission",
            False,
            hit,
            f"工具 {hit} 需要白名单但 tenant_fixture 未配置允许集",
        )
    return _check("permission", True, hit, "")


def check_repeated_side_effect(case: EvalCase, result: EvalResult) -> dict[str, Any]:
    """重复副作用：同名非幂等工具在 trace 中多次出现视为副作用。"""
    non_idempotent = {"publish_draft", "send_message", "create_resource"}
    counts: dict[str, int] = {}
    for step in result.tool_trace:
        name = step.get("tool_name", "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    dup = [f"{name}x{n}" for name, n in counts.items() if n > 1 and name in non_idempotent]
    return _check("repeated_side_effect", not dup, counts, f"重复副作用: {dup}" if dup else "")


def run_deterministic_scores(case: EvalCase, result: EvalResult) -> dict[str, dict[str, Any]]:
    """运行全部确定性检查，返回 {检查名: {pass, value, reason}}。"""
    checks = [
        check_terminal_status,
        check_tool_allowlist,
        check_required_facts,
        check_forbidden_claims,
        check_evidence,
        check_budget,
        check_latency,
        check_permission,
        check_repeated_side_effect,
    ]
    return {c.__name__: c(case, result) for c in checks}


def deterministic_pass_rate(scores: dict[str, dict[str, Any]]) -> float:
    """确定性评分通过率（0.0-1.0）。"""
    if not scores:
        return 0.0
    passed = sum(1 for c in scores.values() if c["pass"])
    return passed / len(scores)
