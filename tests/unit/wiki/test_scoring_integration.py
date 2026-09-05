"""PR-07 ScoringAgentV2 × KnowledgeProvider 集成测试。

覆盖（文档 17.1 / 17.4）：
  - wiki 模式：证据充分 → 用 EVIDENCE_SYSTEM_PROMPT，结果附加 _evidence_meta
  - shadow 模式：用户结果不变（旧提示词），后台附加 _shadow_compare
  - INSUFFICIENT_EVIDENCE：证据不足 → 显式 NO_SCORE（不回退 legacy 补分）
  - legacy：显式注入 LegacyKnowledgeProvider，保持旧链路行为，无 wiki 副作用
"""

from __future__ import annotations

from agent.scorer_v2 import SYSTEM_PROMPT_TEMPLATE, ScoringAgentV2
from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceBundle, EvidenceItem


class _Structured:
    """模拟 invoke_structured 返回的 Pydantic 结果。"""

    def __init__(self, relevance: int = 85, event_impact: int = 70) -> None:
        self._data = {"relevance": relevance, "event_impact": event_impact, "reason": "证据充分"}

    def model_dump(self) -> dict:
        return dict(self._data)


class _FakeWrapper:
    """替换 scorer.llm_wrapper，捕获每次调用的 system_prompt。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def invoke_structured(self, **kwargs):
        self.calls.append(kwargs)
        return _Structured()


class _FakeKnowledge:
    """最小 knowledge 桩：只提供 as_scoring_prompt。"""

    def as_scoring_prompt(self) -> str:
        return "产品支持智能体身份认证\n提供 MCP 协议防护\n"


class _FakeLLM:
    def __init__(self) -> None:
        self.temperature = 0.1


class _FakeProvider:
    def __init__(self, mode: str, bundle: EvidenceBundle) -> None:
        self.mode = mode
        self._bundle = bundle

    async def collect_evidence(self, request):
        return self._bundle


def _make_bundle(status: str) -> EvidenceBundle:
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


def _make_scorer(mode: str, bundle: EvidenceBundle):
    from agent.wiki.provider import LegacyKnowledgeProvider

    # Hard Gate (GOAL B): provider=None 不再等价于 legacy。
    # legacy 模式也必须显式注入 LegacyKnowledgeProvider。
    provider = LegacyKnowledgeProvider() if mode == "legacy" else _FakeProvider(mode, bundle)
    scorer = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        db=None,
        knowledge_provider=provider,
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


async def test_wiki_mode_uses_evidence_prompt() -> None:
    scorer = _make_scorer("wiki", _make_bundle("SUFFICIENT"))
    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # 用了证据提示词而非大段历史上下文
    system_prompt = scorer.llm_wrapper.calls[0]["system_prompt"]
    assert "## Verified Evidence" in system_prompt
    assert "支持智能体身份认证" in system_prompt
    assert "1-产品/overview.md" in system_prompt
    assert "## 待评产品" in system_prompt
    assert result["relevance"] == 85

    # 无 _shadow_compare；wiki 模式附加 _evidence_meta
    assert "_shadow_compare" not in result
    assert result["knowledge_backend"] == "wiki"
    assert result["evidence_ids"] == ["e1"]
    assert result["grounded"] is True
    assert result["wiki_version"] == "v1"
    # invoke 的 context_meta 带证据元信息
    assert scorer.llm_wrapper.calls[0]["context_meta"]["knowledge_backend"] == "wiki"


async def test_shadow_mode_keeps_legacy_result_and_compares() -> None:
    scorer = _make_scorer("shadow", _make_bundle("SUFFICIENT"))
    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # 主评分走旧提示词（用户结果不变），后台再跑一次证据提示词
    assert len(scorer.llm_wrapper.calls) == 2
    assert (
        SYSTEM_PROMPT_TEMPLATE in scorer.llm_wrapper.calls[0]["system_prompt"]
        or "## 产品知识库" in scorer.llm_wrapper.calls[0]["system_prompt"]
    )
    assert "## Verified Evidence" in scorer.llm_wrapper.calls[1]["system_prompt"]

    # 用户结果本身仍是 legacy 形态（relevance 直接来自 LLM）
    assert result["relevance"] == 85
    assert "_shadow_compare" in result
    cmp = result["_shadow_compare"]
    assert cmp["legacy_score"] == 85
    assert cmp["wiki_score"] == 85
    assert cmp["wiki_status"] == "SUFFICIENT"
    assert cmp["evidence_count"] == 1
    assert cmp["wiki_pages_read"] == 1
    # shadow 不覆盖 _evidence_meta 到主结果
    assert result.get("knowledge_backend") != "wiki"


async def test_insufficient_evidence_no_score() -> None:
    """严格 Wiki：证据不充分 → NO_SCORE，不调用 LLM、不回退 legacy 补分（§15.1）。"""
    scorer = _make_scorer("wiki", _make_bundle("INSUFFICIENT_EVIDENCE"))
    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # 不触发任何 LLM 评分调用（避免模型靠常识补分）
    assert scorer.llm_wrapper.calls == []
    assert result["_no_score"] is True
    assert result["status"] == "INSUFFICIENT_PRODUCT_EVIDENCE"
    assert result["relevance"] is None
    assert "缺少经过验证的产品能力证据" in result["reason"]
    # 仍附证据元信息（便于追踪为何不评分）
    assert result["knowledge_backend"] == "wiki"
    assert "## 产品知识库" not in result
    assert "_shadow_compare" not in result


async def test_concurrent_all_no_score_surfaces_insufficient() -> None:
    """严格 Wiki：所有产品证据不足时聚合结果显式 INSUFFICIENT_PRODUCT_EVIDENCE。"""
    scorer = _make_scorer("wiki", _make_bundle("INSUFFICIENT_EVIDENCE"))

    async def fake_score(article, **kwargs):
        return scorer._no_score_result(
            kwargs["product_id"],
            kwargs["product_name"],
            bundle=_make_bundle("INSUFFICIENT_EVIDENCE"),
        )

    scorer._score_single_product = fake_score
    result = await scorer._score_concurrent(
        _ARTICLE,
        products=[{"product_id": "agent_identity", "product_name": "身份"}],
    )
    assert result["_no_score"] is True
    assert result["status"] == "INSUFFICIENT_PRODUCT_EVIDENCE"
    assert result["product_scores"] == []
    assert result["product_relevance"] == 0


async def test_legacy_provider_keeps_old_path() -> None:
    scorer = _make_scorer("legacy", _make_bundle("SUFFICIENT"))
    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # provider 为 None → 旧链路，无证据收集、无_evidence_meta、无_shadow_compare
    assert len(scorer.llm_wrapper.calls) == 1
    system_prompt = scorer.llm_wrapper.calls[0]["system_prompt"]
    assert "## 产品知识库" in system_prompt
    assert result["relevance"] == 85
    assert "knowledge_backend" not in result
    assert "_shadow_compare" not in result
