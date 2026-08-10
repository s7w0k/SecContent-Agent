"""Legacy 基线采集流程可重复性测试 -- 阶段 0.4。

验证：固定样本集重复 N 次采集可生成结构完整的报告；
mock LLM 下指标确定、可重复（报告可在干净环境重建）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from collect_legacy_baseline import (  # noqa: E402
    _MockLLM,
    aggregate,
    collect_once,
    load_dataset,
)


def _run(llm, items, runs: int) -> dict:
    async def _go():
        collected = []
        for _ in range(runs):
            collected.append(await collect_once(llm, items, model_name="deepseek-chat"))
        return collected

    return aggregate(asyncio.run(_go()), model_name="deepseek-chat", llm_backend="mock")


def test_dataset_fixed_and_loaded():
    items = load_dataset()
    assert len(items) >= 40
    assert items[0]["question"]


class TestBaselineCollector:
    def setup_method(self):
        self.items = load_dataset()[:5]  # 固定子集，保证测试速度
        self.llm = _MockLLM(model_name="deepseek-chat")

    def test_collect_once_produces_rows(self):
        rows = asyncio.run(collect_once(self.llm, self.items, model_name="deepseek-chat"))
        assert len(rows) == len(self.items)
        for row in rows:
            assert row["success"] is True
            assert row["error_type"] is None
            assert row["input_tokens"] > 0
            assert row["cost_usd"] >= 0
            assert "latency_ms" in row

    def test_repeat_runs_produce_identical_report(self):
        """同一样本集 + 同一 mock LLM → 3 轮采集的稳定性指标完全一致（可重复）。

        latency_ms（asyncio 调度时延）与 generated_at（时间戳）天然随运行变化，
        不作为可重复性断言对象；仅比较质量/成本/token 等确定性指标。
        """
        report_a = _run(self.llm, self.items, runs=3)
        report_b = _run(self.llm, self.items, runs=3)

        def _stability(report: dict) -> dict:
            return {
                "runs": report["runs"],
                "sample_size": report["sample_size"],
                "llm_backend": report["llm_backend"],
                "model_name": report["model_name"],
                "overall": report["overall"],
                "tokens": report["tokens"],
                "cost": report["cost"],
                "errors": report["errors"],
            }

        assert _stability(report_a) == _stability(report_b)

    def test_report_fields_aligned_with_spec(self):
        """报告字段与 spec 0.4 指标对齐。"""
        report = _run(self.llm, self.items, runs=3)
        assert report["runs"] == 3
        assert report["sample_size"] == 5
        assert report["llm_backend"] == "mock"
        assert report["overall"]["success_rate"] == 1.0
        assert report["overall"]["fact_support_rate"] == 1.0  # mock 命中 expected keywords
        assert report["overall"]["llm_calls"] == 15
        assert report["overall"]["tool_calls"] == 0  # legacy 无工具
        assert report["overall"]["retries"] == 0
        assert report["tokens"]["input_total"] > 0
        assert "e2e_p50" in report["latency_ms"]
        assert "e2e_p95" in report["latency_ms"]
        assert report["cost"]["currency"] == "USD"
        assert report["cost"]["usd_per_request"] >= 0
        assert report["cost"]["usd_per_success"] >= 0
        assert report["errors"]["error_rate"] == 0.0
        assert report["errors"]["by_type"] == {}

    def test_error_path_recorded(self):
        """单条调用失败时记录错误类型且不中断整轮。"""

        class _FailingLLM:
            model_name = "deepseek-chat"

            async def answer(self, message, expected):
                raise TimeoutError("boom")

        rows = asyncio.run(collect_once(_FailingLLM(), self.items, model_name="deepseek-chat"))
        assert all(not r["success"] for r in rows)
        assert all(r["error_type"] == "TimeoutError" for r in rows)
        assert all(r["fact_support_passed"] is False for r in rows)

    def test_estimated_tokens_when_usage_missing(self):
        """provider usage 缺失时保守估算并标记 usage_estimated。"""

        class _NoUsageLLM:
            model_name = "deepseek-chat"

            async def answer(self, message, expected):
                return {"answer": "ok", "references": [], "metadata": {"usage": {}}}

        rows = asyncio.run(collect_once(_NoUsageLLM(), self.items, model_name="deepseek-chat"))
        assert all(r["usage_estimated"] for r in rows)
        assert all(r["input_tokens"] >= 1 for r in rows)
