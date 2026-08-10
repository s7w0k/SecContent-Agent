"""Legacy 基线采集脚本 — 阶段 0.4。

在固定样本集上重复运行 N 次，采集 legacy（非 Agent）问答路径的
质量、Token、成本、时延与错误基线。报告可重复生成。

用法（仓库根目录）:
    python scripts/collect_legacy_baseline.py                 # 默认 3 轮，自动选择后端
    python scripts/collect_legacy_baseline.py --runs 3 --llm mock --output reports/legacy-baseline.json

llm 后端:
    real: 使用 DeepSeek（需 .env 中 DEEPSEEK_API_KEY 为真实 key）
    mock: 确定性 mock LLM（无 key 时验证采集流程可重复，指标为流程校验值）
    自动: 有真实 key 且非占位符时用 real，否则 mock

指标口径与 spec（01-阶段0-基线冻结与工程门禁清零.md §0.4）对齐：
    成功率、事实支持率、tokens、LLM 调用次数、重试次数、
    p50/p95 端到端时延、USD/request 与 USD/success、错误率与失败类型分布。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "services" / "backend"
DATASET_PATH = REPO_ROOT / "tests" / "agent_evals" / "chat_stage1" / "dataset.v1.jsonl"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"

sys.path.insert(0, str(BACKEND_SRC))

from agent.pricing_catalog import compute_cost  # noqa: E402


def load_dataset(path: Path = DATASET_PATH) -> list[dict]:
    items: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


class _MockLLM:
    """确定性 mock LLM：模拟 legacy 单轮调用、usage 与轻微时延。"""

    def __init__(self, model_name: str = "deepseek-chat"):
        self.model_name = model_name
        self._delay = 0.001

    async def answer(self, message: str, expected_keywords: list[str]) -> dict:
        """返回 (result, metadata)。result 结构与 legacy 路径对齐。"""
        import asyncio

        await asyncio.sleep(self._delay)
        answer = "基于产品知识库的回答：" + "，".join(expected_keywords or ["回答"])
        return {
            "answer": answer,
            "references": [],
            "metadata": {
                "model_name": self.model_name,
                "usage": {
                    "input_tokens": max(1, len(message) // 4),
                    "output_tokens": max(1, len(answer) // 4),
                    "total_tokens": max(1, len(message) // 4) + max(1, len(answer) // 4),
                    "prompt_cache_hit_tokens": 0,
                },
            },
        }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * p / 100)))
    return ordered[idx]


def _fact_support_passed(item: dict, answer: str) -> bool:
    expected = item.get("expected_answer_contains", [])
    if not expected:
        return True
    return all(kw in answer for kw in expected)


async def collect_once(
    llm: Any,
    items: list[dict],
    *,
    model_name: str,
) -> list[dict]:
    """单轮采集：对每个样本执行一次 legacy 问答，返回逐条结果。"""
    rows: list[dict] = []
    for item in items:
        start = time.perf_counter()
        error_type: str | None = None
        answer = ""
        usage: dict = {}
        try:
            result = await llm.answer(
                item.get("question", ""),
                item.get("expected_answer_contains", []),
            )
            answer = result.get("answer", "")
            usage = (result.get("metadata") or {}).get("usage", {})
        except Exception as e:  # 基线采集需记录任意失败
            error_type = type(e).__name__
        elapsed_ms = (time.perf_counter() - start) * 1000

        # token 口径：优先 provider usage，缺失时保守估算并标记 estimated
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cached_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        estimated = input_tokens is None or output_tokens is None
        if input_tokens is None:
            input_tokens = max(1, len(item.get("question", "")) // 4)
        if output_tokens is None:
            output_tokens = max(1, len(answer) // 4) if answer else 0

        cost = compute_cost(
            model_name,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            cached_input_tokens=cached_tokens,
            input_tokens_estimated=estimated,
            output_tokens_estimated=estimated,
        )

        rows.append(
            {
                "sample_id": item.get("id", ""),
                "category": item.get("category", ""),
                "success": error_type is None,
                "error_type": error_type,
                "fact_support_passed": _fact_support_passed(item, answer)
                if error_type is None
                else False,
                "latency_ms": round(elapsed_ms, 3),
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "cached_input_tokens": cached_tokens,
                "usage_estimated": estimated,
                "cost_usd": cost["cost_usd"],
            }
        )
    return rows


def aggregate(runs: list[list[dict]], *, model_name: str, llm_backend: str) -> dict:
    """将 N 轮采集结果聚合为基线指标。"""
    flat = [row for run in runs for row in run]
    total = len(flat)
    success = [r for r in flat if r["success"]]
    latencies = [r["latency_ms"] for r in flat]
    costs = [r["cost_usd"] for r in flat]
    est_ratio = sum(1 for r in flat if r["usage_estimated"]) / total if total else 0.0
    errors: dict[str, int] = {}
    for r in flat:
        if not r["success"]:
            errors[r["error_type"] or "unknown"] = errors.get(r["error_type"] or "unknown", 0) + 1

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "runs": len(runs),
        "sample_size": len(runs[0]) if runs else 0,
        "llm_backend": llm_backend,
        "model_name": model_name,
        "overall": {
            "total_requests": total,
            "success_rate": round(len(success) / total, 4) if total else 0.0,
            "fact_support_rate": round(sum(1 for r in flat if r["fact_support_passed"]) / total, 4)
            if total
            else 0.0,
            "llm_calls": total,
            "tool_calls": 0,  # legacy 路径无工具调用
            "retries": 0,  # 基线为单次调用，重试计 0（重试属后续阶段治理项）
        },
        "tokens": {
            "input_total": sum(r["input_tokens"] for r in flat),
            "output_total": sum(r["output_tokens"] for r in flat),
            "cached_input_total": sum(r["cached_input_tokens"] for r in flat),
            "usage_estimated_ratio": round(est_ratio, 4),
        },
        "latency_ms": {
            "e2e_p50": round(_percentile(latencies, 50), 3),
            "e2e_p95": round(_percentile(latencies, 95), 3),
        },
        "cost": {
            "total_usd": round(sum(costs), 8),
            "usd_per_request": round(statistics.mean(costs), 8) if costs else 0.0,
            "usd_per_success": round(statistics.mean([r["cost_usd"] for r in success]), 8)
            if success
            else 0.0,
            "currency": "USD",
        },
        "errors": {
            "error_rate": round((total - len(success)) / total, 4) if total else 0.0,
            "by_type": errors,
        },
    }


def _has_real_key() -> bool:
    import os

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    return bool(key) and "your-" not in key and "placeholder" not in key.lower()


def main() -> None:
    parser = argparse.ArgumentParser(description="Legacy 基线采集（阶段 0.4）")
    parser.add_argument("--runs", type=int, default=3, help="重复运行轮数（默认 3）")
    parser.add_argument("--llm", choices=["real", "mock", "auto"], default="auto")
    parser.add_argument("--sample-size", type=int, default=0, help="样本数（0=全部）")
    parser.add_argument("--output", type=str, default="", help="报告输出路径")
    args = parser.parse_args()

    items = load_dataset()
    if args.sample_size > 0:
        items = items[: args.sample_size]

    backend = args.llm
    if backend == "auto":
        backend = "real" if _has_real_key() else "mock"
    model_name = "deepseek-chat"

    if backend == "real":
        # 真实 DeepSeek 调用（需要有效 API key）
        from config import get_settings
        from langchain_openai import ChatOpenAI

        s = get_settings()
        real_llm = ChatOpenAI(
            model=model_name,
            api_key=s.DEEPSEEK_API_KEY,
            base_url=s.DEEPSEEK_BASE_URL,
            timeout=s.DEEPSEEK_TIMEOUT,
        )

        async def _answer(message, expected):
            from langchain_core.messages import HumanMessage, SystemMessage

            msg = await real_llm.ainvoke(
                [
                    SystemMessage(content="你是 PR 情报助手，请简洁回答。"),
                    HumanMessage(content=message),
                ]
            )
            usage = getattr(msg, "usage_metadata", None) or {}
            return {
                "answer": msg.content if isinstance(msg.content, str) else str(msg.content),
                "references": [],
                "metadata": {"model_name": model_name, "usage": usage},
            }

        llm_obj: Any = type("_RealLLM", (), {"answer": _answer, "model_name": model_name})()
    else:
        llm_obj = _MockLLM(model_name=model_name)

    async def _run() -> list[list[dict]]:
        runs: list[list[dict]] = []
        for i in range(args.runs):
            print(f"run {i + 1}/{args.runs} ...")
            runs.append(await collect_once(llm_obj, items, model_name=model_name))
        return runs

    import asyncio

    runs = asyncio.run(_run())

    report = aggregate(runs, model_name=model_name, llm_backend=backend)

    out_path = (
        Path(args.output)
        if args.output
        else DEFAULT_REPORTS_DIR
        / f"legacy-baseline-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report written: {out_path}")


if __name__ == "__main__":
    main()
