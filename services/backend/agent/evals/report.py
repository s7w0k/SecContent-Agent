"""统计与报告 -- 阶段2 §4/§6/§7（WBS 2.5）。

提供：
  - 标准分位数（线性插值）与手写 bootstrap 置信区间（不依赖 numpy/scipy）；
  - 单后端聚合（成功率、Token、成本、时延、估算率、错误分布）；
  - paired comparison 报告：legacy vs candidate 同输入成对对比，
    输出均值/p50/p95、bootstrap CI 与分类切片，成本门禁与质量门禁联动数据。
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from agent.evals.contracts import PairedEvalResult

BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 2026


def percentile(values: Sequence[float], p: float) -> float:
    """线性插值分位数（p 为 0-100）。"""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    if n == 1:
        return float(ordered[0])
    pos = (n - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def bootstrap_ci(
    values: Sequence[float],
    stat: str = "mean",
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    ci: float = 0.95,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """手写 bootstrap 置信区间。

    Args:
        values: 样本值
        stat: "mean" 或 "median"
        iterations: 重采样次数
        ci: 置信水平（如 0.95）
        seed: 随机种子（可重复）

    Returns:
        (lower, upper) 置信区间
    """
    samples = list(values)
    if not samples:
        return (0.0, 0.0)
    rng = random.Random(seed)
    stat_fn = statistics.mean if stat == "mean" else statistics.median
    boot: list[float] = []
    n = len(samples)
    for _ in range(iterations):
        resample = [samples[rng.randrange(n)] for _ in range(n)]
        boot.append(stat_fn(resample))
    alpha = (1 - ci) / 2
    return (percentile(boot, alpha * 100), percentile(boot, (1 - alpha) * 100))


def summarize(values: Sequence[float]) -> dict[str, float]:
    """均值/p50/p95/bootstrap CI 汇总。"""
    vals = [float(v) for v in values]
    lo, hi = bootstrap_ci(vals)
    return {
        "mean": round(statistics.mean(vals), 4) if vals else 0.0,
        "p50": round(percentile(vals, 50), 4),
        "p95": round(percentile(vals, 95), 4),
        "ci95_lo": round(lo, 4),
        "ci95_hi": round(hi, 4),
        "n": len(vals),
    }


def _token_total(tc: dict[str, Any]) -> float:
    return float(tc.get("input_tokens", 0)) + float(tc.get("output_tokens", 0))


def _check_pass(result: Any, check_name: str) -> bool:
    """取确定性评分中指定检查的通过状态（缺失视为未通过）。"""
    entry = (result.deterministic_scores or {}).get(check_name)
    return bool(entry and entry.get("pass"))


def aggregate_backend(results: list[Any]) -> dict[str, Any]:
    """单后端（legacy 或 candidate）聚合指标。"""
    total = len(results)
    success = [r for r in results if r.succeeded]
    token_totals = [_token_total(r.token_and_cost or {}) for r in results]
    costs = [float((r.token_and_cost or {}).get("cost_usd", 0.0)) for r in results]
    latencies = [r.latency_ms for r in results]
    est_ratio = (
        sum(1 for r in results if (r.token_and_cost or {}).get("usage_estimated")) / total
        if total
        else 0.0
    )
    errors: dict[str, int] = {}
    for r in results:
        if not r.succeeded:
            attr = r.failure_attribution or "unknown"
            errors[attr] = errors.get(attr, 0) + 1
    return {
        "total": total,
        "success_rate": round(len(success) / total, 4) if total else 0.0,
        "success_count": len(success),
        "tokens": summarize(token_totals),
        "cost_usd": summarize(costs),
        "latency_ms": summarize(latencies),
        "usage_estimated_ratio": round(est_ratio, 4),
        "error_rate": round(1 - (len(success) / total), 4) if total else 0.0,
        "errors_by_type": errors,
    }


def _pct_reduction(base: float, candidate: float) -> float:
    if base == 0:
        return 0.0
    return (base - candidate) / base * 100.0


def paired_report(
    pairs: list[PairedEvalResult],
    *,
    dataset_version: str = "",
    judge_calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成完整 paired comparison 报告（阶段2 §4/§6）。"""
    legacy_results = [p.legacy for p in pairs]
    candidate_results = [p.candidate for p in pairs]

    legacy_tokens = [_token_total(p.legacy.token_and_cost or {}) for p in pairs]
    cand_tokens = [_token_total(p.candidate.token_and_cost or {}) for p in pairs]
    legacy_costs = [float((p.legacy.token_and_cost or {}).get("cost_usd", 0.0)) for p in pairs]
    cand_costs = [float((p.candidate.token_and_cost or {}).get("cost_usd", 0.0)) for p in pairs]
    legacy_lat = [p.legacy.latency_ms for p in pairs]
    cand_lat = [p.candidate.latency_ms for p in pairs]

    # 逐条 paired 明细（含成本门禁联动所需的质/本对比）
    rows: list[dict[str, Any]] = []
    for p in pairs:
        lt = _token_total(p.legacy.token_and_cost or {})
        ct = _token_total(p.candidate.token_and_cost or {})
        lc = float((p.legacy.token_and_cost or {}).get("cost_usd", 0.0))
        cc = float((p.candidate.token_and_cost or {}).get("cost_usd", 0.0))
        rows.append(
            {
                "case_id": p.case_id,
                "category": p.category,
                "repetitions": p.repetitions,
                "legacy": {
                    "success": p.legacy.succeeded,
                    "terminal_status": p.legacy.terminal_status,
                    "tokens": lt,
                    "cost_usd": lc,
                    "latency_ms": p.legacy.latency_ms,
                    "deterministic_pass_rate": (
                        sum(1 for c in p.legacy.deterministic_scores.values() if c["pass"])
                        / len(p.legacy.deterministic_scores)
                        if p.legacy.deterministic_scores
                        else 0.0
                    ),
                    "tool_allowlist": _check_pass(p.legacy, "check_tool_allowlist"),
                },
                "candidate": {
                    "success": p.candidate.succeeded,
                    "terminal_status": p.candidate.terminal_status,
                    "tokens": ct,
                    "cost_usd": cc,
                    "latency_ms": p.candidate.latency_ms,
                    "deterministic_pass_rate": (
                        sum(1 for c in p.candidate.deterministic_scores.values() if c["pass"])
                        / len(p.candidate.deterministic_scores)
                        if p.candidate.deterministic_scores
                        else 0.0
                    ),
                    "tool_allowlist": _check_pass(p.candidate, "check_tool_allowlist"),
                },
                "token_delta_pct": round(_pct_reduction(lt, ct), 2),
                "cost_delta_pct": round(_pct_reduction(lc, cc), 2),
                "latency_delta_pct": round(
                    _pct_reduction(p.legacy.latency_ms, p.candidate.latency_ms), 2
                ),
                "quality_regressed": _quality_regressed(p),
            }
        )

    # 分类切片（paired 对比）
    categories: dict[str, dict[str, Any]] = {}
    for p in pairs:
        cat = categories.setdefault(p.category, {"n": 0, "token_deltas": [], "cost_deltas": []})
        cat["n"] += 1
        lt = _token_total(p.legacy.token_and_cost or {})
        ct = _token_total(p.candidate.token_and_cost or {})
        lc = float((p.legacy.token_and_cost or {}).get("cost_usd", 0.0))
        cc = float((p.candidate.token_and_cost or {}).get("cost_usd", 0.0))
        cat["token_deltas"].append(_pct_reduction(lt, ct))
        cat["cost_deltas"].append(_pct_reduction(lc, cc))
    by_category = {
        cat: {
            "n": info["n"],
            "token_reduction_pct": round(statistics.mean(info["token_deltas"]), 2)
            if info["token_deltas"]
            else 0.0,
            "cost_reduction_pct": round(statistics.mean(info["cost_deltas"]), 2)
            if info["cost_deltas"]
            else 0.0,
        }
        for cat, info in sorted(categories.items())
    }

    legacy_agg = aggregate_backend(legacy_results)
    cand_agg = aggregate_backend(candidate_results)
    if rows:
        legacy_agg["avg_deterministic_pass_rate"] = round(
            statistics.mean(r["legacy"]["deterministic_pass_rate"] for r in rows), 4
        )
        cand_agg["avg_deterministic_pass_rate"] = round(
            statistics.mean(r["candidate"]["deterministic_pass_rate"] for r in rows), 4
        )

    return {
        "schema_version": "2.0",
        "runner": "eval_harness_v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_version": dataset_version,
        "n_cases": len(pairs),
        "legacy": legacy_agg,
        "candidate": cand_agg,
        "paired": {
            "token_reduction_pct": round(_pct_reduction(sum(legacy_tokens), sum(cand_tokens)), 2),
            "token_delta_ci95": [
                round(
                    bootstrap_ci(
                        [
                            _pct_reduction(lt, ct)
                            for lt, ct in zip(legacy_tokens, cand_tokens, strict=True)
                        ]
                    )[0],
                    2,
                ),
                round(
                    bootstrap_ci(
                        [
                            _pct_reduction(lt, ct)
                            for lt, ct in zip(legacy_tokens, cand_tokens, strict=True)
                        ]
                    )[1],
                    2,
                ),
            ],
            "cost_reduction_pct": round(_pct_reduction(sum(legacy_costs), sum(cand_costs)), 2),
            "success_rate_delta": round(cand_agg["success_rate"] - legacy_agg["success_rate"], 4),
            "quality_pass_rate_delta": round(
                cand_agg.get("avg_deterministic_pass_rate", 0.0)
                - legacy_agg.get("avg_deterministic_pass_rate", 0.0),
                4,
            ),
            "latency_p95_reduction_pct": round(
                _pct_reduction(percentile(legacy_lat, 95), percentile(cand_lat, 95)), 2
            ),
            "quality_regressed_cases": [r["case_id"] for r in rows if r["quality_regressed"]],
        },
        "by_category": by_category,
        "results": rows,
        "judge_calibration": judge_calibration or {},
    }


def _quality_regressed(p: PairedEvalResult) -> bool:
    """成本/Token 降低但质量退化（阶段2 §6：成本门禁必须与质量门禁联动）。"""
    legacy_rate = (
        sum(1 for c in p.legacy.deterministic_scores.values() if c["pass"])
        / len(p.legacy.deterministic_scores)
        if p.legacy.deterministic_scores
        else 0.0
    )
    cand_rate = (
        sum(1 for c in p.candidate.deterministic_scores.values() if c["pass"])
        / len(p.candidate.deterministic_scores)
        if p.candidate.deterministic_scores
        else 0.0
    )
    return cand_rate < legacy_rate - 1e-9
