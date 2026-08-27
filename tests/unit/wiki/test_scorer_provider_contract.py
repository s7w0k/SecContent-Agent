"""PR-2 / Provider Contract（计划 §15）：ScoringAgentV2 必须显式注入 KnowledgeProvider。

覆盖：
  - 未传 knowledge_provider → 构造报错（TypeError，required kwarg）
  - knowledge_provider=None → 构造报错（ValueError，None 不再等价 Legacy）
  - _provider_mode 只来自 provider.mode，不按 None/环境变量推断
  - 显式 LegacyKnowledgeProvider / Shadow provider 被支持
"""

from __future__ import annotations

import pytest
from agent.scorer_v2 import ScoringAgentV2
from agent.wiki.provider import LegacyKnowledgeProvider


class _FakeLLM:
    def __init__(self) -> None:
        self.temperature = 0.1


class _FakeKnowledge:
    def as_scoring_prompt(self) -> str:
        return "产品支持智能体身份认证\nMCP 协议防护\n"


class _StubProvider:
    def __init__(self, mode: str) -> None:
        self.mode = mode


def test_scorer_requires_explicit_provider():
    """未传 knowledge_provider（required kwarg）→ TypeError。"""
    with pytest.raises(TypeError):
        ScoringAgentV2(llm=_FakeLLM(), knowledge=_FakeKnowledge())


def test_scorer_rejects_none_provider():
    """knowledge_provider=None → ValueError，None 不再代表 Legacy。"""
    with pytest.raises(ValueError) as exc_info:
        ScoringAgentV2(
            llm=_FakeLLM(),
            knowledge=_FakeKnowledge(),
            knowledge_provider=None,
        )
    assert "explicit KnowledgeProvider" in str(exc_info.value)


def test_provider_mode_comes_from_provider():
    """_provider_mode 只读取 provider.mode。"""
    wiki = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        knowledge_provider=_StubProvider("wiki"),
    )
    assert wiki._provider_mode() == "wiki"

    shadow = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        knowledge_provider=_StubProvider("shadow"),
    )
    assert shadow._provider_mode() == "shadow"


def test_explicit_legacy_provider_is_supported():
    """显式 LegacyKnowledgeProvider 可用。"""
    scorer = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        knowledge_provider=LegacyKnowledgeProvider(),
    )
    assert scorer._provider_mode() == "legacy"


def test_explicit_shadow_provider_is_supported():
    """显式 ShadowProvider 可用。"""
    scorer = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        knowledge_provider=_StubProvider("shadow"),
    )
    assert scorer._provider_mode() == "shadow"


def test_none_never_means_legacy():
    """None 绝不作为 Legacy 的隐式语义存在。"""
    with pytest.raises(ValueError):
        ScoringAgentV2(
            llm=_FakeLLM(),
            knowledge=_FakeKnowledge(),
            knowledge_provider=None,
        )
