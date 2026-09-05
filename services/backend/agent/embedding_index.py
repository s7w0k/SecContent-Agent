"""轻量 embedding 召回索引（阶段八 11.2）。

按评测决定是否启用（`KNOWLEDGE_EMBEDDING_ENABLED`）。文档增长到千级、同义表达导致
BM25 召回不足时启用。首选内存/SQLite 能力，无需直接引入 Milvus/Qdrant。

设计：
- `EmbeddingStore`：doc_id → 向量 的内存存储，提供 cosine 相似度打分；
- `EmbeddingProvider`（可插拔）：把文本转成向量的回调（模型/重建成本可治理）；
- 与 `DocumentRetriever` 配合：注入 embedding 分数参与混合排序，权重可配置。

默认不启用（无 provider 时 score 返回 0），保证纯 BM25 路径行为不变。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable

logger = logging.getLogger("backend.agent.embedding_index")

# 文本向量化回调：输入文本，输出 float 向量
EmbeddingProvider = Callable[[list[str]], list[list[float]]]


class EmbeddingStore:
    """doc_id → 向量 的内存 embedding 存储（cosine 相似度）。"""

    def __init__(self, dim: int = 0):
        self._dim = dim
        self._vectors: dict[str, list[float]] = {}
        self._provider: EmbeddingProvider | None = None

    # ── 构建 ──────────────────────────────────────────────

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def size(self) -> int:
        return len(self._vectors)

    def set_provider(self, provider: EmbeddingProvider | None) -> None:
        """注入文本向量化器（可插拔）。"""
        self._provider = provider

    def add(self, doc_id: str, vector: list[float]) -> None:
        if not vector:
            return
        if self._dim and len(vector) != self._dim:
            raise ValueError(f"vector dim {len(vector)} mismatch store dim {self._dim}")
        if not self._dim:
            self._dim = len(vector)
        self._vectors[doc_id] = vector

    def embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        """用 provider 批量向量化；无 provider 返回 None。"""
        if self._provider is None:
            return None
        try:
            return self._provider(texts)
        except Exception as exc:
            logger.warning("embedding provider failed: %s", exc)
            return None

    # ── 查询 ──────────────────────────────────────────────

    def score(self, doc_id: str, query_vector: list[float] | None) -> float:
        """返回 doc 与 query 的 cosine 相似度；缺向量返回 0.0。"""
        if not query_vector:
            return 0.0
        vec = self._vectors.get(doc_id)
        if not vec:
            return 0.0
        return _cosine(vec, query_vector)

    def score_many(self, doc_ids: list[str], query_vector: list[float] | None) -> dict[str, float]:
        return {d: self.score(d, query_vector) for d in doc_ids}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
