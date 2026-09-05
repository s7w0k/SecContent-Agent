"""Golden Set 评测器 -- 阶段一 Step 10。

运行 Golden Set 数据集，执行确定性检查，输出报告。

使用方式：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.chat_stage1.evaluator

或在测试中：
    python -m pytest tests/agent_evals/chat_stage1/test_eval.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.agent_evals.chat_stage1.deterministic_checks import (
    load_dataset,
    run_deterministic_checks,
)


def evaluate_result(
    item: dict[str, Any], mock_result: dict[str, Any] | None = None
) -> dict[str, Any]:
    """评估单条用例。

    在实际运行中，mock_result 会被替换为真实的 Agent Loop 返回。
    目前提供 mock 结果用于验证检查逻辑。
    """
    if mock_result is None:
        # 默认 mock 结果（用于验证检查逻辑）
        mock_result = _generate_mock_result(item)

    return run_deterministic_checks(item, mock_result)


def _generate_mock_result(item: dict[str, Any]) -> dict[str, Any]:
    """为测试生成 mock 结果（验证检查逻辑用）。"""
    category = item.get("category", "")
    question = item.get("question", "")
    expected_tools = item.get("expected_tool_calls", [])

    if category == "no_tool":
        return {
            "answer": f"回答: {' '.join(item.get('expected_answer_contains', ['回答']))}",
            "tool_names_used": [],
            "rounds": 1,
            "degraded": False,
        }

    if category == "product_knowledge":
        return {
            "answer": f"基于知识库: {' '.join(item.get('expected_answer_contains', ['知识']))}",
            "tool_names_used": expected_tools,
            "rounds": 2,
            "degraded": False,
        }

    if category == "article":
        if expected_tools:
            return {
                "answer": f"文章内容: {' '.join(item.get('expected_answer_contains', ['内容']))}",
                "tool_names_used": expected_tools,
                "rounds": 2,
                "degraded": False,
            }
        return {
            "answer": f"文章信息: {' '.join(item.get('expected_answer_contains', ['信息']))}",
            "tool_names_used": [],
            "rounds": 1,
            "degraded": False,
        }

    if category == "memory":
        return {
            "answer": f"用户偏好: {' '.join(item.get('expected_answer_contains', ['偏好']))}",
            "tool_names_used": expected_tools,
            "rounds": 2,
            "degraded": False,
        }

    if category == "multi_turn":
        return {
            "answer": f"综合分析: {' '.join(item.get('expected_answer_contains', ['分析']))}",
            "tool_names_used": expected_tools,
            "rounds": 3,
            "degraded": False,
        }

    if category == "failure":
        if not question.strip():
            return {"answer": "", "tool_names_used": [], "rounds": 0, "degraded": False}
        return {
            "answer": "无法执行此操作，不能执行危险操作",
            "tool_names_used": [],
            "rounds": 1,
            "degraded": False,
        }

    if category == "security":
        return {
            "answer": "产品不在允许列表内，操作失败",
            "tool_names_used": [],
            "rounds": 1,
            "degraded": False,
        }

    return {"answer": "", "tool_names_used": [], "rounds": 0, "degraded": False}


def run_all() -> dict[str, Any]:
    """运行全部 Golden Set 评测。

    Returns:
        {"total": int, "passed": int, "failed": int, "results": list}
    """
    dataset = load_dataset()
    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for item in dataset:
        eval_result = evaluate_result(item)
        results.append(
            {
                "id": item.get("id", ""),
                "category": item.get("category", ""),
                "question": item.get("question", "")[:50],
                **eval_result,
            }
        )
        if eval_result["pass"]:
            passed += 1
        else:
            failed += 1

    return {
        "total": len(dataset),
        "passed": passed,
        "failed": failed,
        "results": results,
    }


def print_report(report: dict[str, Any]) -> None:
    """打印评测报告。"""
    print(f"\n{'=' * 60}")
    print("  Golden Set 评测报告")
    print(f"{'=' * 60}")
    print(f"  总计: {report['total']}")
    print(f"  通过: {report['passed']}")
    print(f"  失败: {report['failed']}")
    print(f"  通过率: {report['passed'] / report['total'] * 100:.1f}%")
    print(f"{'=' * 60}")

    for r in report["results"]:
        status = "✅" if r["pass"] else "❌"
        print(f"  {status} [{r['id']}] [{r['category']}] {r['question']}")
        if not r["pass"]:
            for check in r.get("checks", []):
                if not check.get("pass"):
                    print(f"       {check['name']}: {check.get('reason', '')}")


if __name__ == "__main__":
    report = run_all()
    print_report(report)
