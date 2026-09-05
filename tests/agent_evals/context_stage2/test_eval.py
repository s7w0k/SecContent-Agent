"""Context Stage2 配对比较检查测试 -- 阶段二 Step 7。"""

from __future__ import annotations

from tests.agent_evals.context_stage2.deterministic_checks import (
    check_facts,
    check_latency,
    check_red_line,
    check_reference,
    check_token_reduction,
    load_dataset,
    run_pair_checks,
)
from tests.agent_evals.context_stage2.evaluator import run_all


class TestDataset:
    def test_dataset_has_90_items(self):
        items = load_dataset()
        assert len(items) == 90

    def test_dataset_purpose_counts(self):
        """评分 50 篇 + draft 20 + chat 20。"""
        from collections import Counter

        counts = Counter(item["purpose"] for item in load_dataset())
        assert counts["score"] == 50
        assert counts["draft"] == 20
        assert counts["chat"] == 20

    def test_dataset_fields(self):
        items = load_dataset()
        for item in items:
            assert item["id"]
            assert item["purpose"] in ("score", "draft", "chat")
            assert item["expected_reference"]
            assert "expected_facts" in item


class TestFactCheck:
    def test_facts_pass(self):
        result = check_facts("身份认证与授权管理", "身份认证", ["身份认证"])
        assert result["pass"]

    def test_facts_fail_stage2_missing(self):
        result = check_facts("身份认证与授权管理", "授权管理", ["身份认证"])
        assert not result["pass"]

    def test_no_facts_pass(self):
        result = check_facts("任意", "任意", [])
        assert result["pass"]


class TestReferenceCheck:
    def test_reference_pass(self):
        assert check_reference("来源 [overview.md] 引用标注", "overview.md")["pass"]

    def test_reference_fail(self):
        assert not check_reference("无来源标注", "overview.md")["pass"]

    def test_no_reference_requirement_pass(self):
        assert check_reference("任意", "")["pass"]


class TestRedLineCheck:
    def test_red_line_pass(self):
        result = check_red_line("合规基线", "合规基线", forbidden=["宣传"], required=["合规"])
        assert result["pass"]

    def test_forbidden_appears_fail(self):
        result = check_red_line("领先方案", "领先方案", forbidden=["领先"], required=[])
        assert not result["pass"]

    def test_required_missing_fail(self):
        result = check_red_line("方案介绍", "方案介绍", forbidden=[], required=["合规"])
        assert not result["pass"]


class TestTokenAndLatency:
    def test_token_reduction_pass(self):
        assert check_token_reduction(3200, 1600)["pass"]

    def test_token_degraded_fail(self):
        assert not check_token_reduction(1600, 3200)["pass"]

    def test_latency_pass(self):
        assert check_latency(5200.0, 3600.0)["pass"]

    def test_latency_degraded_fail(self):
        assert not check_latency(3600.0, 5200.0)["pass"]


class TestRunPairChecks:
    def test_pair_pass(self):
        item = {
            "expected_facts": ["身份认证"],
            "expected_reference": "overview.md",
            "red_line_forbidden": [],
            "red_line_required": ["合规"],
        }
        legacy = {"answer": "身份认证合规", "context_tokens": 3200, "latency_ms": 5200.0}
        stage2 = {
            "answer": "身份认证合规，来源 [overview.md]",
            "context_tokens": 1600,
            "latency_ms": 3600.0,
        }
        check = run_pair_checks(item, legacy, stage2)
        assert check["pass"]

    def test_pair_fail_on_reference(self):
        item = {
            "expected_facts": ["身份认证"],
            "expected_reference": "sales-brief.md",
            "red_line_forbidden": [],
            "red_line_required": [],
        }
        legacy = {"answer": "身份认证", "context_tokens": 3200, "latency_ms": 5200.0}
        stage2 = {
            "answer": "身份认证，来源 [overview.md]",
            "context_tokens": 1600,
            "latency_ms": 3600.0,
        }
        check = run_pair_checks(item, legacy, stage2)
        assert not check["pass"]


class TestEvaluatorRunAll:
    def test_run_all_returns_report(self):
        report = run_all()
        assert report["total"] == 90
        assert report["passed"] + report["failed"] == report["total"]

    def test_mock_results_all_pass(self):
        """mock 配对结果应全部通过确定性检查。"""
        report = run_all()
        assert report["failed"] == 0, (
            f"Mock 检查有失败: {[r['id'] for r in report['results'] if not r['pass']]}"
        )

    def test_gates_all_pass(self):
        """聚合门禁（事实≥98%、引用、红线 100%、token≥30%、p95≥20%）应全过。"""
        report = run_all()
        for name, gate in report["gates"].items():
            assert gate["pass"], f"门禁未通过: {name} = {gate['value']:.1f}%"
