"""阶段0 知识检索评测集与旧链路基线测试。"""

from __future__ import annotations

from collections import Counter

from tests.agent_evals.knowledge_retrieval.deterministic_checks import (
    check_expansion,
    check_forbidden,
    check_no_hit,
    check_top1,
    check_top2_recall,
    load_dataset,
    run_checks,
)
from tests.agent_evals.knowledge_retrieval.evaluator import run_all
from tests.agent_evals.knowledge_retrieval.generate_query_dataset import (
    _build_samples as _build_query_samples,
)
from tests.agent_evals.knowledge_retrieval.generate_query_dataset import (
    generate as generate_query,
)
from tests.agent_evals.knowledge_retrieval.generate_user_input_dataset import (
    _build_samples,
)
from tests.agent_evals.knowledge_retrieval.generate_user_input_dataset import (
    generate as generate_v2,
)


class TestDataset:
    def test_dataset_size(self):
        items = load_dataset()
        assert 60 <= len(items) <= 100, f"评测集规模应在 60-100，当前 {len(items)}"

    def test_dataset_distribution(self):
        """45 正例（3 产品各15）+ 多产品 + 无命中 + 章节展开。"""
        items = load_dataset()
        expected = [tuple(c["expected_product_ids"]) for c in items]
        counts = Counter(tuple(sorted(pe)) for pe in expected)
        # 三个已发布产品的单产品正例各 ≥15
        for pid in ("agent-identity-security", "agent-security", "ai-bom"):
            assert counts[(pid,)] >= 15, f"{pid} 单产品正例应 ≥15，当前 {counts[(pid,)]}"
        # 无命中用例 ≥ 10
        assert counts[()] >= 10, f"无命中用例应 ≥10，当前 {counts[()]}"
        # 多产品用例 ≥ 10
        multi = sum(v for k, v in counts.items() if len(k) >= 2)
        assert multi >= 10, f"多产品用例应 ≥10，当前 {multi}"
        # 章节展开用例 ≥ 10
        expansion = sum(1 for c in items if c.get("requires_expansion"))
        assert expansion >= 10, f"章节展开用例应 ≥10，当前 {expansion}"

    def test_dataset_fields(self):
        items = load_dataset()
        for case in items:
            assert case["case_id"]
            assert case["mode"] in ("selected", "auto", "none")
            assert isinstance(case["article"], dict)
            assert case["article"].get("title")
            assert "expected_product_ids" in case
            assert "required_doc_ids" in case
            assert "forbidden_product_ids" in case
            assert "requires_expansion" in case
            assert "allowed_product_claims" in case

    def test_all_expected_products_are_published(self):
        """期望产品必须属于已发布产品目录。"""
        published = {"agent-identity-security", "agent-security", "ai-bom"}
        for case in load_dataset():
            for pid in case["expected_product_ids"]:
                assert pid in published, f"{case['case_id']} 期望产品 {pid} 未发布"

    def test_unique_case_ids(self):
        ids = [c["case_id"] for c in load_dataset()]
        assert len(ids) == len(set(ids)), "case_id 必须唯一"


class TestCheckFunctions:
    def test_top1_hit(self):
        assert check_top1(["ai-bom", "agent-security"], ["ai-bom"])["pass"]
        assert not check_top1(["agent-security"], ["ai-bom"])["pass"]

    def test_top2_recall(self):
        assert check_top2_recall(["agent-security", "ai-bom"], ["ai-bom"])["pass"]
        assert not check_top2_recall(["agent-security"], ["ai-bom"])["pass"]

    def test_forbidden(self):
        assert check_forbidden(["ai-bom"], ["agent-security"])["pass"]
        assert not check_forbidden(["agent-security"], ["agent-security"])["pass"]

    def test_no_hit(self):
        assert check_no_hit([], [])["pass"]
        assert not check_no_hit(["ai-bom"], [])["pass"]

    def test_expansion(self):
        assert check_expansion(
            requires_expansion=True,
            required_doc_ids=["3-AI-BOM/overview.md"],
            predicted=["ai-bom"],
        )["pass"]
        assert not check_expansion(
            requires_expansion=True,
            required_doc_ids=[],
            predicted=["ai-bom"],
        )["pass"]


class TestRunChecks:
    def test_run_checks_pass(self):
        case = {
            "expected_product_ids": ["ai-bom"],
            "forbidden_product_ids": [],
            "requires_expansion": False,
        }
        check = run_checks(case, ["ai-bom"])
        assert check["pass"]

    def test_run_checks_fail_forbidden(self):
        case = {
            "expected_product_ids": ["ai-bom"],
            "forbidden_product_ids": ["agent-security"],
            "requires_expansion": False,
        }
        check = run_checks(case, ["agent-security"])
        assert not check["pass"]


class TestEvaluatorRunAll:
    def test_run_all_returns_report(self):
        report = run_all()
        assert report["total"] == len(load_dataset())
        assert report["passed"] + report["failed"] == report["total"]
        assert "gates" in report
        assert "top1_accuracy" in report
        assert "expansion_coverage" in report

    def test_gate_keys(self):
        report = run_all()
        for name in ("top1_accuracy", "top2_recall", "forbidden_isolation", "no_hit_no_fabrication", "expansion_coverage"):
            assert name in report["gates"], f"缺少门禁 {name}"


class TestV2UserInputDataset:
    """多角色真实用户输入评测集（u-* 用例）。"""

    def test_generate_writes_file(self):
        samples = generate_v2()
        assert len(samples) >= 8, "v2 评测集应至少 8 条"
        ids = {s["case_id"] for s in samples}
        assert len(ids) == len(samples), "case_id 必须唯一"

    def test_cases_have_role_and_tone(self):
        for case in _build_samples():
            assert case.get("role"), f"{case['case_id']} 缺少 role"
            assert case.get("tone"), f"{case['case_id']} 缺少 tone"

    def test_expected_products_published(self):
        published = {"agent-identity-security", "agent-security", "ai-bom"}
        for case in _build_samples():
            for pid in case["expected_product_ids"]:
                assert pid in published, f"{case['case_id']} 期望未发布产品 {pid}"

    def test_forbidden_excludes_expected(self):
        for case in _build_samples():
            for pid in case["expected_product_ids"]:
                assert pid not in case["forbidden_product_ids"], (
                    f"{case['case_id']} 禁止产品混入期望产品 {pid}"
                )

    def test_covers_multiple_roles(self):
        roles = {c.get("role") for c in _build_samples()}
        assert len(roles) >= 5, f"应覆盖至少 5 种用户角色，当前 {len(roles)}"

    def test_v2_covers_no_hit_case(self):
        """v2 应含无命中用例（测不编造产品）。"""
        assert any(not c["expected_product_ids"] for c in _build_samples())

    def test_v2_run_all_passes_gates(self):
        """v2 多角色真实输入在旧链路规则下应通过全部门禁。"""
        report = run_all(dataset_version="v2")
        assert report["dataset_version"] == "v2"
        for name, g in report["gates"].items():
            assert g["pass"], f"v2 门禁 {name} 未通过: {g}"
        assert report["top1_accuracy"] >= 0.90


class TestQueryDataset:
    """真实线上用户 query 短句评测集（q-* 用例，100 条）。"""

    def test_generate_size_100(self):
        samples = generate_query()
        assert len(samples) == 100, f"query 评测集应 100 条，当前 {len(samples)}"

    def test_unique_case_ids(self):
        ids = [c["case_id"] for c in _build_query_samples()]
        assert len(ids) == len(set(ids)), "case_id 必须唯一"

    def test_query_short_form(self):
        """query 应作为 article.title 短句传入，无正文。"""
        for case in _build_query_samples():
            assert case["query"], f"{case['case_id']} 缺少 query"
            assert case["article"]["title"] == case["query"]
            assert case["article"]["content_md"] == ""
            assert case["input_type"] == "user-query"

    def test_expected_products_published(self):
        published = {"agent-identity-security", "agent-security", "ai-bom"}
        for case in _build_query_samples():
            for pid in case["expected_product_ids"]:
                assert pid in published, f"{case['case_id']} 期望未发布产品 {pid}"

    def test_forbidden_excludes_expected(self):
        for case in _build_query_samples():
            for pid in case["expected_product_ids"]:
                assert pid not in case["forbidden_product_ids"], (
                    f"{case['case_id']} 禁止产品混入期望产品 {pid}"
                )

    def test_pressure_scenarios_present(self):
        """应覆盖单产品/多产品/无命中/章节展开/竞品隔离。"""
        samples = _build_query_samples()
        exp = [tuple(sorted(c["expected_product_ids"])) for c in samples]
        counts = Counter(exp)
        for pid in ("agent-identity-security", "agent-security", "ai-bom"):
            assert counts[(pid,)] >= 15, f"{pid} 单产品用例应 ≥15"
        assert counts[()] >= 10, f"无命中用例应 ≥10，当前 {counts[()]}"
        multi = sum(v for k, v in counts.items() if len(k) >= 2)
        assert multi >= 10, f"多产品用例应 ≥10，当前 {multi}"
        expansion = sum(1 for c in samples if c.get("requires_expansion"))
        assert expansion >= 10, f"章节展开用例应 ≥10，当前 {expansion}"
        # 竞品隔离：存在显式 forbidden 且非默认的用例
        nondefault_forbid = sum(
            1 for c in samples
            if c["forbidden_product_ids"] and c["forbidden_product_ids"] != [p for p in ("agent-identity-security", "agent-security", "ai-bom") if p not in c["expected_product_ids"]]
        )
        assert nondefault_forbid >= 3, f"竞品隔离用例应 ≥3，当前 {nondefault_forbid}"

    def test_query_run_all_passes_gates(self):
        """100 条真实线上短句在旧链路规则下应通过全部门禁。"""
        report = run_all(dataset_version="query")
        assert report["dataset_version"] == "query"
        assert report["total"] == 100
        for name, g in report["gates"].items():
            assert g["pass"], f"query 门禁 {name} 未通过: {g}"
        assert report["top1_accuracy"] >= 0.90
