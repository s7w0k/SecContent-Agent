"""DASHSCOPE embedding 召回注入（阶段八 11.2/11.3 评测用）。

基于阿里云 DashScope `text-embedding-v3`（OpenAI 兼容接口）：
- 对所有已发布产品下可检索 core 文档做批量向量化，构建 EmbeddingStore；
- 注入 DocumentRetriever 的 hybrid 排序（exact 关键词 + embedding 加权）。

用法（评测器内）：
    from ...embedding_provider import build_embedding_store, build_retriever_with_embedding
    store = build_embedding_store(indexer)
    retriever = build_retriever_with_embedding(indexer, store, embedding_weight=0.3)
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("agent_evals.knowledge_retrieval.embedding_provider")

EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 1024
DASH_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 文档向量化所用文本字段优先级（与关键词检索对齐，覆盖 title/description/summary/章节）
_DOC_TEXT_FIELDS = ("title", "description", "summary")


def _dashscope_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if key:
        return key
    env_file = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".env")
    env_file = os.path.abspath(env_file)
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DASHSCOPE_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


def _env_value(key_name: str) -> str:
    """从环境变量或仓库根 .env 读取配置值。"""
    val = os.environ.get(key_name, "").strip()
    if val:
        return val
    env_file = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".env")
    env_file = os.path.abspath(env_file)
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key_name}="):
                    return line.split("=", 1)[1].strip()
    return ""


def _doc_to_text(doc: Any) -> str:
    """把 DocIndex 拼成一个可用于向量化的文本段。"""
    parts: list[str] = []
    for field in _DOC_TEXT_FIELDS:
        val = getattr(doc, field, None)
        if val:
            parts.append(str(val))
    for s in doc.sections or []:
        if s.title:
            parts.append(s.title.replace("|", " "))
        if s.summary:
            parts.append(s.summary)
    return " ".join(parts)


def build_embedding_store(indexer: Any) -> Any:
    """对所有已发布可检索 core 文档批量向量化，构建 EmbeddingStore。

    无 DASHSCOPE key 或调用失败时返回 None（调用方退化为纯关键词排序）。
    """
    from agent.embedding_index import EmbeddingStore

    key = _dashscope_api_key()
    if not key:
        logger.warning("DASHSCOPE_API_KEY 未配置，embedding 召回跳过")
        return None

    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=DASH_BASE)

    docs = [d for d in (indexer.manifest.docs or []) if d.published]
    texts = [_doc_to_text(d) for d in docs]
    if not texts:
        return None

    store = EmbeddingStore(dim=EMBEDDING_DIM)

    def provider(texts_in: list[str]) -> list[list[float]]:
        # text-embedding-v3 单次 batch ≤10，分批调用
        out: list[list[float]] = []
        for i in range(0, len(texts_in), 10):
            batch = texts_in[i : i + 10]
            resp = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            out.extend(item.embedding for item in resp.data)
        return out

    store.set_provider(provider)
    vectors = store.embed_texts(texts)
    if not vectors:
        logger.warning("embedding 向量化失败，跳过 embedding 召回")
        return None

    for doc, vec in zip(docs, vectors, strict=True):
        store.add(doc.doc_id, vec)
    logger.info("embedding store 构建完成：%d 文档，dim=%d", store.size, store.dim)
    return store


def build_retriever_with_embedding(indexer: Any, store: Any, embedding_weight: float = 0.3) -> Any:
    """构建注入 embedding 的 DocumentRetriever（exact=1.0，embedding=weight）。"""
    from agent.document_retriever import DocumentRetriever

    return DocumentRetriever(
        indexer=indexer,
        embedding_store=store,
        hybrid_weights={"exact": 1.0, "embedding": float(embedding_weight)},
    )


def build_reranker(
    indexer: Any,
    top_n: int = 12,
    min_candidates: int = 2,
) -> Any:
    """构建基于 DEEPSEEK 的文档重排回调（注入候选文档标题/摘要，供 LLM 判断相关性）。

    返回 `Callable[[list[str], str], list[str]]`，兼容 DocumentRetriever.Reranker。
    无 DEEPSEEK key 或调用失败时返回 None（调用方退回混合排序）。
    """
    try:
        from agent.document_reranker import LLMDocumentReranker
    except Exception:
        return None

    key = _env_value("DEEPSEEK_API_KEY")
    if not key:
        logger.warning("DEEPSEEK_API_KEY 未配置，LLM 重排跳过")
        return None
    base_url = _env_value("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = _env_value("DEEPSEEK_MODEL") or "deepseek-chat"

    try:
        from openai import OpenAI
    except Exception:
        return None

    client = OpenAI(api_key=key, base_url=base_url)
    by_id = {d.doc_id: d for d in (indexer.manifest.docs or [])}

    def _snippet(doc: Any) -> str:
        title = doc.title or doc.doc_id
        summary = (doc.summary or "")[:120]
        return f"{title}" + (f" | {summary}" if summary else "")

    def _snippet_map(doc_id: str) -> str:
        doc = by_id.get(doc_id)
        if doc is None:
            return ""
        return _snippet(doc)

    def _llm_call(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "你是文档检索重排器。根据检索查询，把候选文档按相关性从高到低排序。"
                    '严格只输出 JSON：{"ranked_doc_ids": ["doc_id", ...]}。不得引入新 ID。',
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=1024,
        )
        return resp.choices[0].message.content or ""

    reranker = LLMDocumentReranker(
        llm_call=_llm_call,
        top_n=top_n,
        min_candidates=min_candidates,
        snippet_map=_snippet_map,
    )
    return reranker.rerank
