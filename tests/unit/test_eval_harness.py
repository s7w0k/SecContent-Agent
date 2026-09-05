"""真实 Agent Eval Harness 单元测试 -- 阶段2（WBS 2.1-2.6）。

覆盖：数据集治理、契约、确定性评分器、paired 统计与门禁、
LLM-as-Judge、mock 模拟器与真实双轨 runner。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.evals.contracts import EvalCase, EvalResult, PairedEvalResult
from agent.evals.dataset import (
    VALID_SPLITS,
    DatasetError,
    coverage_report,
    dataset_fingerprint,
    group_split,
    load_dataset,
    parse_dataset_version,
)
from agent.evals.gates import GateViolation, evaluate_gates
from agent.evals.judge import Judge, calibration_stats, flag_disagreements
from agent.evals.mock_llm import MockLegacyLLM, MockToolLLM
from agent.evals.report import (
    aggregate_backend,
    bootstrap_ci,
    paired_report,
    percentile,
)
from agent.evals.runner import EvalRunner, FixtureTool
from agent.evals.scorers import (
    check_budget,
    check_evidence,
    check_forbidden_claims,
    check_repeated_side_effect,
    check_required_facts,
    check_terminal_status,
    check_tool_allowlist,
    run_deterministic_scores,
)

DATASET = Path(__file__).parent.parent / "agent_evals" / "eval_datasets" / "real_v1.jsonl"


# ── fixture 构造 ──────────────────────────────────────────────


def _case(**kw) -> EvalCase:
    d = {
        "case_id": "T001",
        "dataset_version": "test_v1",
        "category": "no_tool",
        "split": "train",
        "input_fixture": {"question": "测试问题？", "tool_script": []},
        "allowed_tools": [],
        "expected_terminal_status": "completed",
        "max_steps": 3,
        "max_tokens": 1000,
    }
    d.update(kw)
    return EvalCase(**d)


def _result(**kw) -> EvalResult:
    d = {
        "backend": "candidate",
        "actual_output": "答案",
        "terminal_status": "completed",
    }
    d.update(kw)
    return EvalResult(**d)


# ── 数据集治理 ────────────────────────────────────────────────


class TestDataset:
    def test_parse_version(self):
        assert parse_dataset_version("real_v1.jsonl") == ("real", 1)
        assert parse_dataset_version("agent_eval_v3.jsonl") == ("agent_eval", 3)
        with pytest.raises(DatasetError):
            parse_dataset_version("no_version.jsonl")

    def test_load_real_dataset(self):
        cases = load_dataset(DATASET)
        assert len(cases) >= 30
        assert all(c.split in VALID_SPLITS for c in cases)
        # 全覆盖清单
        cov = coverage_report(cases)
        assert cov["missing_categories"] == []

    def test_load_rejects_duplicate_case_id(self, tmp_path):
        p = tmp_path / "dup_v1.jsonl"
        line = (
            '{"case_id":"X","category":"no_tool","split":"train","input_fixture":{"question":"q"}}'
        )
        p.write_text(line + "\n" + line, encoding="utf-8")
        with pytest.raises(DatasetError, match="重复 case_id"):
            load_dataset(p)

    def test_load_rejects_invalid_split(self, tmp_path):
        p = tmp_path / "bad_v1.jsonl"
        p.write_text(
            '{"case_id":"X","category":"no_tool","split":"other",'
            '"input_fixture":{"question":"q"}}\n',
            encoding="utf-8",
        )
        with pytest.raises(DatasetError, match="非法 split"):
            load_dataset(p)

    def test_group_split_no_leakage(self):
        """同 payload_hash 的组必须整体进入同一切分（无泄漏）。"""
        cases = load_dataset(DATASET)
        base = cases[0]
        # 两个未标记 split 的近似样本（同一输入+租户 fixture -> 同一 payload_hash）
        un1 = _case(
            case_id="U1",
            split="unassigned",
            input_fixture=base.input_fixture,
            tenant_fixture=base.tenant_fixture,
        )
        un2 = _case(
            case_id="U2",
            split="unassigned",
            input_fixture=base.input_fixture,
            tenant_fixture=base.tenant_fixture,
        )
        assert un1.payload_hash() == un2.payload_hash()
        splits = group_split([un1, un2, *cases], seed=7)
        all_ids = {c.case_id for s in VALID_SPLITS for c in splits[s]}
        assert {"U1", "U2"} <= all_ids
        for s in VALID_SPLITS:
            ids = {c.case_id for c in splits[s]}
            if "U1" in ids:
                assert "U2" in ids

    def test_fingerprint_stable(self):
        cases = load_dataset(DATASET)
        assert dataset_fingerprint(cases) == dataset_fingerprint(cases)


# ── 契约 ──────────────────────────────────────────────────────


class TestContracts:
    def test_from_dict_defaults(self):
        c = EvalCase.from_dict({"case_id": "X", "category": "no_tool"})
        assert c.split == "validation"
        assert c.expected_terminal_status == "completed"
        assert c.question == ""

    def test_payload_hash_stable_and_input_sensitive(self):
        c1 = _case()
        c2 = _case()
        assert c1.payload_hash() == c2.payload_hash()
        c3 = _case(input_fixture={"question": "另一个问题？", "tool_script": []})
        assert c1.payload_hash() != c3.payload_hash()

    def test_tools_used_dedup(self):
        r = _result(
            tool_trace=[
                {"tool_name": "a"},
                {"tool_name": "b"},
                {"tool_name": "a"},
            ]
        )
        assert r.tools_used == ["a", "b"]


# ── 确定性评分器 ──────────────────────────────────────────────


class TestScorers:
    def test_terminal_status_match(self):
        c = _case(expected_terminal_status="budget_exceeded")
        assert check_terminal_status(c, _result(terminal_status="budget_exceeded"))["pass"]
        assert not check_terminal_status(c, _result(terminal_status="completed"))["pass"]

    def test_tool_allowlist(self):
        c = _case(allowed_tools=["search_knowledge"], forbidden_tools=["get_article"])
        r = _result(tool_trace=[{"tool_name": "search_knowledge"}])
        assert check_tool_allowlist(c, r)["pass"]
        r_bad = _result(tool_trace=[{"tool_name": "get_article"}])
        assert not check_tool_allowlist(c, r_bad)["pass"]
        r_extra = _result(tool_trace=[{"tool_name": "unknown_tool"}])
        assert not check_tool_allowlist(c, r_extra)["pass"]

    def test_required_facts(self):
        c = _case(required_facts=["事实A", "事实B"])
        ok = check_required_facts(c, _result(actual_output="包含事实A与事实B。"))
        assert ok["pass"]
        bad = check_required_facts(c, _result(actual_output="只有事实A。"))
        assert not bad["pass"]
        assert "事实B" in bad["reason"]

    def test_forbidden_claims(self):
        c = _case(forbidden_claims=["编造数据"])
        assert check_forbidden_claims(c, _result(actual_output="合规回答。"))["pass"]
        assert not check_forbidden_claims(c, _result(actual_output="我编造数据了。"))["pass"]

    def test_evidence_from_source_ids(self):
        c = _case(required_evidence=["kb/ztna"])
        r = _result(evidence_trace=["kb/ztna"])
        assert check_evidence(c, r)["pass"]
        assert not check_evidence(c, _result(evidence_trace=[]))["pass"]

    def test_budget_respects_expected_exceeded(self):
        # 预期 budget_exceeded 的用例：触发步数上限不算越限
        c = _case(expected_terminal_status="budget_exceeded", max_steps=2, max_tokens=100)
        r = _result(
            tool_trace=[{"tool_name": "a"}, {"tool_name": "b"}],
            llm_events=[{}],
            token_and_cost={"input_tokens": 60, "output_tokens": 40},
        )
        assert check_budget(c, r)["pass"]

    def test_repeated_side_effect(self):
        c = _case()
        dup = _result(tool_trace=[{"tool_name": "publish_draft"}, {"tool_name": "publish_draft"}])
        assert not check_repeated_side_effect(c, dup)["pass"]
        safe = _result(
            tool_trace=[{"tool_name": "search_knowledge"}, {"tool_name": "search_knowledge"}]
        )
        assert check_repeated_side_effect(c, safe)["pass"]

    def test_run_deterministic_scores_all_checks(self):
        c = _case(
            allowed_tools=["search_knowledge"],
            required_facts=["持续验证"],
            required_evidence=["kb/ztna"],
            max_tokens=1000,
            tenant_fixture={"allowed_product_ids": ["p1"]},
        )
        r = _result(
            actual_output="基于资料：持续验证。",
            evidence_trace=["kb/ztna"],
            tool_trace=[{"tool_name": "search_knowledge"}],
            token_and_cost={"input_tokens": 10, "output_tokens": 10},
        )
        scores = run_deterministic_scores(c, r)
        assert len(scores) == 9
        assert all(v["pass"] for v in scores.values())


# ── 统计与 paired 报告 ────────────────────────────────────────


class TestStats:
    def test_percentile(self):
        assert percentile([1, 2, 3, 4], 50) == 2.5
        assert percentile([5], 95) == 5.0

    def test_bootstrap_ci_sane(self):
        vals = list(range(1, 101))
        lo, hi = bootstrap_ci(vals, iterations=500, seed=1)
        assert 0 < lo <= hi < 110

    def test_aggregate_backend(self):
        results = [
            _result(
                terminal_status="completed", token_and_cost={"input_tokens": 5, "output_tokens": 5}
            ),
            _result(
                terminal_status="completed",
                token_and_cost={"input_tokens": 10, "output_tokens": 10},
            ),
        ]
        agg = aggregate_backend(results)
        assert agg["success_rate"] == 1.0
        assert agg["tokens"]["mean"] == 15.0

    def test_paired_report_structure(self):
        cases = load_dataset(DATASET)[:3]
        pairs: list[PairedEvalResult] = []
        for c in cases:
            legacy = _result(backend="legacy", actual_output="legacy 答案")
            cand = _result(backend="candidate", actual_output="candidate 答案")
            legacy.deterministic_scores = run_deterministic_scores(c, legacy)
            cand.deterministic_scores = run_deterministic_scores(c, cand)
            pairs.append(
                PairedEvalResult(
                    case_id=c.case_id, category=c.category, legacy=legacy, candidate=cand
                )
            )
        report = paired_report(pairs, dataset_version="test_v1")
        assert report["runner"] == "eval_harness_v2"
        assert "tool_allowlist" in report["results"][0]["candidate"]
        assert report["n_cases"] == 3


# ── 门禁 ──────────────────────────────────────────────────────


def _report_from_pairs(pairs: list[PairedEvalResult], **kwargs) -> dict:
    return paired_report(pairs, dataset_version="test_v1", **kwargs)


class TestGates:
    @staticmethod
    def _all_pass_scores() -> dict:
        checks = [
            "check_terminal_status",
            "check_tool_allowlist",
            "check_required_facts",
            "check_forbidden_claims",
            "check_evidence",
            "check_budget",
            "check_latency",
            "check_permission",
            "check_repeated_side_effect",
        ]
        return {name: {"pass": True, "value": 1, "reason": ""} for name in checks}

    def test_pr_gates_all_pass(self):
        cases = load_dataset(DATASET)[:5]
        pairs: list[PairedEvalResult] = []
        for c in cases:
            cand = _result(backend="candidate", actual_output="答案")
            cand.deterministic_scores = self._all_pass_scores()
            pairs.append(
                PairedEvalResult(
                    case_id=c.case_id, category=c.category, legacy=_result(), candidate=cand
                )
            )
        report = _report_from_pairs(pairs)
        gates = evaluate_gates(report, "pr")
        assert all(g["pass"] for g in gates.values())

    def test_pr_contract_hard_gate_blocks(self):
        cases = load_dataset(DATASET)[:3]
        pairs: list[PairedEvalResult] = []
        for i, c in enumerate(cases):
            cand = _result(backend="candidate", actual_output="")
            cand.deterministic_scores = {
                **self._all_pass_scores(),
                "check_terminal_status": {"pass": i != 1, "value": 1, "reason": ""},
            }
            pairs.append(
                PairedEvalResult(
                    case_id=c.case_id, category=c.category, legacy=_result(), candidate=cand
                )
            )
        report = _report_from_pairs(pairs)
        with pytest.raises(GateViolation, match="pr_contract_passed"):
            evaluate_gates(report, "pr")

    def test_nightly_quality_gate(self):
        # candidate 质量退化超过容差 -> 失败
        cases = load_dataset(DATASET)[:4]
        pairs: list[PairedEvalResult] = []
        for c in cases:
            legacy = _result(backend="legacy")
            legacy.deterministic_scores = self._all_pass_scores()
            cand = _result(backend="candidate")
            cand.deterministic_scores = {
                **self._all_pass_scores(),
                "check_terminal_status": {"pass": False, "value": 0, "reason": ""},
            }
            pairs.append(
                PairedEvalResult(
                    case_id=c.case_id, category=c.category, legacy=legacy, candidate=cand
                )
            )
        report = _report_from_pairs(pairs)
        with pytest.raises(GateViolation, match="nightly"):
            evaluate_gates(report, "nightly")

    def test_release_requires_holdout(self):
        cases = load_dataset(DATASET)
        pairs: list[PairedEvalResult] = []
        for c in cases:
            cand = _result(backend="candidate")
            cand.deterministic_scores = self._all_pass_scores()
            pairs.append(
                PairedEvalResult(
                    case_id=c.case_id, category=c.category, legacy=_result(), candidate=cand
                )
            )
        report = _report_from_pairs(pairs)
        splits = {c.case_id: c.split for c in cases}
        categories = {c.case_id: c.category for c in cases}
        # 未校准 judge 时 release 硬门禁（release_judge_calibration）阻断
        with pytest.raises(GateViolation, match="release_judge_calibration"):
            evaluate_gates(report, "release", case_splits=splits, categories=categories)

    def test_release_holdout_pass_with_calibration(self):
        cases = load_dataset(DATASET)
        pairs: list[PairedEvalResult] = []
        for c in cases:
            cand = _result(backend="candidate")
            cand.deterministic_scores = self._all_pass_scores()
            pairs.append(
                PairedEvalResult(
                    case_id=c.case_id, category=c.category, legacy=_result(), candidate=cand
                )
            )
        report = _report_from_pairs(
            pairs,
            judge_calibration={
                "calibrated": True,
                "agreement_rate": 0.9,
                "compared_dimension_pairs": 10,
            },
        )
        splits = {c.case_id: c.split for c in cases}
        categories = {c.case_id: c.category for c in cases}
        gates = evaluate_gates(report, "release", case_splits=splits, categories=categories)
        assert gates["release_holdout"]["pass"]
        assert gates["release_safety"]["pass"]
        assert gates["release_judge_calibration"]["pass"]

    def test_release_missing_holdout_blocks(self):
        cases = load_dataset(DATASET)[:2]
        pairs = [
            PairedEvalResult(
                case_id=c.case_id,
                category=c.category,
                legacy=_result(),
                candidate=_result(backend="candidate"),
            )
            for c in cases
        ]
        report = _report_from_pairs(pairs)
        with pytest.raises(GateViolation, match="release_holdout"):
            evaluate_gates(report, "release", case_splits={"X": "train"})


# ── LLM-as-Judge ──────────────────────────────────────────────


class TestJudge:
    @pytest.mark.asyncio
    async def test_mock_judge_deterministic(self):
        j = Judge(rubric_version="v1", mock=True)
        s1 = await j.judge_answer("q", "答案")
        s2 = await j.judge_answer("q", "答案")
        assert s1 == s2
        assert s1["total"] <= 25

    @pytest.mark.asyncio
    async def test_judge_pair_no_position_bias_mock(self):
        j = Judge(mock=True)
        res = await j.judge_pair("q", "legacy 回答", "candidate 回答")
        assert res["position_bias"]["order_swapped"] is True
        assert res["legacy"]["total"] == res["candidate"]["total"]  # mock 确定性打分

    def test_flag_disagreements(self):
        judge = {"A": {"total": 20, "max_total": 25}}
        det = {"A": {"pass": False}}
        assert flag_disagreements(judge, det) == ["A"]

    def test_calibration_stats(self):
        judge = {"A": {"accuracy": 4, "relevance": 5, "max_total": 25}}
        human = {"A": {"accuracy": 4, "relevance": 2}}
        cal = calibration_stats(judge, human)
        assert cal["calibrated"] is True
        assert cal["compared_dimension_pairs"] == 2
        assert 0 < cal["agreement_rate"] < 1.0


# ── Mock LLM 模拟器 ───────────────────────────────────────────


class TestMockLLM:
    @pytest.mark.asyncio
    async def test_tool_script_sequence(self):
        llm = MockToolLLM(tool_script=[["search_knowledge"], ["get_article"]])
        msg1 = await llm.ainvoke([])
        assert msg1.tool_calls[0]["name"] == "search_knowledge"
        msg2 = await llm.ainvoke([])
        assert msg2.tool_calls[0]["name"] == "get_article"
        msg3 = await llm.ainvoke([])
        assert not msg3.tool_calls  # 剧本用尽 -> 答案

    @pytest.mark.asyncio
    async def test_fault_persistent_on_decision_round(self):
        llm = MockToolLLM(fault={"llm_error": "rate_limit"})
        with pytest.raises(RuntimeError):
            await llm.ainvoke([])
        with pytest.raises(RuntimeError):
            await llm.ainvoke([])

    @pytest.mark.asyncio
    async def test_finalization_mode_recovers_from_fault(self):
        llm = MockToolLLM(fault={"llm_error": "rate_limit"})
        llm.bind_tools([], tool_choice="none")  # finalization 轮
        msg = await llm.ainvoke([])
        assert not msg.tool_calls

    @pytest.mark.asyncio
    async def test_legacy_llm_usage_metadata(self):
        llm = MockLegacyLLM(final_answer="确定答案")
        msg = await llm.ainvoke([])
        assert msg.content == "确定答案"
        assert msg.usage_metadata["input_tokens"] >= 1


# ── 真实双轨 Runner ───────────────────────────────────────────


class TestRunner:
    def test_fixture_tool_returns_typed_result(self):
        tool = FixtureTool("search_knowledge", "知识内容", source_ids=["kb/ztna"])
        assert tool.ainvoke is not None
        import asyncio

        res = asyncio.run(tool.ainvoke({"product_id": "p_eval"}))
        assert res.ok is True
        assert res.source_ids == ["kb/ztna"]

    @pytest.mark.asyncio
    async def test_run_pair_mock(self):
        cases = load_dataset(DATASET)
        runner = EvalRunner(llm_backend="mock", model_name="deepseek-chat", n_runs=1)
        pair = await runner.run_pair(cases[0])
        assert pair.candidate.backend == "candidate"
        assert pair.candidate.terminal_status in ("completed", "budget_exceeded")

    @pytest.mark.asyncio
    async def test_candidate_collects_evidence(self):
        cases = load_dataset(DATASET)
        by_id = {c.case_id: c for c in cases}
        runner = EvalRunner(llm_backend="mock", model_name="deepseek-chat", n_runs=1)
        pair = await runner.run_pair(by_id["R004"])  # product_knowledge，工具 search_knowledge
        assert "kb/ztna" in pair.candidate.evidence_trace
        assert pair.candidate.tools_used == ["search_knowledge"]

    @pytest.mark.asyncio
    async def test_budget_case_ends_exceeded(self):
        cases = load_dataset(DATASET)
        by_id = {c.case_id: c for c in cases}
        runner = EvalRunner(llm_backend="mock", model_name="deepseek-chat", n_runs=1)
        pair = await runner.run_pair(by_id["R020"])  # llm_error=timeout
        assert pair.candidate.terminal_status == "budget_exceeded"

    @pytest.mark.asyncio
    async def test_no_finalization_reserve_path(self):
        cases = load_dataset(DATASET)
        by_id = {c.case_id: c for c in cases}
        runner = EvalRunner(llm_backend="mock", model_name="deepseek-chat", n_runs=1)
        pair = await runner.run_pair(by_id["R028"])  # no_finalization_reserve + rate_limit
        assert pair.candidate.terminal_status == "budget_exceeded"
