"""Final Plan PR-A：LLM Navigation Decider 单元测试（§4.9）。"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from agent.wiki.navigation_decider import (
    LLMNavigationDecider,
    NavigationAction,
    NavigationDecisionContext,
    NavigationDecisionError,
    PageDescriptor,
    deterministic_decision,
)


class _Model:
    def __init__(self, payload):
        self._payload = payload
        self.ainvoked = 0

    async def ainvoke(self, prompt):
        self.ainvoked += 1
        result = self._payload()
        if inspect.isawaitable(result):
            return await result
        return result


class _Wrapper:
    def __init__(self, payload_factory):
        self._factory = payload_factory

    def with_structured_output(self, schema):
        return _Model(self._factory)


def _ctx(candidates=None, missing=None, visited=None):
    return NavigationDecisionContext(
        query="身份认证事件",
        task_type="score",
        missing_requirements=missing or ["R2"],
        visited_pages=visited or [],
        candidate_pages=candidates
        or [
            {"page_id": "product.agent.capability.a", "page_type": "capability", "task_affinity": ["R1"]},
            {"page_id": "product.agent.scenario.b", "page_type": "scenario", "task_affinity": ["R2"]},
        ],
        pages_remaining=4,
        tool_calls_remaining=8,
        tokens_remaining=8000,
    )


async def test_decider_accepts_candidate_target():
    decider = LLMNavigationDecider(
        _Wrapper(lambda: NavigationAction(action="OPEN_PAGE", target="product.agent.scenario.b")),
        timeout=5,
    )
    action = await decider.decide(_ctx())
    assert action.action == "OPEN_PAGE"
    assert action.target == "product.agent.scenario.b"


async def test_decider_stop_requires_no_target():
    decider = LLMNavigationDecider(
        _Wrapper(lambda: NavigationAction(action="STOP_INSUFFICIENT")), timeout=5
    )
    action = await decider.decide(_ctx())
    assert action.action == "STOP_INSUFFICIENT"


async def test_decider_rejects_invented_page_id():
    """LLM 不能发明 page_id（§4.2/§4.9）。"""
    decider = LLMNavigationDecider(
        _Wrapper(lambda: NavigationAction(action="OPEN_PAGE", target="product.fake.unknown")),
        timeout=5,
    )
    with pytest.raises(NavigationDecisionError):
        await decider.decide(_ctx())


async def test_decider_rejects_unknown_action():
    decider = LLMNavigationDecider(
        _Wrapper(lambda: NavigationAction(action="DELETE_EVERYTHING")), timeout=5
    )
    with pytest.raises(NavigationDecisionError):
        await decider.decide(_ctx())


async def test_decider_timeout_falls_back():
    """LLM timeout → NavigationDecisionError（调用方回退 deterministic，§4.5）。"""

    async def slow():
        await asyncio.sleep(0.1)
        return NavigationAction(action="STOP_INSUFFICIENT")

    decider = LLMNavigationDecider(_Wrapper(slow), timeout=0.005)
    with pytest.raises(NavigationDecisionError):
        await decider.decide(_ctx())


async def test_decider_llm_exception_falls_back():
    async def boom():
        raise RuntimeError("llm down")

    decider = LLMNavigationDecider(_Wrapper(boom), timeout=5)
    with pytest.raises(NavigationDecisionError):
        await decider.decide(_ctx())


def test_deterministic_prefers_missing_requirement():
    candidates = [
        {"page_id": "cap.a", "task_affinity": ["R1"]},
        {"page_id": "scenario.b", "task_affinity": ["R2"]},
    ]
    action = deterministic_decision(candidates, missing_requirements=["R2"])
    assert action.target == "scenario.b"
    assert action.requirement_id == "R2"


def test_deterministic_no_candidates_stops_insufficient():
    action = deterministic_decision([], missing_requirements=["R1"])
    assert action.action == "STOP_INSUFFICIENT"


def test_deterministic_falls_back_to_first():
    action = deterministic_decision([{"page_id": "a"}, {"page_id": "b"}], missing_requirements=[])
    assert action.target == "a"


def test_page_descriptor_bounded_fields():
    d = PageDescriptor(page_id="x", summary="s" * 500, task_affinity=["R1"])
    assert d.page_id == "x"
    assert len(d.task_affinity) == 1
