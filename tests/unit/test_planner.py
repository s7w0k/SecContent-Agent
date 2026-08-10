"""Planner 单元测试 — 阶段三 Step 4。"""

from __future__ import annotations

import asyncio

import pytest

from agent.plan_contracts import PlanValidator, build_default_plan, input_snapshot_hash
from agent.planner import (
    PLANNER_VERSION,
    Planner,
    PlannerArticleInput,
    PlannerChoice,
    PlannerInput,
    PlannerOutcome,
    build_plan_from_choice,
)


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _articles() -> list[PlannerArticleInput]:
    return [
        PlannerArticleInput(id="art-1", title="漏洞通告", summary="某产品远程代码执行", status="crawled"),
        PlannerArticleInput(id="art-2", title="竞品动态", summary="友商发布新版本", status="crawled"),
    ]


def _products() -> list[dict]:
    return [{"id": "agent-identity-security", "name": "智能体身份安全"}, {"id": "pr-agent", "name": "PR 情报"}]


def _choice(**overrides) -> PlannerChoice:
    kwargs = {
        "needs_fulltext": True,
        "breaking_article_ids": ["art-1"],
        "article_ids": ["art-1", "art-2"],
        "product_ids": ["agent-identity-security"],
        "score_threshold": 90,
        "style_hints": ["强调影响面"],
        "rationale_summary": "依据 art-1 漏洞影响面选择重点；补全文提升证据质量。",
    }
    kwargs.update(overrides)
    return PlannerChoice(**kwargs)


class _FakeWrapper:
    """可编程 LLM 封装，直接返回/抛错/挂起。"""

    def __init__(self, choice: PlannerChoice | None = None, error: Exception | None = None, hang: bool = False):
        self.choice = choice
        self.error = error
        self.hang = hang
        self.calls: list[dict] = []

    async def invoke_structured(self, **kwargs) -> PlannerChoice:
        self.calls.append(kwargs)
        if self.hang:
            await asyncio.sleep(60)
        if self.error is not None:
            raise self.error
        return self.choice


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc: dict):
        self.docs.append(doc)


class _FakeDB:
    def __init__(self):
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str) -> _FakeCol:
        return self._cols.setdefault(name, _FakeCol())


def _planner(
    *,
    db=None,
    wrapper=None,
    enabled: bool = True,
    model: str = "deepseek-planner",
    timeout: int = 10,
) -> Planner:
    return Planner(
        llm_wrapper=wrapper,
        db=db,
        enabled=enabled,
        planner_model=model,
        timeout_seconds=timeout,
        validator=PlanValidator(),
    )


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════
# PlannerChoice Schema
# ═══════════════════════════════════════════════════════════════


class TestPlannerChoice:
    def test_bounds(self):
        with pytest.raises(ValueError):
            PlannerChoice(breaking_article_ids=["a"] * 6)
        with pytest.raises(ValueError):
            PlannerChoice(score_threshold=201)

    def test_rationale_truncated(self):
        choice = PlannerChoice(rationale_summary="x" * 600)
        assert len(choice.rationale_summary) == 500


# ═══════════════════════════════════════════════════════════════
# 服务端转换 build_plan_from_choice
# ═══════════════════════════════════════════════════════════════


class TestBuildPlanFromChoice:
    def _plan(self, choice: PlannerChoice):
        return build_plan_from_choice(
            choice,
            run_id="run-1",
            input_snapshot_hash_value="sha256:" + "0" * 64,
            user_id="u-1",
            trace_id="t-1",
        )

    def test_fixed_skeleton(self):
        plan = self._plan(_choice())
        assert [s.worker for s in plan.steps] == [
            "crawl", "enrich", "classify", "filter", "score",
            "draft", "quality_check", "rewrite", "review",
        ]

    def test_model_cannot_inject_workers_or_args(self):
        plan = self._plan(_choice())
        for step in plan.steps:
            assert step.worker in {
                "crawl", "enrich", "classify", "filter", "score",
                "draft", "quality_check", "rewrite", "review",
            }
            assert "publish" not in step.input_refs
            assert "delete" not in step.input_refs

    def test_draft_guard_present(self):
        plan = self._plan(_choice())
        workers = {s.worker for s in plan.steps}
        assert {"quality_check", "review"} <= workers

    def test_choice_inputs_flow_into_refs(self):
        plan = self._plan(_choice(breaking_article_ids=["art-1"], style_hints=["强调影响面"], score_threshold=90))
        draft = next(s for s in plan.steps if s.worker == "draft")
        assert draft.input_refs["breaking_article_ids"] == ["art-1"]
        assert draft.input_refs["style_hints"] == ["强调影响面"]
        score = next(s for s in plan.steps if s.worker == "score")
        assert score.input_refs["score_threshold"] == 90

    def test_no_fulltext_skips_enrich(self):
        plan = self._plan(_choice(needs_fulltext=False))
        assert [s.worker for s in plan.steps] == [
            "crawl", "classify", "filter", "score",
            "draft", "quality_check", "rewrite", "review",
        ]

    def test_plan_hash_stable_across_plan_ids(self):
        a = build_plan_from_choice(
            _choice(), run_id="run-1", input_snapshot_hash_value="h1", user_id="u-1"
        )
        b = build_plan_from_choice(
            _choice(), run_id="run-1", input_snapshot_hash_value="h1", user_id="u-1"
        )
        assert a.plan_id != b.plan_id
        assert a.plan_hash == b.plan_hash


# ═══════════════════════════════════════════════════════════════
# Planner.plan
# ═══════════════════════════════════════════════════════════════


class TestPlannerPlan:
    def _base_kwargs(self):
        return dict(
            run_id="run-1",
            user_id="u-1",
            trace_id="t-1",
            products=_products(),
            articles=_articles(),
        )

    def test_disabled_falls_back(self):
        planner = _planner(enabled=False, wrapper=_FakeWrapper(_choice()))
        outcome = _run(planner.plan(**self._base_kwargs()))
        assert outcome.source == "fallback"
        assert outcome.reason == "planner disabled"
        assert outcome.plan_hash == outcome.plan.plan_hash

    def test_enabled_requires_model_and_wrapper(self):
        planner = _planner(enabled=True, model="", wrapper=None)
        assert planner.enabled is False

    def test_planner_choice_accepted(self):
        wrapper = _FakeWrapper(_choice())
        planner = _planner(db=_FakeDB(), wrapper=wrapper)
        outcome = _run(planner.plan(**self._base_kwargs()))
        assert outcome.source == "planner"
        assert outcome.rejected is False
        assert outcome.plan_hash == outcome.plan.plan_hash
        assert outcome.persisted is True
        assert len(wrapper.calls) == 1
        assert wrapper.calls[0]["agent_type"] == "planner"

    def test_snapshot_authoritative(self):
        planner = _planner(wrapper=_FakeWrapper(_choice()))
        outcome = _run(planner.plan(**self._base_kwargs()))
        expected = input_snapshot_hash(
            user_id="u-1", product_ids=["agent-identity-security", "pr-agent"], article_ids=["art-1", "art-2"]
        )
        assert outcome.input_snapshot_hash == expected
        assert outcome.plan.input_snapshot_hash == expected

    def test_llm_error_falls_back(self):
        wrapper = _FakeWrapper(error=RuntimeError("provider down"))
        planner = _planner(db=_FakeDB(), wrapper=wrapper)
        outcome = _run(planner.plan(**self._base_kwargs()))
        assert outcome.source == "fallback"
        assert "planner error" in outcome.reason

    def test_timeout_falls_back(self):
        wrapper = _FakeWrapper(choice=_choice(), hang=True)
        planner = _planner(db=_FakeDB(), wrapper=wrapper, timeout=1)
        outcome = _run(planner.plan(**self._base_kwargs()))
        assert outcome.source == "fallback"
        assert "timeout" in outcome.reason

    def test_disallowed_product_rejected_then_fallback(self):
        wrapper = _FakeWrapper(_choice(product_ids=["hacker-product"]))
        planner = _planner(db=_FakeDB(), wrapper=wrapper)
        outcome = _run(planner.plan(**self._base_kwargs()))
        assert outcome.source == "fallback"
        assert outcome.rejected is True
        assert "product not allowed" in outcome.reason
        assert outcome.plan.rationale_summary == build_default_plan(
            run_id="run-1",
            input_snapshot_hash_value=outcome.input_snapshot_hash,
            user_id="u-1",
        ).rationale_summary

    def test_run_id_authoritative(self):
        wrapper = _FakeWrapper(_choice())
        planner = _planner(db=_FakeDB(), wrapper=wrapper)
        outcome = _run(planner.plan(run_id="run-9", user_id="u-1", products=_products(), articles=_articles()))
        assert outcome.plan.run_id == "run-9"


# ═══════════════════════════════════════════════════════════════
# 持久化
# ═══════════════════════════════════════════════════════════════


class TestPlannerPersist:
    def test_accepted_persisted(self):
        db = _FakeDB()
        planner = _planner(db=db, wrapper=_FakeWrapper(_choice()))
        outcome = _run(planner.plan(run_id="run-1", user_id="u-1", products=_products(), articles=_articles()))
        docs = db["planner_plans"].docs
        assert len(docs) == 1
        doc = docs[0]
        assert doc["status"] == "accepted"
        assert doc["plan_hash"] == outcome.plan_hash
        assert doc["planner_version"] == PLANNER_VERSION
        assert doc["input_snapshot_hash"] == outcome.input_snapshot_hash
        assert doc["rationale_summary"] == outcome.rationale_summary

    def test_fallback_persisted(self):
        db = _FakeDB()
        planner = _planner(db=db, enabled=False)
        outcome = _run(planner.plan(run_id="run-1", user_id="u-1", products=_products(), articles=_articles()))
        docs = db["planner_plans"].docs
        assert len(docs) == 1
        assert docs[0]["status"] == "fallback"
        assert docs[0]["plan_hash"] == outcome.plan_hash

    def test_rejected_persisted_then_fallback(self):
        db = _FakeDB()
        planner = _planner(db=db, wrapper=_FakeWrapper(_choice(product_ids=["bad"])))
        outcome = _run(planner.plan(run_id="run-1", user_id="u-1", products=_products(), articles=_articles()))
        docs = db["planner_plans"].docs
        assert [d["status"] for d in docs] == ["rejected", "fallback"]
        assert outcome.rejected is True
