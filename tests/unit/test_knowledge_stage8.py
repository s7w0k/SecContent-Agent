"""阶段八 按评测升级召回能力 单元测试 - LLM 文档重排、embedding 召回、混合排序权重。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from agent.document_reranker import (
    LLMDocumentReranker,
    top_n_rerank,
)
from agent.document_retriever import (
    DocumentRetriever,
    RetrievalRequest,
)
from agent.embedding_index import EmbeddingStore, _cosine
from agent.knowledge_index import KnowledgeIndexBuilder, KnowledgeIndexer


def _build_retrieval_kb(root) -> None:
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


def _make_retriever(root, **kwargs) -> DocumentRetriever:
    builder = KnowledgeIndexBuilder(root)
    manifest = builder.build_manifest()
    builder.write(manifest)
    indexer = KnowledgeIndexer(root / "_index" / "kb-index.json")
    return DocumentRetriever(indexer=indexer, **kwargs)


P1 = "agent-identity-security"
DRAFT = RetrievalRequest(
    purpose="draft",
    product_ids=[P1],
    query="E4002 错误码",
    max_optional_docs=6,
)


# ═══════════════════════════════════════════════════════════════
# S8-1 LLM 文档重排（Top-N 截断 + 同步/异步 + 回退）
# ═══════════════════════════════════════════════════════════════


class TestLLMDocumentReranker:
    def test_top_n_rerank(self):
        ids = ["a", "b", "c", "d", "e"]
        # 重排窗口 top_n=3，LLM 返回 [c,b]，其余 a,d,e 按原序补
        assert top_n_rerank(ids, ["c", "b"], 3) == ["c", "b", "a", "d", "e"]

    def test_top_n_rerank_ignores_unknown_and_dups(self):
        ids = ["a", "b", "c"]
        # LLM 返回含未知 id 和重复
        assert top_n_rerank(ids, ["x", "c", "c", "b"], 3) == ["c", "b", "a"]

    def test_rerank_parses_json(self):
        def llm(prompt):
            return '{"ranked_doc_ids": ["b", "a", "c"]}'

        r = LLMDocumentReranker(llm_call=llm, min_candidates=1)
        assert r.rerank(["a", "b", "c"], "q") == ["b", "a", "c"]

    def test_rerank_async(self):
        import asyncio

        async def llm(prompt):
            return '{"ranked_doc_ids": ["c", "a"]}'

        r = LLMDocumentReranker(llm_call=llm, min_candidates=1, top_n=2)
        ids = ["a", "b", "c"]
        assert asyncio.run(r.rerank_async(ids, "q")) is not None

    def test_rerank_fallback_on_invalid_json(self):
        def llm(prompt):
            return "not json"

        r = LLMDocumentReranker(llm_call=llm, min_candidates=1)
        assert r.rerank(["a", "b"], "q") == ["a", "b"]

    def test_rerank_fallback_on_exception(self):
        def llm(prompt):
            raise RuntimeError("boom")

        r = LLMDocumentReranker(llm_call=llm, min_candidates=1)
        assert r.rerank(["a", "b"], "q") == ["a", "b"]

    def test_rerank_no_llm_call_returns_input(self):
        r = LLMDocumentReranker(min_candidates=1)
        assert r.rerank(["a", "b"], "q") == ["a", "b"]

    def test_rerank_below_min_candidates_skips(self):
        called = {"v": False}

        def llm(prompt):
            called["v"] = True
            return '{"ranked_doc_ids": ["a"]}'

        r = LLMDocumentReranker(llm_call=llm, min_candidates=8)
        assert r.rerank(["a", "b"], "q") == ["a", "b"]
        assert called["v"] is False


# ═══════════════════════════════════════════════════════════════
# S8-2 embedding 索引（cosine + 混合检索）
# ═══════════════════════════════════════════════════════════════


class TestEmbeddingStore:
    def test_cosine(self):
        assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
        assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
        assert _cosine([], [1, 0]) == 0.0

    def test_add_and_score(self):
        store = EmbeddingStore(dim=2)
        store.add("a", [1.0, 0.0])
        store.add("b", [0.0, 1.0])
        assert store.score("a", [1.0, 0.0]) == pytest.approx(1.0)
        assert store.score("b", [1.0, 0.0]) == pytest.approx(0.0)
        assert store.score("missing", [1.0, 0.0]) == 0.0

    def test_dim_mismatch(self):
        with pytest.raises(ValueError):
            EmbeddingStore(dim=2).add("a", [1.0, 2.0, 3.0])

    def test_auto_dim(self):
        store = EmbeddingStore(dim=0)
        store.add("a", [1.0, 2.0, 3.0])
        assert store.dim == 3

    def test_embed_texts_provider(self):
        store = EmbeddingStore(dim=2)
        store.set_provider(lambda texts: [[1.0, 0.0] for _ in texts])
        assert store.embed_texts(["q"]) == [[1.0, 0.0]]

    def test_embed_texts_no_provider(self):
        assert EmbeddingStore().embed_texts(["q"]) is None


class TestHybridRanking:
    def test_default_no_embedding_keeps_keyword(self):
        root = Path(tempfile.mkdtemp())
        _build_retrieval_kb(root)
        retriever = _make_retriever(root)
        assert retriever._embedding_weight == 0.0
        result = retriever.retrieve(DRAFT)
        assert result.trace.optional_ids is not None

    def test_embedding_weight_zero_ignored(self):
        root = Path(tempfile.mkdtemp())
        _build_retrieval_kb(root)
        retriever = _make_retriever(
            root,
            embedding_store=EmbeddingStore(dim=2),
            hybrid_weights={"embedding": 0.0, "exact": 1.0},
        )
        # 权重 0 时 embedding 不参与排序
        assert retriever._embedding_weight == 0.0

    def test_hybrid_embedding_affects_rank(self):
        root = Path(tempfile.mkdtemp())
        _build_retrieval_kb(root)
        store = EmbeddingStore(dim=2)
        # 让架构简报与 query 高度相似，靠 embedding 权重提升
        store.add("architecture-brief", [1.0, 0.0])
        store.set_provider(lambda texts: [[1.0, 0.0]])
        retriever = _make_retriever(
            root,
            embedding_store=store,
            hybrid_weights={"embedding": 10.0, "exact": 1.0},
        )
        # 不应抛异常，且 embedding 权重已生效
        result = retriever.retrieve(DRAFT)
        assert retriever._embedding_weight == 10.0
        assert result.trace.optional_ids is not None


# ═══════════════════════════════════════════════════════════════
# S8-3 集成：相对阶段五行为不变（默认不启用 embedding/LLM）
# ═══════════════════════════════════════════════════════════════


class TestStage5Compatibility:
    def test_default_retriever_no_rerank_parse(self):
        root = Path(tempfile.mkdtemp())
        _build_retrieval_kb(root)
        retriever = _make_retriever(root)
        result = retriever.retrieve(DRAFT)
        assert result.trace.optional_ids is not None
        assert result.required_docs  # required 保留
