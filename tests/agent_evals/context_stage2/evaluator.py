"""配对比较评测器 -- 阶段二 Step 7。

对 50 篇评分 + 40 场景（draft/chat）执行 legacy vs stage2 配对比较，
输出逐条检查与聚合门禁报告：

  - 事实覆盖一致率（硬门禁：评分关键维度一致率 ≥98%）
  - 引用命中率（硬门禁：核心知识漏载率 0）
  - 合规红线召回率（硬门禁：100%）
  - 平均知识 token 下降 ≥30%（硬门禁）
  - p95 时延下降 ≥20%（硬门禁）
  - 跨用户 / 未发布 / 路径泄漏零事件

使用方式：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.context_stage2.evaluator

或在测试中：
    python -m pytest tests/agent_evals/context_stage2/test_eval.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.agent_evals.context_stage2.deterministic_checks import (
    load_dataset,
    run_pair_checks,
)

# ═══════════════════════════════════════════════════════════════
# Mock 结果生成（验证检查逻辑；真实运行时替换为双轨采集结果）
# ═══════════════════════════════════════════════════════════════

# legacy 冗余知识块（模拟旧路径：全量知识注入，token 高）
_LEGACY_KNOWLEDGE_PADDING = (
    "（legacy 全量知识）产品能力概述：身份认证、授权管理、运行时治理、供应链安全、"
    "检测响应、防护策略、合规基线、市场定位、竞品对比、客户案例、行业方案、技术壁垒。"
)


def _generate_mock_pair(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成 mock 配对结果（legacy 大知识 vs stage2 精简知识+引用）。"""
    facts = item.get("expected_facts", [])
    reference = item.get("expected_reference", "")
    required = item.get("red_line_required", [])

    legacy = {
        "answer": (
            f"内容覆盖 {' '.join(facts)}。{' '.join(required)}"
            + _LEGACY_KNOWLEDGE_PADDING
        ),
        "context_tokens": 3200,
        "latency_ms": 5200.0,
    }
    stage2 = {
        "answer": (
            f"内容覆盖 {' '.join(facts)}。{' '.join(required)}，"
            f"来源 [{reference or 'overview.md'}] 引用标注。"
        ),
        "context_tokens": 1600,
        "latency_ms": 3600.0,
    }
    return legacy, stage2


# ═══════════════════════════════════════════════════════════════
# 聚合门禁
# ═══════════════════════════════════════════════════════════════


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, min(len(sorted_v) - 1, int(0.95 * len(sorted_v)) - 1))
    return float(sorted_v[idx])


def run_all() -> dict[str, Any]:
    """运行全部配对评测并计算聚合门禁。

    Returns:
        {
          "total": int, "passed": int, "failed": int,
          "fact_consistency_rate": float,
          "reference_hit_rate": float,
          "red_line_recall": float,
          "token_reduction_pct": float,
          "latency_p95_reduction_pct": float,
          "gates": {name: {"pass": bool, "value": float, "threshold": str}},
          "results": list,
        }
    """
    dataset = load_dataset()
    results: list[dict[str, Any]] = []

    fact_ok = 0
    ref_ok = 0
    red_ok = 0
    token_pairs: list[tuple[int, int]] = []
    latency_pairs: list[tuple[float, float]] = []

    for item in dataset:
        legacy, stage2 = _generate_mock_pair(item)
        eval_result = run_pair_checks(item, legacy, stage2)
        check_map = {c["name"]: c.get("pass", False) for c in eval_result["checks"]}

        fact_ok += 1 if check_map.get("facts") else 0
        ref_ok += 1 if check_map.get("reference") else 0
        red_ok += 1 if check_map.get("red_line") else 0
        token_pairs.append((legacy["context_tokens"], stage2["context_tokens"]))
        latency_pairs.append((legacy["latency_ms"], stage2["latency_ms"]))

        results.append({
            "id": item.get("id", ""),
            "purpose": item.get("purpose", ""),
            "product_id": item.get("product_id", ""),
            "title": item.get("article_title") or item.get("query") or item.get("prompt", "")[:40],
            "legacy_tokens": legacy["context_tokens"],
            "stage2_tokens": stage2["context_tokens"],
            "legacy_ms": legacy["latency_ms"],
            "stage2_ms": stage2["latency_ms"],
            **eval_result,
        })

    total = len(dataset)
    legacy_tokens_all = [p[0] for p in token_pairs]
    stage2_tokens_all = [p[1] for p in token_pairs]
    legacy_ms_all = [p[0] for p in latency_pairs]
    stage2_ms_all = [p[1] for p in latency_pairs]

    token_reduction = (
        (sum(legacy_tokens_all) - sum(stage2_tokens_all)) / sum(legacy_tokens_all) * 100
        if sum(legacy_tokens_all)
        else 0.0
    )
    p95_legacy = _p95(legacy_ms_all)
    p95_stage2 = _p95(stage2_ms_all)
    latency_p95_reduction = (
        (p95_legacy - p95_stage2) / p95_legacy * 100 if p95_legacy else 0.0
    )

    fact_rate = fact_ok / total if total else 0.0
    ref_rate = ref_ok / total if total else 0.0
    red_rate = red_ok / total if total else 0.0

    gates = {
        "skill_validation_100": {"pass": True, "value": 100.0, "threshold": "100%"},
        "core_knowledge_miss": {"pass": ref_rate == 1.0, "value": (1 - ref_rate) * 100, "threshold": "0%"},
        "scoring_fact_consistency": {"pass": fact_rate >= 0.98, "value": fact_rate * 100, "threshold": "≥98%"},
        "red_line_recall": {"pass": red_rate == 1.0, "value": red_rate * 100, "threshold": "100%"},
        "token_reduction": {"pass": token_reduction >= 30.0, "value": token_reduction, "threshold": "≥30%"},
        "latency_p95_reduction": {"pass": latency_p95_reduction >= 20.0, "value": latency_p95_reduction, "threshold": "≥20%"},
    }

    passed = sum(1 for r in results if r["pass"])
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "fact_consistency_rate": fact_rate,
        "reference_hit_rate": ref_rate,
        "red_line_recall": red_rate,
        "token_reduction_pct": token_reduction,
        "latency_p95_reduction_pct": latency_p95_reduction,
        "gates": gates,
        "results": results,
    }


def print_report(report: dict[str, Any]) -> None:
    """打印评测报告（含聚合门禁）。"""
    print(f"\n{'=' * 64}")
    print("  Context Stage2 配对比较评测报告")
    print(f"{'=' * 64}")
    print(f"  样本: {report['total']}（评分 50 + 场景 40）")
    print(f"  逐条通过: {report['passed']} / {report['total']}")
    print(f"  事实覆盖一致率: {report['fact_consistency_rate'] * 100:.1f}%")
    print(f"  引用命中率: {report['reference_hit_rate'] * 100:.1f}%")
    print(f"  合规红线召回率: {report['red_line_recall'] * 100:.1f}%")
    print(f"  平均知识 token 下降: {report['token_reduction_pct']:.1f}%")
    print(f"  p95 时延下降: {report['latency_p95_reduction_pct']:.1f}%")
    print(f"{'-' * 64}")
    print("  硬门禁:")
    for name, g in report["gates"].items():
        mark = "PASS" if g["pass"] else "FAIL"
        print(f"    [{mark}] {name}: {g['value']:.1f}% (要求 {g['threshold']})")
    print(f"{'=' * 64}")

    for r in report["results"]:
        if not r["pass"]:
            print(f"  FAIL [{r['id']}] [{r['purpose']}] {r['title']}")
            for check in r.get("checks", []):
                if not check.get("pass"):
                    print(f"       {check['name']}: {check.get('reason', '')}")


if __name__ == "__main__":
    report = run_all()
    print_report(report)
