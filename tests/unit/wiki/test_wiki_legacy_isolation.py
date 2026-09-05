"""PR-2 §9 严格 Wiki × Legacy 隔离测试。

验证 Wiki 评分链路完全独立于 Legacy 子系统：
  - 即使 `_build_system_prompt_for_product`（Legacy Prompt Builder）抛异常，
    wiki 模式的 SUFFICIENT 证据仍能正常评分，绝不受影响（文档 15.x 隔离目标）。
  - wiki 模式实例的 `.mode` 唯一来自注入的 provider。

说明：`_score_with_wiki` 从不调用 `_build_system_prompt_for_product`，
因此本测试通过把它 mock 成抛错来证明「Wiki 不依赖 Legacy」这一不变量。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from agent.scorer_v2 import ScoringAgentV2
from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceBundle, EvidenceItem


class _Structured:
    def __init__(self, relevance: int = 88, event_impact: int = 72) -> None:
        self._data = {"relevance": relevance, "event_impact": event_impact, "reason": "隔离正常"}

    def model_dump(self) -> dict:
        return dict(self._data)


class _FakeWrapper:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke_structured(self, **kwargs):
        self.calls.append(kwargs)
        return _Structured()


class _FakeLLM:
    def __init__(self) -> None:
        self.temperature = 0.1


class _FakeKnowledge:
    def as_scoring_prompt(self) -> str:
        return "通用安全知识上下文\n"


class _FakeProvider:
    def __init__(self, bundle: EvidenceBundle) -> None:
        self.mode = "wiki"
        self._bundle = bundle

    async def collect_evidence(self, request):
        return self._bundle


def _make_bundle(status: str = "SUFFICIENT") -> EvidenceBundle:
    return EvidenceBundle(
        status=status,
        task_type="score",
        query="身份认证事件",
        product_ids=["agent_identity"],
        evidence=[
            EvidenceItem(
                evidence_id="e1",
                fact="支持智能体身份认证",
                page_id="capability.identity_auth",
                page_title="身份认证",
                source_refs=[
                    SourceRef(source_id="s1", relative_path="1-产品/overview.md", content_hash="h1")
                ],
                relevance=0.9,
                confidence=0.9,
                relation_to_task="potential_match",
            )
        ],
        coverage=0.8,
        confidence=0.9,
        visited_pages=["capability.identity_auth"],
        wiki_version="v1",
    )


def _make_wiki_scorer() -> ScoringAgentV2:
    scorer = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        db=None,
        knowledge_provider=_FakeProvider(_make_bundle()),
    )
    scorer.llm_wrapper = _FakeWrapper()
    return scorer


_ARTICLE = {
    "title": "某平台身份认证漏洞",
    "source": "ext",
    "category_v2": "身份安全",
    "summary": "攻击者可绕过身份认证。",
    "content_md": "事件详情：可绕过产品身份认证机制。",
}


async def test_wiki_scoring_survives_legacy_subsystem_failure() -> None:
    """Legacy Prompt Builder 抛异常，wiki SUFFICIENT 仍正常评分。"""
    scorer = _make_wiki_scorer()

    failing_legacy = AsyncMock()
    failing_legacy.side_effect = RuntimeError("legacy subsystem down")
    scorer._build_system_prompt_for_product = failing_legacy

    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # Wiki 未受 Legacy 故障波及：正常评分 + 单次 Judge
    assert len(scorer.llm_wrapper.calls) == 1
    assert result["relevance"] == 88
    # 成功评分不携带 _no_score，也不以证据不足态返回
    assert result.get("_no_score") is not True
    assert result["knowledge_backend"] == "wiki"
    # Legacy 方法确实被 mock 抛错，而 wiki 评分仍成功 → 证明完全隔离
    failing_legacy.assert_not_awaited()


async def test_wiki_mode_from_injected_provider() -> None:
    """wiki 模式的 mode 唯一由注入的 provider 决定（Provider Contract §6）。"""
    scorer = _make_wiki_scorer()
    assert scorer._provider_mode() == "wiki"


async def test_wiki_score_still_attaches_evidence_meta() -> None:
    """Legacy 故障下 wiki 结果仍携带证据元信息（不影响正确性）。"""
    scorer = _make_wiki_scorer()
    failing_legacy = AsyncMock(side_effect=RuntimeError("down"))
    scorer._build_system_prompt_for_product = failing_legacy

    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )
    assert result["knowledge_backend"] == "wiki"
    assert result["evidence_ids"] == ["e1"]
    assert result["grounded"] is True
    assert result["wiki_version"] == "v1"
