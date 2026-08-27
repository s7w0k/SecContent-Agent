"""PR-2 §9 严格 Wiki 评分隔离测试。

验证 `_score_with_wiki`（wiki 模式唯一路径）：
  - Case1 SUFFICIENT：只构建 Evidence Prompt（Evidence Prompt Builder=1），
    绝不调用 Legacy Prompt Builder（Legacy Prompt Builder=0），Judge=1。
  - Case2 INSUFFICIENT_EVIDENCE：Legacy=0、Judge=0，返回 INSUFFICIENT_PRODUCT_EVIDENCE。
  - Case3 CONFLICTED：Legacy=0、Judge=0，返回 NO_SCORE（INSUFFICIENT_PRODUCT_EVIDENCE）。
  - Case4 FAILED：Legacy=0、Judge=0，返回 NO_SCORE（INSUFFICIENT_PRODUCT_EVIDENCE）。

核心不变量（文档 15.1）：wiki 模式绝不回退到 legacy 补分。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from agent.scorer_v2 import SYSTEM_PROMPT_TEMPLATE, ScoringAgentV2
from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceBundle, EvidenceItem


class _Structured:
    def __init__(self, relevance: int = 85, event_impact: int = 70) -> None:
        self._data = {"relevance": relevance, "event_impact": event_impact, "reason": "证据充分"}

    def model_dump(self) -> dict:
        return dict(self._data)


class _CountingWrapper:
    """替换 scorer.llm_wrapper，统计 invoke_structured 的次数并记录 system_prompt。"""

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
                    SourceRef(
                        source_id="s1", relative_path="1-产品/overview.md", content_hash="h1"
                    )
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


def _make_strict_scorer(bundle: EvidenceBundle) -> ScoringAgentV2:
    scorer = ScoringAgentV2(
        llm=_FakeLLM(),
        knowledge=_FakeKnowledge(),
        db=None,
        knowledge_provider=_FakeProvider(bundle),
    )
    scorer.llm_wrapper = _CountingWrapper()
    return scorer


_ARTICLE = {
    "title": "某平台身份认证漏洞",
    "source": "ext",
    "category_v2": "身份安全",
    "summary": "攻击者可绕过身份认证。",
    "content_md": "事件详情：可绕过产品身份认证机制。",
}


async def test_case1_sufficient_uses_evidence_prompt_and_zero_legacy() -> None:
    """Case1 SUFFICIENT：只构建 Evidence Prompt，绝不调用 Legacy Prompt Builder。"""
    scorer = _make_strict_scorer(_make_bundle("SUFFICIENT"))
    stub_legacy = AsyncMock()
    stub_legacy.return_value = (SYSTEM_PROMPT_TEMPLATE.format(
        knowledge_context="LEGACY", product_list="- product_id: x"
    ), {})
    scorer._build_system_prompt_for_product = stub_legacy

    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # ① 只发生 1 次 Judge 调用
    assert len(scorer.llm_wrapper.calls) == 1
    # ② 用的是 Evidence Prompt Builder（而不是直接渲染 SYSTEM_PROMPT_TEMPLATE 一处即可佐证）
    system_prompt = scorer.llm_wrapper.calls[0]["system_prompt"]
    assert "## Verified Evidence" in system_prompt
    assert "支持智能体身份认证" in system_prompt
    # ③ Legacy Prompt Builder 绝未被调用
    stub_legacy.assert_not_awaited()
    # ④ 结果正常且附证据元信息
    assert result["relevance"] == 85
    assert result["knowledge_backend"] == "wiki"


async def _assert_no_score_and_no_judge(status: str) -> None:
    scorer = _make_strict_scorer(_make_bundle(status))
    stub_legacy = AsyncMock(return_value=("LEGACY", {}))
    scorer._build_system_prompt_for_product = stub_legacy

    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )

    # 不触发 Judge（不靠常识补分）
    assert scorer.llm_wrapper.calls == []
    # 不触发 Legacy Prompt Builder
    stub_legacy.assert_not_awaited()
    # NO_SCORE + 显式状态
    assert result["_no_score"] is True
    assert result["status"] == "INSUFFICIENT_PRODUCT_EVIDENCE"
    assert result["reason"] == "缺少经过验证的产品能力证据"
    # 仍附证据元信息便于追踪
    assert result["knowledge_backend"] == "wiki"


async def test_case2_insufficient_evidence_no_judge_no_legacy() -> None:
    await _assert_no_score_and_no_judge("INSUFFICIENT_EVIDENCE")


async def test_case3_conflicted_no_judge_no_legacy() -> None:
    """CONFLICTED：证据冲突视为不足以评分，绝不回退 legacy。"""
    await _assert_no_score_and_no_judge("CONFLICTED")


async def test_case4_failed_no_judge_no_legacy() -> None:
    await _assert_no_score_and_no_judge("FAILED")


async def test_wiki_path_never_renders_legacy_template() -> None:
    """佐证：wiki 模式下喂给 Judge 的提示词必须来自 EVIDENCE 模板。"""
    scorer = _make_strict_scorer(_make_bundle("SUFFICIENT"))
    result = await scorer._score_single_product(
        _ARTICLE, product_id="agent_identity", product_name="身份"
    )
    assert result["relevance"] == 85
    system_prompt = scorer.llm_wrapper.calls[0]["system_prompt"]
    # 禁止 legacy 的关键特征：Product Knowledge 上下文块
    assert "## 产品知识库" not in system_prompt
    # 必须含证据模板的编排节
    assert "## Verified Evidence" in system_prompt
    assert "## Evidence Coverage" in system_prompt
    assert "## Unknown / Missing" in system_prompt
