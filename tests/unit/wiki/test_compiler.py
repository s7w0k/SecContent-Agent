"""PR-03 Page Compiler 单元测试。"""

from __future__ import annotations

from agent.wiki.compiler import PageCompiler, SourceSection, build_wiki_page
from agent.wiki.contracts import SourceRef, WikiPageDraft
from agent.wiki.source_registry import SourceRegistry


def _section(source_id: str, text: str, heading: str = "能力") -> SourceSection:
    return SourceSection(
        ref=SourceRef(source_id=source_id, relative_path="x.md", content_hash="h", heading=heading),
        text=text,
    )


def test_rule_based_compile_keeps_provenance():
    draft = PageCompiler().compile(
        page_id="product.agent_identity.capability.identity_auth",
        page_type="capability",
        title="智能体身份认证",
        product_id="agent_identity",
        source_sections=[_section("src_1", "- 支持智能体身份认证\n- 提供 MCP 协议防护\n", "能力")],
    )
    assert draft.page_type == "capability"
    claims = draft.claim_objects()
    assert len(claims) >= 1
    assert claims[0].source_id == "src_1"
    assert "MCP 协议防护" in claims[0].fact


def test_compile_empty_sources():
    draft = PageCompiler().compile(
        page_id="concept.identity",
        page_type="concept",
        title="身份",
        product_id=None,
        source_sections=[],
    )
    assert draft.summary == ""
    assert draft.claims == []


def test_build_wiki_page_maps_grounded_claims(source_root, wiki_root):
    from helpers import make_source_file

    make_source_file(source_root, "1-产品/overview.md", "# 产品\n- 支持智能体身份认证\n")
    registry = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    registry.sync()
    sid = registry.get_by_path("1-产品/overview.md").source_id

    draft = WikiPageDraft(
        page_id="product.agent_identity.capability.identity_auth",
        title="智能体身份认证",
        page_type="capability",
        product_id="agent_identity",
        summary="身份认证",
        claims=[
            {
                "fact": "支持智能体身份认证",
                "source_id": sid,
                "section_id": "",
                "heading": "",
            }
        ],
        relations=[{"type": "belongs_to", "target_page_id": "product.agent_identity"}],
    )
    page = build_wiki_page(draft, registry)
    assert page.meta.source_refs[0].source_id == sid
    sec = [s for s in page.sections if "Evidence" in s.title]
    assert sec and "来源" in sec[0].body


def test_build_wiki_page_ungrounded_claim_not_in_refs():
    draft = WikiPageDraft(
        page_id="concept.madeup",
        title="臆造",
        page_type="concept",
        product_id=None,
        summary="",
        claims=[{"fact": "凭空声称", "source_id": "nonexistent", "section_id": "", "heading": ""}],
        relations=[],
    )
    page = build_wiki_page(draft, registry=None)
    assert page.meta.source_refs == []
