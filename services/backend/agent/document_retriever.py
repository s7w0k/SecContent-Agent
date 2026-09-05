"""DocumentRetriever - 基于知识索引的自适应文档召回（阶段五）。

职责：
  - S5-1 检索模型：RetrievalRequest / RetrievalResult / RetrievedDocument / RetrievalTrace
  - S5-2 硬过滤：产品、purpose、published、tenant/user、allowed tier、路径安全
  - S5-3 required 保留：复用 PURPOSE_DOC_TYPES 分层，required 缺失时记录、不跨产品补齐
  - S5-4 首期关键词/BM25 召回：标题/keywords/description/summary 打分，精确术语加权，
          purpose 与 doc_type 优先级；候选 ≤8 不做不必要 LLM 重排
  - S5-5 注入策略：required 短 brief 注入正文、optional 优先 summary、fallback/raw 仅章节摘要
          或候选提示；每块携带 doc_id/title/source_path；needs_confirmation 显式标注

本阶段仅做确定性召回，不调用 LLM 重排（LLM 重排留待阶段八按评测启用）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.knowledge_index import (
    DocIndex,
    KnowledgeIndexer,
    KnowledgeIndexManifest,
)
from agent.knowledge_slice import PURPOSE_DOC_TYPES, Purpose
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.document_retriever")

# 候选数 ≤ 该阈值时不做 LLM 重排（避免不必要计算）
LLM_RERANK_MIN_CANDIDATES = 8

# 可选文档默认上限（与 config.KNOWLEDGE_MAX_OPTIONAL_DOCS 对齐）
DEFAULT_MAX_OPTIONAL_DOCS = 6

# 注入段预算（字符）
OPTIONAL_EXCERPT_CHARS = 800
FALLBACK_SECTION_CHARS = 400

# query 分词：空白 + 中英文常见标点
_TERM_SPLIT = re.compile(r"[\s，。；、,.!?；：:（）()\[\]【】“”\"'\-]+")


class RetrievalRequest(BaseModel):
    """一次检索请求。"""

    purpose: Purpose
    user_id: str | None = None
    product_ids: list[str] = Field(default_factory=list)
    query: str = ""
    max_optional_docs: int = DEFAULT_MAX_OPTIONAL_DOCS
    include_shared: bool = True


class RetrievalTrace(BaseModel):
    """检索过程追踪（用于 trace 还原每次来源）。"""

    index_version: str = ""
    query: str = ""
    purpose: str = ""
    product_ids: list[str] = Field(default_factory=list)
    hard_filtered: int = 0
    required_ids: list[str] = Field(default_factory=list)
    optional_ids: list[str] = Field(default_factory=list)
    fallback_ids: list[str] = Field(default_factory=list)


class RetrievedDocument(BaseModel):
    """召回并注入就绪的文档。"""

    doc_id: str
    relative_path: str
    title: str
    doc_type: str
    tier: str
    product_id: str | None = None
    purpose: str = ""
    score: float = 0.0
    excerpt: str = Field(default="", description="注入用内容（正文或摘要）")
    needs_confirmation: bool = Field(
        default=False, description="是否为 fallback/raw，需生成模型确认"
    )


class RetrievalResult(BaseModel):
    """检索结果，区分 required / optional / fallback 候选。"""

    required_docs: list[RetrievedDocument] = Field(default_factory=list)
    optional_docs: list[RetrievedDocument] = Field(default_factory=list)
    fallback_candidates: list[RetrievedDocument] = Field(default_factory=list)
    trace: RetrievalTrace = Field(default_factory=RetrievalTrace)


def _doc_type_rank(doc_type: str, purpose: Purpose) -> tuple[int, int]:
    """purpose 稳定排序：required 优先（按 required 列表序），optional 次之。"""
    config = PURPOSE_DOC_TYPES.get(purpose, {"required": [], "optional": []})
    required = config.get("required", [])
    optional = config.get("optional", [])
    if doc_type in required:
        return (0, required.index(doc_type))
    if doc_type in optional:
        return (1, optional.index(doc_type))
    return (2, 0)


def _stable_key(doc: DocIndex, purpose: Purpose) -> tuple:
    """无 query 时的稳定排序键：purpose 分层 → 路径。"""
    rank = _doc_type_rank(doc.doc_type, purpose)
    return (rank[0], rank[1], doc.relative_path)


def _tokenize(query: str) -> list[str]:
    """切分 query 得到检索词。

    对中文启用 jieba 分词（解决整句无法与文档子词匹配的问题），
    英文/数字仍按空白与标点切分；jieba 不可用时回退原规则切分。
    """
    text = (query or "").lower()
    if not text:
        return []
    try:
        import jieba

        terms = [t.strip() for t in jieba.cut_for_search(text)]
        filtered = [t for t in terms if t and not _is_pure_punct(t)]
        # 若分词退化为空（如纯英文标点），回退原规则切分
        if filtered:
            return filtered
    except Exception:
        pass
    return [t for t in _TERM_SPLIT.split(text) if t]


def _is_pure_punct(t: str) -> bool:
    return not any(ch.isalnum() for ch in t)


def _keyword_score(doc: DocIndex, query: str) -> float:
    """确定性关键词打分：标题 > 精确术语 > description > summary > 章节内容。

    覆盖文档标题、keywords、description、summary，以及各章节的 title 与 summary
    （正文摘要，阶段六按需展开），使答案藏在章节内的文档也能被召回。
    """
    terms = _tokenize(query)
    if not terms:
        return 0.0
    title = doc.title.lower()
    summary = (doc.summary or "").lower()
    description = (doc.description or "").lower()
    keywords = [k.lower() for k in doc.keywords]
    keywords_text = " ".join(keywords)

    # 章节内容（title + summary）合并为一块可检索文本，权重低于文档级字段
    section_text = _section_searchable_text(doc)

    score = 0.0
    for term in terms:
        if term in title:
            score += 3.0
        if term in keywords or term in keywords_text.split():
            score += 2.0  # 精确术语额外加权
        if term in description:
            score += 1.0
        if term in summary:
            score += 1.0
        if term in section_text:
            score += 0.5  # 章节命中（正文摘要），权重较低但可召回
    return score


def _section_searchable_text(doc: DocIndex) -> str:
    """把文档各章节的 title + summary 拼成可检索文本（小写）。"""
    parts: list[str] = []
    for s in doc.sections or []:
        if s.title:
            parts.append(s.title.lower())
        if s.summary:
            parts.append(s.summary.lower())
    return " ".join(parts)


# 可选 LLM 重排器签名：输入候选 doc_id 列表 + query，返回按相关度降序的 doc_id 列表
Reranker = Callable[[list[str], str], list[str]]


class DocumentRetriever:
    """基于知识索引做硬过滤、required 保留与关键词召回。"""

    def __init__(
        self,
        indexer: KnowledgeIndexer | None = None,
        index_path: str | Path | None = None,
        settings: Any = None,
        reranker: Reranker | None = None,
        embedding_store: Any = None,
        hybrid_weights: dict[str, float] | None = None,
    ):
        self._indexer = indexer or KnowledgeIndexer(index_path)
        self._settings = settings
        self._reranker = reranker
        # 阶段八 11.2/11.3：可选 embedding 存储与混合排序权重
        self._embedding_store = embedding_store
        self._embedding_weight = (
            float(getattr(settings, "KNOWLEDGE_EMBEDDING_WEIGHT", 0.0))
            if settings is not None
            else 0.0
        )
        if hybrid_weights is not None:
            # 显式传入的权重优先（评测调优用），否则取配置
            self._embedding_weight = float(hybrid_weights.get("embedding", 0.0))
            self._exact_weight = float(hybrid_weights.get("exact", 1.0))
        else:
            self._exact_weight = 1.0
        self._manifest: KnowledgeIndexManifest | None = None

    # ── 加载 ──────────────────────────────────────────────

    def load(self) -> KnowledgeIndexManifest | None:
        """加载索引；缺失/失败返回 None（检索退化）。"""
        self._manifest = self._indexer.load()
        return self._manifest

    # ── 主入口 ────────────────────────────────────────────

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """执行检索：硬过滤 → required 保留 → optional/fallback 召回排序。"""
        manifest = self._manifest or self.load()
        if manifest is None:
            return RetrievalResult(
                trace=RetrievalTrace(
                    index_version="",
                    query=request.query,
                    purpose=request.purpose,
                    product_ids=list(request.product_ids),
                )
            )

        filtered = self._hard_filter(manifest.docs, request)

        required, optional, fallback = self._partition(filtered, request)

        # S5-3 required 保留：缺失记录（不跨产品补齐）
        required_ids = [d.doc_id for d in required]

        # S5-4 召回排序
        ranked_optional = self._rank(optional, request)
        ranked_fallback = self._rank(fallback, request)

        optional_docs = ranked_optional[: request.max_optional_docs]
        fallback_candidates = ranked_fallback[: request.max_optional_docs]

        required_docs = [
            self._to_retrieved(d, request, needs_confirmation=False, tier_kind="required")
            for d in required
        ]
        optional_docs_r = [
            self._to_retrieved(d, request, needs_confirmation=False, tier_kind="optional")
            for d in optional_docs
        ]
        fallback_r = [
            self._to_retrieved(d, request, needs_confirmation=True, tier_kind="fallback")
            for d in fallback_candidates
        ]

        trace = RetrievalTrace(
            index_version=manifest.index_version,
            query=request.query,
            purpose=request.purpose,
            product_ids=list(request.product_ids),
            hard_filtered=len(filtered),
            required_ids=required_ids,
            optional_ids=[d.doc_id for d in optional_docs],
            fallback_ids=[d.doc_id for d in fallback_candidates],
        )

        return RetrievalResult(
            required_docs=required_docs,
            optional_docs=optional_docs_r,
            fallback_candidates=fallback_r,
            trace=trace,
        )

    def retrieve_ranked(self, request: RetrievalRequest) -> list[RetrievedDocument]:
        """纯相关性排序：对硬过滤后的全量候选按相关性降序返回。

        与 `retrieve()` 的区别：不做 required/optional/fallback 注入分区、
        不前置 required 文档，仅衡量检索策略的相关性排序质量（IR 指标评测用）。

        raw/shared 巨正文本会虚高得分并污染 LLM 重排窗口，且非 IR 评测目标，
        故在此排除，仅对 core（allowed types）文档排序。
        """
        manifest = self._manifest or self.load()
        if manifest is None:
            return []
        purpose = request.purpose
        allowed_types = set(PURPOSE_DOC_TYPES.get(purpose, {}).get("required", [])) | set(
            PURPOSE_DOC_TYPES.get(purpose, {}).get("optional", [])
        )
        filtered = [
            d for d in self._hard_filter(manifest.docs, request) if d.doc_type in allowed_types
        ]
        ranked = self._rank(filtered, request)
        return [
            self._to_retrieved(d, request, needs_confirmation=False, tier_kind="candidate")
            for d in ranked
        ]

    # ── S5-2 硬过滤 ───────────────────────────────────────

    def _hard_filter(
        self,
        docs: list[DocIndex],
        request: RetrievalRequest,
    ) -> list[DocIndex]:
        """严格过滤：产品、purpose、published、共享可见性、路径安全。"""
        purpose = request.purpose
        product_set = set(request.product_ids)
        allowed_types = set(PURPOSE_DOC_TYPES.get(purpose, {}).get("required", [])) | set(
            PURPOSE_DOC_TYPES.get(purpose, {}).get("optional", [])
        )

        result: list[DocIndex] = []
        for doc in docs:
            # 路径安全（索引已校验，此处防御性复核）
            if not self._safe_rel(doc.relative_path):
                continue
            # 未发布文档不参与检索
            if not doc.published:
                continue
            # 产品硬过滤：产品文档必须属于请求产品（零串扰）
            if doc.product_id is not None and doc.product_id not in product_set:
                continue
            # 共享文档：受 include_shared 控制
            if doc.product_id is None and not request.include_shared:
                continue
            # purpose/doc_type 过滤：raw 视为 fallback 候选，shared 视为可选上下文，
            # 其余必须属于 allowed types
            if doc.doc_type == "raw":
                result.append(doc)
                continue
            if doc.doc_type == "shared":
                result.append(doc)
                continue
            if doc.doc_type not in allowed_types:
                continue
            result.append(doc)
        return result

    @staticmethod
    def _safe_rel(rel: str) -> bool:
        if not rel:
            return False
        if ".." in rel.split("/"):
            return False
        return not (rel.startswith("/") or ":" in rel[:2])

    # ── 分区 ──────────────────────────────────────────────

    def _partition(
        self,
        docs: list[DocIndex],
        request: RetrievalRequest,
    ) -> tuple[list[DocIndex], list[DocIndex], list[DocIndex]]:
        """required / optional / fallback。"""
        purpose = request.purpose
        required_types = set(PURPOSE_DOC_TYPES.get(purpose, {}).get("required", []))
        required: list[DocIndex] = []
        optional: list[DocIndex] = []
        fallback: list[DocIndex] = []
        for doc in docs:
            if doc.doc_type == "raw":
                fallback.append(doc)
            elif doc.doc_type in required_types:
                required.append(doc)
            else:
                # 其余（optional 类型 + shared）视为 optional
                optional.append(doc)
        return required, optional, fallback

    # ── S5-4 排序 ─────────────────────────────────────────

    def _rank(self, docs: list[DocIndex], request: RetrievalRequest) -> list[DocIndex]:
        """按相关性排序；空 query 退回 purpose 稳定排序。

        候选 ≤LLM_RERANK_MIN_CANDIDATES 时不做 LLM 重排（避免不必要调用）。
        """
        if not docs:
            return []
        query = (request.query or "").strip()

        if not query:
            return sorted(docs, key=lambda d: _stable_key(d, request.purpose))

        # 混合排序：exact/BM25 关键词分数 + 可选 embedding 相似度（权重可配置）
        hybrid = self._hybrid_score(docs, query)

        # LLM 重排：候选超过阈值且配置了重排器时，只重排 Top-N（阶段八 11.1）
        use_rerank = self._reranker is not None and len(docs) > LLM_RERANK_MIN_CANDIDATES
        if use_rerank:
            try:
                ordered_ids = self._reranker([d.doc_id for d in docs], request.query)
                by_id = {d.doc_id: d for d in docs}
                reranked = [by_id[i] for i in ordered_ids if i in by_id]
                # 重排后补充未命中的候选
                appended = [d for d in docs if d not in reranked]
                return reranked + appended
            except Exception as exc:
                logger.warning("DocumentRetriever reranker failed: %s", exc)

        scored = [(hybrid.get(d.doc_id, 0.0), _stable_key(d, request.purpose), d) for d in docs]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [d for _, _, d in scored]

    def _hybrid_score(self, docs: list[DocIndex], query: str) -> dict[str, float]:
        """计算混合排序分数：exact_score * exact_weight + embedding * embedding_weight。

        默认 embedding_weight=0（未启用），返回纯关键词分数，保持阶段五行为不变。
        """
        scores = {d.doc_id: _keyword_score(d, query) * self._exact_weight for d in docs}
        if self._embedding_store is not None and self._embedding_weight > 0:
            try:
                qvectors = self._embedding_store.embed_texts([query])
                if qvectors:
                    qv = qvectors[0]
                    emb_scores = self._embedding_store.score_many([d.doc_id for d in docs], qv)
                    for doc_id, s in emb_scores.items():
                        scores[doc_id] = scores.get(doc_id, 0.0) + (s * self._embedding_weight)
            except Exception as exc:  # embedding 失败不影响关键词排序
                logger.warning("DocumentRetriever hybrid embedding failed: %s", exc)
        return scores

    # ── S5-5 注入就绪内容 ─────────────────────────────────

    def _to_retrieved(
        self,
        doc: DocIndex,
        request: RetrievalRequest,
        *,
        needs_confirmation: bool,
        tier_kind: str,
    ) -> RetrievedDocument:
        score = _keyword_score(doc, request.query) if request.query else 0.0
        excerpt = self._build_excerpt(doc, needs_confirmation=needs_confirmation)
        return RetrievedDocument(
            doc_id=doc.doc_id,
            relative_path=doc.relative_path,
            title=doc.title,
            doc_type=doc.doc_type,
            tier=doc.tier,
            product_id=doc.product_id,
            purpose=tier_kind,
            score=score,
            excerpt=excerpt,
            needs_confirmation=needs_confirmation,
        )

    def _build_excerpt(
        self,
        doc: DocIndex,
        *,
        needs_confirmation: bool,
    ) -> str:
        """按注入策略生成注入就绪片段。

        - required/optional：优先 summary（无摘要时用 description）
        - fallback/raw：仅章节摘要或候选提示，不默认正文
        """
        if needs_confirmation:
            return self._fallback_excerpt(doc)
        summary = (doc.summary or "").strip()
        if not summary:
            summary = (doc.description or "").strip()
        if len(summary) > OPTIONAL_EXCERPT_CHARS:
            summary = summary[:OPTIONAL_EXCERPT_CHARS].rstrip() + "…"
        return summary

    def _fallback_excerpt(self, doc: DocIndex) -> str:
        """fallback/raw：合并章节摘要，附候选提示。"""
        if doc.sections:
            parts = []
            for s in doc.sections[:3]:
                seg = (s.summary or "").strip()
                if seg:
                    parts.append(f"- {s.title}: {seg}")
            if parts:
                return "\n".join(parts)[:OPTIONAL_EXCERPT_CHARS]
        return f"（原始文档候选，需确认后再展开：{doc.title}）"
