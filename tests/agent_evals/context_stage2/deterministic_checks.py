"""确定性检查 -- 阶段二 Step 7（legacy vs stage2 配对比较）。

对 dataset 中每条用例执行配对断言：
  1. 事实覆盖：legacy 与 stage2 输出都覆盖 expected_facts
  2. 引用：stage2 知识注入包含 expected_reference 来源标记
  3. 红线：禁词不出现 / 必含词必须出现（双方输出）
  4. token：stage2 知识 token 不大于 legacy（聚合时计算平均下降 ≥30%）
  5. 时延：stage2 不慢于 legacy（聚合时计算 p95 下降 ≥20%）

不依赖 LLM-as-judge，可快速运行。硬门禁指标在 evaluator 聚合层计算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "dataset.v1.jsonl"


def load_dataset() -> list[dict[str, Any]]:
    """加载配对比较数据集。"""
    items: list[dict[str, Any]] = []
    with open(DATASET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def check_facts(
    legacy_text: str,
    stage2_text: str,
    expected_facts: list[str],
) -> dict[str, Any]:
    """事实覆盖：双方输出都应包含期望事实关键词。"""
    if not expected_facts:
        return {"pass": True, "reason": "无事实要求"}
    legacy_lower = legacy_text.lower()
    stage2_lower = stage2_text.lower()
    missing_legacy = [f for f in expected_facts if f.lower() not in legacy_lower]
    missing_stage2 = [f for f in expected_facts if f.lower() not in stage2_lower]
    if missing_legacy or missing_stage2:
        return {
            "pass": False,
            "reason": (
                f"legacy 缺失: {missing_legacy or '无'}; stage2 缺失: {missing_stage2 or '无'}"
            ),
        }
    return {"pass": True, "reason": "事实双方均覆盖"}


def check_reference(stage2_text: str, expected_reference: str) -> dict[str, Any]:
    """引用：stage2 注入文本应携带期望来源标记（文件名/来源 ID）。"""
    if not expected_reference:
        return {"pass": True, "reason": "无引用要求"}
    if expected_reference.lower() not in stage2_text.lower():
        return {
            "pass": False,
            "reason": f"stage2 未携带引用来源: {expected_reference}",
        }
    return {"pass": True, "reason": f"引用来源存在: {expected_reference}"}


def check_red_line(
    legacy_text: str,
    stage2_text: str,
    forbidden: list[str],
    required: list[str],
) -> dict[str, Any]:
    """红线：禁词不出现；必含词必须出现（双方输出一致校验）。"""
    problems: list[str] = []
    legacy_lower = legacy_text.lower()
    stage2_lower = stage2_text.lower()
    for word in forbidden or []:
        if word.lower() in stage2_lower:
            problems.append(f"stage2 出现禁词: {word}")
    for word in required or []:
        if word.lower() not in legacy_lower or word.lower() not in stage2_lower:
            problems.append(f"必含词缺失: {word}")
    if problems:
        return {"pass": False, "reason": "; ".join(problems)}
    return {"pass": True, "reason": "红线通过"}


def check_token_reduction(legacy_tokens: int, stage2_tokens: int) -> dict[str, Any]:
    """token：stage2 知识 token 不得大于 legacy（逐条不劣化）。"""
    if legacy_tokens <= 0:
        return {"pass": True, "reason": "legacy 无 token 数据"}
    if stage2_tokens > legacy_tokens:
        return {
            "pass": False,
            "reason": f"stage2 token 劣化: {stage2_tokens} > {legacy_tokens}",
        }
    return {"pass": True, "reason": f"token 不劣化: {stage2_tokens} ≤ {legacy_tokens}"}


def check_latency(legacy_ms: float, stage2_ms: float) -> dict[str, Any]:
    """时延：stage2 不得慢于 legacy（逐条不劣化）。"""
    if legacy_ms <= 0:
        return {"pass": True, "reason": "legacy 无时延数据"}
    if stage2_ms > legacy_ms:
        return {
            "pass": False,
            "reason": f"stage2 时延劣化: {stage2_ms:.0f}ms > {legacy_ms:.0f}ms",
        }
    return {"pass": True, "reason": f"时延不劣化: {stage2_ms:.0f}ms ≤ {legacy_ms:.0f}ms"}


def run_pair_checks(
    item: dict[str, Any],
    legacy: dict[str, Any] | None = None,
    stage2: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对单条用例执行全部配对检查。

    Args:
        item: dataset 数据项
        legacy: legacy 路径结果（answer/context_tokens/latency_ms）
        stage2: stage2 路径结果（answer/context_tokens/latency_ms）

    Returns:
        {"pass": bool, "checks": list[dict], "summary": str}
    """
    if legacy is None or stage2 is None:
        # 由 evaluator 注入 mock（验证检查逻辑用）
        raise ValueError("必须同时提供 legacy 与 stage2 结果")

    legacy_text = legacy.get("answer", "")
    stage2_text = stage2.get("answer", "")
    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "facts",
            **check_facts(legacy_text, stage2_text, item.get("expected_facts", [])),
        }
    )
    checks.append(
        {
            "name": "reference",
            **check_reference(stage2_text, item.get("expected_reference", "")),
        }
    )
    checks.append(
        {
            "name": "red_line",
            **check_red_line(
                legacy_text,
                stage2_text,
                item.get("red_line_forbidden", []),
                item.get("red_line_required", []),
            ),
        }
    )
    checks.append(
        {
            "name": "token",
            **check_token_reduction(
                legacy.get("context_tokens", 0),
                stage2.get("context_tokens", 0),
            ),
        }
    )
    checks.append(
        {
            "name": "latency",
            **check_latency(
                legacy.get("latency_ms", 0),
                stage2.get("latency_ms", 0),
            ),
        }
    )

    all_pass = all(c.get("pass", False) for c in checks)
    failed = [c["name"] for c in checks if not c.get("pass", False)]
    return {
        "pass": all_pass,
        "checks": checks,
        "summary": f"{'PASS' if all_pass else 'FAIL'} ({', '.join(failed) if failed else 'all checks passed'})",
    }
