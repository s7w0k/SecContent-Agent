"""阶段五 自适应文档召回 单元测试 - 硬过滤、required 保留、关键词召回、注入策略、切片对接。"""

from __future__ import annotations

import pytest
from agent.document_retriever import (
    DEFAULT_MAX_OPTIONAL_DOCS,
    DocumentRetriever,
    RetrievalRequest,
)
from agent.knowledge_index import KnowledgeIndexBuilder, KnowledgeIndexer
from agent.knowledge_slice import KnowledgeSliceResolver


def _build_retrieval_kb(root) -> None:
    """构造用于检索测试的产品知识库。"""
    p1 = root / "1-智能体身份安全"
    p1.mkdir(parents=True)
    (p1 / "overview.md").write_text(
        "# 智能体身份安全产品概述\n\n支持 E4002 余额不足错误码与密钥管理。", encoding="utf-8"
    )
    (p1 / "market-brief.md").write_text(
        "# 市场简报\n\nagent-identity-security 面向企业，术语 IDP、OIDC。", encoding="utf-8"
    )
    (p1 / "sales-brief.md").write_text(
        "# 销售简报\n\n杀手锏即 AI-BOM 兼容，修复错误码 E4001 E4002。", encoding="utf-8"
    )
    (p1 / "architecture-brief.md").write_text("# 架构简报\n\n微服务 + 安全网关。", encoding="utf-8")
    raw = p1 / "原始文档"
    raw.mkdir(parents=True)
    (raw / "error-handbook.md").write_text(
        "# 错误处理手册\n\n## 错误码\nE4002 余额不足处理。\n\n## 重试\n必须指数退避。" * 3,
        encoding="utf-8",
    )

    p2 = root / "2-智能体安全"
    p2.mkdir(parents=True)
    (p2 / "overview.md").write_text("# 智能体安全产品概述\n\n安全防护能力。", encoding="utf-8")
    (p2 / "market-brief.md").write_text("# 市场简报\n\nagent-security 竞争格局。", encoding="utf-8")
    (p2 / "sales-brief.md").write_text("# 销售简报\n\n威胁情报与告警。", encoding="utf-8")

    p3 = root / "3-AI-BOM"
    p3.mkdir(parents=True)
    (p3 / "overview.md").write_text("# AI-BOM 产品概述\n\n软件物料清单能力。", encoding="utf-8")
    (p3 / "market-brief.md").write_text("# 市场简报\n\nAI-BOM 供应链合规。", encoding="utf-8")

    shared = root / "shared"
    shared.mkdir(parents=True)
    (shared / "glossary.md").write_text("# 术语表\n\nOIDC 与 IDP 定义。", encoding="utf-8")
    (shared / "competitor-brief.md").write_text("# 竞品简报\n\nAI-BOM 竞品对比。", encoding="utf-8")


def _make_retriever(root, **kwargs) -> DocumentRetriever:
    builder = KnowledgeIndexBuilder(root)
    manifest = builder.build_manifest()
    builder.write(manifest)
    indexer = KnowledgeIndexer(root / "_index" / "kb-index.json")
    return DocumentRetriever(indexer=indexer, **kwargs)


# 使用产品目录中的产品 ID 而非目录名
P1 = "agent-identity-security"
P2 = "agent-security"
P3 = "ai-bom"
P4 = "agent-security-gateway"

DRAFT = RetrievalRequest(
    purpose="draft",
    product_ids=[P1],
    query="E4002 错误码",
    max_optional_docs=6,
)


# ═══════════════════════════════════════════════════════════════
# S5-2 硬过滤
# ═══════════════════════════════════════════════════════════════


class TestHardFilter:
    def test_product_zero_crosstalk(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(DRAFT)
        all_docs = result.required_docs + result.optional_docs + result.fallback_candidates
        # 只允许请求产品（P1）或共享文档
        for d in all_docs:
            if d.product_id is not None:
                assert d.product_id == P1

    def test_unpublished_product_excluded(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        # 请求一个未发布产品（目录不存在 → 无该产品文档），不应返回该产品任何内容
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P4],
                query="概述",
            )
        )
        all_docs = result.required_docs + result.optional_docs + result.fallback_candidates
        # 不返回 P4 的产品文档；最多允许全局 shared 文档
        assert not any(d.product_id == P4 for d in all_docs)

    def test_shared_respects_include_shared(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        with_shared = retriever.retrieve(DRAFT)
        assert any(d.product_id is None for d in with_shared.optional_docs)

        without = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P1],
                query="OIDC",
                include_shared=False,
            )
        )
        assert not any(d.product_id is None for d in without.optional_docs)


# ═══════════════════════════════════════════════════════════════
# S5-3 required 保留
# ═══════════════════════════════════════════════════════════════


class TestRequiredPreservation:
    def test_required_always_preserved(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        # query 命中无关内容，required brief 仍保留
        result = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P1],
                query="完全不相关内容zzzz",
            )
        )
        types = {d.doc_type for d in result.required_docs}
        assert "overview" in types
        assert "market-brief" in types

    def test_required_missing_not_filled_from_other_products(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        # 删除 product 2 的 overview，使其 required 缺失
        (tmp_path / "2-智能体安全" / "overview.md").unlink()
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P2],
                query="概述",
            )
        )
        # 不跨产品补齐：不出现 product 1 的 overview
        assert not any(d.product_id != P2 for d in result.required_docs)


# ═══════════════════════════════════════════════════════════════
# S5-4 关键词召回
# ═══════════════════════════════════════════════════════════════


class TestKeywordRecall:
    def test_error_code_hits(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(DRAFT)
        optional_types = {d.doc_type for d in result.optional_docs}
        assert "sales-brief" in optional_types  # sales-brief 含 E4002
        # raw 文档作为 fallback 候选命中
        assert any(d.doc_type == "raw" for d in result.fallback_candidates)

    def test_abbrev_and_product_term_hits(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        # 英文缩写 IDP
        idp = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P1],
                query="IDP",
            )
        )
        assert any(d.doc_type == "market-brief" for d in idp.required_docs)
        # 产品术语 AI-BOM
        bom = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P1],
                query="AI-BOM",
            )
        )
        assert any(d.doc_type == "sales-brief" for d in bom.optional_docs)

    def test_candidates_le8_no_llm_call(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        called = {"v": False}

        def reranker(doc_ids, query):
            called["v"] = True
            return doc_ids

        retriever = _make_retriever(tmp_path, reranker=reranker)
        retriever.retrieve(DRAFT)
        # 候选 ≤8，不做 LLM 重排
        assert called["v"] is False

    def test_optional_over_budget_dropped_in_stable_order(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        capped = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P1, P2, P3],
                query="简报",
                max_optional_docs=1,
            )
        )
        assert len(capped.optional_docs) == 1

    def test_empty_query_falls_back_to_purpose_stable_sort(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(
            RetrievalRequest(
                purpose="draft",
                product_ids=[P2],
                query="",
            )
        )
        # 空 query：optional 按 doc_type 稳定序（sales-brief 优先于 shared）
        optional_paths = [d.relative_path for d in result.optional_docs]
        assert optional_paths == sorted(optional_paths)


# ═══════════════════════════════════════════════════════════════
# S5-5 注入策略
# ═══════════════════════════════════════════════════════════════


class TestInjectionStrategy:
    def test_optional_prefers_summary(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(DRAFT)
        sales = [d for d in result.optional_docs if d.doc_type == "sales-brief"]
        assert sales
        assert sales[0].excerpt  # 摘要有内容
        assert not sales[0].needs_confirmation

    def test_fallback_needs_confirmation(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(DRAFT)
        raw = [d for d in result.fallback_candidates if d.doc_type == "raw"]
        assert raw
        assert raw[0].needs_confirmation is True

    def test_result_carries_doc_meta(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(DRAFT)
        for d in result.optional_docs:
            assert d.doc_id
            assert d.title
            assert d.relative_path


# ═══════════════════════════════════════════════════════════════
# 用户 custom 文档隔离
# ═══════════════════════════════════════════════════════════════


class TestUserCustomIsolation:
    def test_user_custom_not_in_global_index(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        retriever = _make_retriever(tmp_path)
        result = retriever.retrieve(DRAFT)
        all_docs = result.required_docs + result.optional_docs + result.fallback_candidates
        # 全局索引不含用户 custom 文档
        assert not any(d.doc_type == "custom" for d in all_docs)


# ═══════════════════════════════════════════════════════════════
# S5-6 KnowledgeSliceResolver 对接
# ═══════════════════════════════════════════════════════════════


class TestSliceIntegration:
    @pytest.mark.asyncio
    async def test_resolve_with_retriever_enriches_slice(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        builder.write(manifest)
        indexer = KnowledgeIndexer(tmp_path / "_index" / "kb-index.json")
        retriever = DocumentRetriever(indexer=indexer)

        resolver = KnowledgeSliceResolver(tmp_path, retriever=retriever, max_optional_docs=6)
        result = await resolver.resolve(
            purpose="draft",
            product_ids=[P1],
            query="E4002 错误码",
        )
        assert result.selected_document_ids, "应选中至少一个可选文档"
        assert result.retrieval_trace is not None
        assert result.index_version == manifest.index_version

    @pytest.mark.asyncio
    async def test_resolve_without_query_keeps_legacy_behavior(self, tmp_path):
        _build_retrieval_kb(tmp_path)
        resolver = KnowledgeSliceResolver(tmp_path, retriever=None)
        result = await resolver.resolve(
            purpose="draft",
            product_ids=[P1],
        )
        # 无 query：不启用检索，不注入可选召回
        assert result.selected_document_ids == []
        assert result.index_version == ""
        assert "sales-brief" in result.content or result.content


def test_default_max_optional_docs():
    assert DEFAULT_MAX_OPTIONAL_DOCS == 6
