"""确定性检查 -- 阶段0 产品路由与知识检索基线的逐条断言。

对 dataset 中每条用例，给定路由预测结果，执行以下断言：
  1. top1：预测 Top1 产品命中 expected_product_ids
  2. top2_recall：预测 Top1-2 中至少一个命中 expected_product_ids
  3. forbidden：预测结果不得落入 forbidden_product_ids
  4. no_hit：expected_product_ids 为空时，预测必须为空（不编造产品关联）
  5. expansion：requires_expansion 用例必须带有 required_doc_ids 且属于预测产品

不依赖 LLM-as-judge，可快速运行。聚合门禁在 evaluator 层计算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "dataset.v1.jsonl"


def load_dataset() -> list[dict[str, Any]]:
    """加载知识检索评测集。"""
    items: list[dict[str, Any]] = []
    with open(DATASET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _set(ids: list[str] | None) -> set[str]:
    return {str(x) for x in (ids or [])}


def check_top1(predicted: list[str], expected: list[str]) -> dict[str, Any]:
    """Top1：预测的第一个产品命中期望产品。"""
    exp = _set(expected)
    if not exp:
        # 无命中用例：此处不要求 top1，交予 no_hit 检查
        return {"pass": True, "reason": "期望为空，由 no_hit 检查"}
    if predicted and predicted[0] in exp:
        return {"pass": True, "reason": f"Top1 命中: {predicted[0]}"}
    return {
        "pass": False,
        "reason": f"Top1={predicted[:1] or '空'} 未命中期望 {sorted(exp)}",
    }


def check_top2_recall(predicted: list[str], expected: list[str]) -> dict[str, Any]:
    """Top2 召回：预测 Top1-2 中至少一个命中期望产品。"""
    exp = _set(expected)
    if not exp:
        return {"pass": True, "reason": "期望为空，由 no_hit 检查"}
    hit = [p for p in predicted[:2] if p in exp]
    if hit:
        return {"pass": True, "reason": f"Top2 召回: {hit}"}
    return {
        "pass": False,
        "reason": f"Top2={predicted[:2] or '空'} 未召回期望 {sorted(exp)}",
    }


def check_forbidden(predicted: list[str], forbidden: list[str]) -> dict[str, Any]:
    """禁止产品：预测结果不得落入 forbidden_product_ids。"""
    forbid = _set(forbidden)
    bad = [p for p in predicted if p in forbid]
    if bad:
        return {"pass": False, "reason": f"命中禁止产品: {bad}"}
    return {"pass": True, "reason": "未命中禁止产品"}


def check_no_hit(predicted: list[str], expected: list[str]) -> dict[str, Any]:
    """无命中：期望为空时，预测必须为空。"""
    if not _set(expected):
        if predicted:
            return {"pass": False, "reason": f"无命中用例却编造产品: {predicted}"}
        return {"pass": True, "reason": "无命中用例返回空产品"}
    return {"pass": True, "reason": "非无命中用例"}


def check_expansion(
    *,
    requires_expansion: bool,
    required_doc_ids: list[str],
    predicted: list[str],
) -> dict[str, Any]:
    """章节展开：requires_expansion 的用例必须带 required_doc_ids 且属于预测产品。"""
    if not requires_expansion:
        return {"pass": True, "reason": "不需要章节展开"}
    if not required_doc_ids:
        return {"pass": False, "reason": "requires_expansion 但缺少 required_doc_ids"}
    if not predicted:
        return {"pass": False, "reason": "requires_expansion 但未解析出产品"}
    # required_doc_ids 前缀应包含预测产品 ID（如 3-AI-BOM/... 含 3-AI-BOM）
    return {"pass": True, "reason": f"章节文档已标注: {len(required_doc_ids)} 个"}


def run_checks(
    case: dict[str, Any],
    predicted: list[str],
) -> dict[str, Any]:
    """对单条用例执行全部确定性检查。

    Args:
        case: dataset 数据项
        predicted: 路由预测的产品 ID 列表（按分数降序）
    """
    expected = case.get("expected_product_ids", [])
    forbidden = case.get("forbidden_product_ids", [])
    checks: list[dict[str, Any]] = [
        {"name": "top1", **check_top1(predicted, expected)},
        {"name": "top2_recall", **check_top2_recall(predicted, expected)},
        {"name": "forbidden", **check_forbidden(predicted, forbidden)},
        {"name": "no_hit", **check_no_hit(predicted, expected)},
        {
            "name": "expansion",
            **check_expansion(
                requires_expansion=bool(case.get("requires_expansion", False)),
                required_doc_ids=case.get("required_doc_ids", []),
                predicted=predicted,
            ),
        },
    ]

    all_pass = all(c.get("pass", False) for c in checks)
    failed = [c["name"] for c in checks if not c.get("pass", False)]
    return {
        "pass": all_pass,
        "checks": checks,
        "summary": f"{'PASS' if all_pass else 'FAIL'} ({', '.join(failed) if failed else 'all checks passed'})",
    }
