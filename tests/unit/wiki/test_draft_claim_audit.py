"""Phase 13 Draft Claim Audit（§16.1）单元测试：草稿产品事实必须 Grounded。"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef
from agent.wiki.draft_claim_audit import DraftClaimAudit, _short_hash
from agent.wiki.evidence import EvidenceBundle, EvidenceItem


def _bundle(verified_facts: list[tuple[str, str]]) -> EvidenceBundle:
    """构造证据 bundle：[(claim_id, fact), ...]，全部 confidence=0.9（已 Grounded）。"""
    items = [
        EvidenceItem(
            evidence_id=f"ev{i}",
            claim_id=cid,
            fact=fact,
            page_id="product.a.capability.x",
            page_title="能力",
            source_refs=[
                SourceRef(source_id="s1", relative_path="1-产品/overview.md", content_hash="h")
            ],
            relevance=0.9,
            confidence=0.9,
            relation_to_task="potential_match",
        )
        for i, (cid, fact) in enumerate(verified_facts)
    ]
    return EvidenceBundle(
        status="SUFFICIENT",
        query="身份认证",
        product_ids=["a"],
        evidence=items,
        coverage=0.8,
        confidence=0.9,
    )


def test_extract_claims_splits_sentences():
    audit = DraftClaimAudit(_bundle([]))
    claims = audit.extract_claims("支持智能体身份认证。可防止账号冒用。\n后续另行说明")
    texts = [c[1] for c in claims]
    assert any("身份" in t and "认证" in t for t in texts)
    assert any("账号冒用" in t for t in texts)


def test_supported_claim_aligned_to_evidence():
    b = _bundle([("claim_cap", "提供智能体身份认证能力")])
    audit = DraftClaimAudit(b)
    # 草稿声明与证据事实共享 bigram → supported 且对齐到该证据
    result = audit.audit("该产品支持智能体身份认证能力。")
    assert result["total"] >= 1
    for c in result["claims"]:
        if "身份认证能力" in c.claim:
            assert c.supported is True
            assert c.evidence_ids and "claim_cap" in c.evidence_ids


def test_unsupported_claim_not_grounded():
    b = _bundle([("claim_cap", "提供智能体身份认证能力")])
    audit = DraftClaimAudit(b)
    # 产品声称与证据完全无关 → 不应被 Grounded
    result = audit.audit("具备量子加密后量子抗性，可抵御量子攻击。")
    assert result["unsupported"] >= 1
    for c in result["claims"]:
        assert c.supported is False


def test_grounded_ratio_and_claim_id_stable():
    b = _bundle([("claim_cap", "提供智能体身份认证能力")])
    audit = DraftClaimAudit(b)
    result = audit.audit("产品支持身份认证。同时宣称无关量子能力。")
    assert 0 < result["grounded_ratio"] < 1
    # claim_id 稳定：同内容 hash 一致
    a = audit.extract_claims("支持身份认证。")
    b2 = audit.extract_claims("支持身份认证。")
    assert a[0][0] == b2[0][0]
    assert a[0][0].startswith("claim_")
    assert a[0][0] == _short_hash("|" + a[0][1])


def test_empty_input_returns_empty():
    audit = DraftClaimAudit(_bundle([]))
    assert audit.audit("")["total"] == 0
    assert audit.extract_claims("  ") == []
