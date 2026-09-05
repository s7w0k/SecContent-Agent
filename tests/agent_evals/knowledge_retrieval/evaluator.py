"""阶段0 产品路由基线评测器（旧链路基线）。

在离线评测集上运行**当前 legacy 规则式 ProductMatcher**，产出可重复的基线：
  - Top1 准确率
  - Top2 召回率
  - 禁止产品命中率（应为 0）
  - 无命中用例误报率（应为 0）
  - 章节展开用例 coverage

同时将基线 JSON 写入 `reports/knowledge-retrieval-baseline.json`，
满足阶段0 退出条件"旧链路指标可重复运行"（S0-4）。

使用方式：
    cd pr-agent-demo-v2
    python -m pytest tests/agent_evals/knowledge_retrieval/test_eval.py -v
    python -m tests.agent_evals.knowledge_retrieval.evaluator --report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 添加 backend 源码到 path（ProductRoutingService 依赖 agent.product_*、models）
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "services" / "backend"
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_SRC))
sys.path.insert(0, str(ROOT))

from tests.agent_evals.knowledge_retrieval.deterministic_checks import (  # noqa: E402
    load_dataset,
    run_checks,
)
from tests.agent_evals.knowledge_retrieval.generate_query_dataset import (  # noqa: E402
    OUTPUT as QUERY_OUTPUT,
)
from tests.agent_evals.knowledge_retrieval.generate_query_dataset import (  # noqa: E402
    generate as generate_query,
)
from tests.agent_evals.knowledge_retrieval.generate_user_input_dataset import (  # noqa: E402
    OUTPUT as V2_OUTPUT,
)
from tests.agent_evals.knowledge_retrieval.generate_user_input_dataset import (  # noqa: E402
    generate as generate_v2,
)


def route_auto(case: dict[str, Any], service: Any) -> tuple[list[str], list[dict]]:
    """使用统一 ProductRoutingService 预测产品 ID（auto 模式，纯规则路径）。

    Returns:
        (predicted_ids, predicted_matches)
    """
    snapshot = asyncio.run(
        service.resolve(
            case.get("article", {}),
            mode="auto",
            selected_product_ids=[],
            user_id="eval",
        )
    )
    matches = [
        {
            "product_id": rp.product_id,
            "product_name": rp.product_name,
            "match_score": rp.match_score,
            "match_reason": rp.match_reason,
        }
        for rp in snapshot.resolved_products
    ]
    return snapshot.product_ids, matches


def _load_v2() -> list[dict[str, Any]]:
    """加载 v2 多角色真实用户输入评测集。"""
    items: list[dict[str, Any]] = []
    with V2_OUTPUT.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _load_query() -> list[dict[str, Any]]:
    """加载 v3 真实线上用户 query 短句评测集。"""
    items: list[dict[str, Any]] = []
    with QUERY_OUTPUT.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def run_all(*, write_report: bool = False, dataset_version: str = "v1") -> dict[str, Any]:
    """运行全部基线检查并聚合门禁。

    Args:
        write_report: 是否写入 reports/knowledge-retrieval-baseline.json
        dataset_version: "v1"（原始评测集）、"v2"（多角色真实用户输入）
        或 "query"（真实线上用户 query 短句评测集）。
            评测 v2 时报告写入 reports/knowledge-retrieval-baseline-v2.json。

    Returns:
        {
          "total", "passed", "failed",
          "top1_accuracy", "top2_recall",
          "forbidden_violation_rate", "false_positive_rate",
          "expansion_coverage", "gates", "results", ...
        }
    """
    from agent.product_routing import ProductRoutingService

    if dataset_version == "v2":
        generate_v2()  # 重新生成 v2（幂等）
        dataset = _load_v2()
    elif dataset_version == "query":
        generate_query()  # 重新生成 v3 短 query 语料（幂等）
        dataset = _load_query()
    else:
        dataset = load_dataset()
    service = ProductRoutingService()

    results: list[dict[str, Any]] = []
    top1_ok = top2_ok = forbid_ok = nohit_ok = expansion_ok = 0
    total_expected = total_forbid = total_nohit = total_expansion = 0

    for case in dataset:
        mode = case.get("mode", "auto")
        expected = case.get("expected_product_ids", [])
        forbidden = case.get("forbidden_product_ids", [])
        requires_expansion = bool(case.get("requires_expansion", False))

        if mode == "selected":
            predicted = list(case.get("expected_product_ids", []))
            matches = [
                {
                    "product_id": pid,
                    "product_name": "",
                    "match_score": 100,
                    "match_reason": "用户指定",
                }
                for pid in predicted
            ]
        elif mode == "none":
            predicted, matches = [], []
        else:
            predicted, matches = route_auto(case, service)

        eval_result = run_checks(case, predicted)
        check_map = {c["name"]: c.get("pass", False) for c in eval_result["checks"]}

        # 仅在相关子集上累计，避免"无需求用例平凡通过"稀释分母
        if expected:
            total_expected += 1
            top1_ok += 1 if check_map.get("top1") else 0
            top2_ok += 1 if check_map.get("top2_recall") else 0
        if forbidden:
            total_forbid += 1
            forbid_ok += 1 if check_map.get("forbidden") else 0
        if not expected:
            total_nohit += 1
            nohit_ok += 1 if check_map.get("no_hit") else 0
        if requires_expansion:
            total_expansion += 1
            expansion_ok += 1 if check_map.get("expansion") else 0

        article = case.get("article", {})
        results.append(
            {
                "case_id": case["case_id"],
                "mode": mode,
                "role": case.get("role", ""),
                "tone": case.get("tone", ""),
                "query": case.get("query", ""),
                "title": article.get("title", ""),
                "summary_cn": article.get("summary_cn", ""),
                "expected": expected,
                "predicted": predicted,
                "predicted_matches": matches,
                "requires_expansion": requires_expansion,
                **eval_result,
            }
        )

    total = len(dataset)
    top1_acc = top1_ok / total_expected if total_expected else 0.0
    top2_rec = top2_ok / total_expected if total_expected else 0.0
    forbid_violation = (total_forbid - forbid_ok) / total_forbid if total_forbid else 0.0
    false_pos = (total_nohit - nohit_ok) / total_nohit if total_nohit else 0.0
    expansion_cov = expansion_ok / total_expansion if total_expansion else 0.0

    gates = {
        "top1_accuracy": {"pass": top1_acc >= 0.90, "value": top1_acc, "threshold": "≥90%"},
        "top2_recall": {"pass": top2_rec >= 0.97, "value": top2_rec, "threshold": "≥97%"},
        "forbidden_isolation": {
            "pass": forbid_violation == 0.0,
            "value": forbid_violation,
            "threshold": "0%",
        },
        "no_hit_no_fabrication": {"pass": false_pos == 0.0, "value": false_pos, "threshold": "0%"},
        "expansion_coverage": {
            "pass": expansion_cov == 1.0,
            "value": expansion_cov,
            "threshold": "100%",
        },
    }

    report = {
        "stage": "phase0",
        "schema_version": "1.0",
        "dataset_version": dataset_version,
        "generated_at": datetime.now(UTC).isoformat(),
        "total": total,
        "passed": sum(1 for r in results if r["pass"]),
        "failed": sum(1 for r in results if not r["pass"]),
        "top1_accuracy": top1_acc,
        "top2_recall": top2_rec,
        "forbidden_violation_rate": forbid_violation,
        "false_positive_rate": false_pos,
        "expansion_coverage": expansion_cov,
        "gates": gates,
        "results": results,
    }

    if write_report:
        fname = {
            "v2": "knowledge-retrieval-baseline-v2.json",
            "query": "knowledge-retrieval-baseline-query.json",
        }.get(dataset_version, "knowledge-retrieval-baseline.json")
        out = REPO_ROOT / "reports" / fname
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return report


def print_report(report: dict[str, Any]) -> None:
    """打印基线评测报告。"""
    print(f"\n{'=' * 64}")
    print("  阶段0 知识检索旧链路基线评测报告")
    print(f"{'=' * 64}")
    print(f"  样本: {report['total']}")
    print(f"  逐条通过: {report['passed']} / {report['total']}")
    print(f"  Top1 准确率: {report['top1_accuracy'] * 100:.1f}%")
    print(f"  Top2 召回率: {report['top2_recall'] * 100:.1f}%")
    print(f"  禁止产品命中率: {report['forbidden_violation_rate'] * 100:.1f}%")
    print(f"  无命中误报率: {report['false_positive_rate'] * 100:.1f}%")
    print(f"  章节展开 coverage: {report['expansion_coverage'] * 100:.1f}%")
    print(f"{'-' * 64}")
    print("  门禁:")
    for name, g in report["gates"].items():
        mark = "PASS" if g["pass"] else "FAIL"
        print(f"    [{mark}] {name}: {g['value'] * 100:.1f}% (要求 {g['threshold']})")
    print(f"{'=' * 64}")

    for r in report["results"]:
        if not r["pass"]:
            print(
                f"  FAIL [{r['case_id']}] [{r['mode']}] expected={r['expected']} predicted={r['predicted']}"
            )
            for check in r.get("checks", []):
                if not check.get("pass"):
                    print(f"       {check['name']}: {check.get('reason', '')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="阶段0 产品路由基线")
    parser.add_argument(
        "--report", action="store_true", help="写入 reports/knowledge-retrieval-baseline.json"
    )
    parser.add_argument(
        "--dataset",
        choices=["v1", "v2", "query"],
        default="v1",
        help="评测集版本：v1 原始，v2 多角色真实用户输入，query 真实线上短句语料",
    )
    args = parser.parse_args()
    _report = run_all(write_report=args.report, dataset_version=args.dataset)
    print_report(_report)
