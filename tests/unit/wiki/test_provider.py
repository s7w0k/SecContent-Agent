"""PR-06 KnowledgeProvider 单元测试。"""

from __future__ import annotations

import pytest
from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceBundle, EvidenceItem
from agent.wiki.index import WikiIndex, build_manifest
from agent.wiki.provider import (
    KnowledgeRequest,
    LegacyKnowledgeProvider,
    ShadowKnowledgeProvider,
    WikiKnowledgeProvider,
    build_knowledge_provider,
)
from agent.wiki.store import WikiStore
from helpers import make_source_file


def _build_wiki(store: WikiStore, registry):
    """构造一个带真实 source_ref（哈希匹配）的 Wiki。"""
    from agent.wiki.compiler import PageCompiler, SourceSection, build_wiki_page

    entry = registry.get_by_path("1-产品/overview.md")
    assert entry is not None
    draft = PageCompiler().compile(
        page_id="product.agent_identity.capability.identity_auth",
        page_type="capability",
        title="身份认证",
        product_id="agent_identity",
        source_sections=[
            SourceSection(
                ref=SourceRef(
                    source_id=entry.source_id,
                    relative_path=entry.relative_path,
                    content_hash=entry.sha256,
                ),
                text="- 支持智能体身份认证\n- 提供 MCP 协议防护\n",
            )
        ],
    )
    page = build_wiki_page(draft, registry, status="published", updated_at="2026-01-01T00:00:00Z")
    store.write_page(page)
    return store


async def test_wiki_provider_end_to_end(source_root, wiki_root):
    make_source_file(source_root, "1-产品/overview.md", "# 产品\n支持智能体身份认证\n")
    from agent.wiki.source_registry import SourceRegistry

    registry = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    registry.sync()
    store = WikiStore(wiki_root)
    _build_wiki(store, registry)

    entry = registry.get_by_path("1-产品/overview.md")
    assert entry is not None
    index = WikiIndex(build_manifest(store))

    provider = WikiKnowledgeProvider(
        store,
        index=index,
        source_registry=registry,
        source_root=source_root,
    )
    bundle = await provider.collect_evidence(
        KnowledgeRequest(task_type="score", query="身份认证事件", product_ids=["agent_identity"])
    )
    assert bundle.wiki_version  # 非空
    assert bundle.status in {"SUFFICIENT", "INSUFFICIENT_EVIDENCE"}
    assert bundle.visited_pages


def test_legacy_provider_uses_backend_text():
    provider = LegacyKnowledgeProvider(
        legacy_backend=lambda req: "产品支持身份认证\n提供 MCP 防护\n"
    )

    async def run():
        return await provider.collect_evidence(
            KnowledgeRequest(task_type="score", query="q", product_ids=["agent_identity"])
        )

    import asyncio

    bundle = asyncio.run(run())
    assert bundle.evidence
    assert bundle.wiki_version == "legacy"
    assert bundle.status == "SUFFICIENT"


def test_shadow_provider_records_comparison():
    import asyncio

    legacy = LegacyKnowledgeProvider(legacy_backend=lambda req: "支持身份认证\n")

    class _WikiBrand:
        mode = "wiki"

        def __init__(self, bundle):
            self._bundle = bundle

        async def collect_evidence(self, request):
            return self._bundle

    wiki_bundle = EvidenceBundle(
        status="SUFFICIENT",
        query="q",
        evidence=[EvidenceItem(evidence_id="e1", fact="支持身份认证", page_id="p", confidence=0.9)],
        visited_pages=["p"],
        wiki_version="v1",
        confidence=0.9,
        coverage=0.6,
    )
    shadow = ShadowKnowledgeProvider(legacy=legacy, wiki=_WikiBrand(wiki_bundle))
    bundle = asyncio.run(
        shadow.collect_evidence(KnowledgeRequest(task_type="score", query="q", product_ids=[]))
    )
    assert bundle is wiki_bundle
    assert shadow.last_comparison["wiki_version"] == "v1"


async def test_factory_builds_all_modes(source_root, wiki_root):
    store = WikiStore(wiki_root)
    reg = build_knowledge_provider(mode="legacy")
    assert reg.mode == "legacy"
    w = build_knowledge_provider(
        mode="wiki", store=store, source_root=str(source_root), source_registry=None
    )
    assert w.mode == "wiki"
    s = build_knowledge_provider(
        mode="shadow",
        store=store,
        source_root=str(source_root),
        legacy_backend=lambda req: "x",
    )
    assert s.mode == "shadow"
    with pytest.raises(ValueError):
        build_knowledge_provider(mode="bogus", store=store)


def test_prompt_builder_uses_verified_evidence():

    bundle = EvidenceBundle(
        status="SUFFICIENT",
        query="q",
        evidence=[
            EvidenceItem(
                evidence_id="e1",
                fact="支持智能体身份认证",
                page_id="p",
                source_refs=[SourceRef(source_id="s", relative_path="a.md", content_hash="h")],
                confidence=0.9,
            )
        ],
        coverage=0.8,
        confidence=0.9,
        wiki_version="v",
    )
    from agent.scorer_v2 import ScoringAgentV2

    prompt = ScoringAgentV2._build_scoring_prompt_from_bundle(
        bundle, product_id="agent_identity", product_name="身份"
    )
    assert "## Verified Evidence" in prompt
    assert "支持智能体身份认证" in prompt
    assert "a.md" in prompt
    assert "agent_identity" in prompt
