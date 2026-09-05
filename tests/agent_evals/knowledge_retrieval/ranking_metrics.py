"""信息检索排序指标（Recall@K / Precision@K / MRR / NDCG@K / HitRate）。

纯函数，无副作用，便于单测与复用。所有函数输入：
  ranked : list[str]   检索器返回的有序 doc_id 列表（从高到低）。
  relevant : set[str]  该 query 的相关（ground-truth）doc_id 集合。

K 默认取 [1,3,5]；MRR 与 HitRate 为全局标量。
"""

from __future__ import annotations

DEFAULT_KS = (1, 3, 5)


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """前 k 个命中相关文档数 / 相关文档总数；无相关文档视为 0（避免除零）。"""
    if not relevant:
        return 0.0
    hit = sum(1 for d in ranked[:k] if d in relevant)
    return hit / len(relevant)


def precision_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """前 k 个中相关文档占比。"""
    if k <= 0:
        return 0.0
    hit = sum(1 for d in ranked[:k] if d in relevant)
    return hit / k


def mrr(ranked: list[str], relevant: set[str]) -> float:
    """平均倒数排名：第一个相关文档排名的倒数；无相关则为 0。"""
    for i, doc in enumerate(ranked, start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """归一化折损累计增益（二值相关：命中=1 否则=0）。"""
    dcg = 0.0
    for i, doc in enumerate(ranked[:k], start=1):
        rel = 1.0 if doc in relevant else 0.0
        dcg += rel / _log2(i + 1)
    if not relevant:
        return 0.0
    idcg = sum(1.0 / _log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / idcg if idcg > 0 else 0.0


def hit_rate(ranked: list[str], relevant: set[str], k: int) -> float:
    """前 k 个是否至少命中一个相关文档（0/1）。"""
    return 1.0 if any(d in relevant for d in ranked[:k]) else 0.0


def _log2(x: float) -> float:
    import math

    return math.log2(x)


def per_query_metrics(
    ranked: list[str],
    relevant: set[str],
    ks: tuple[int, ...] = DEFAULT_KS,
) -> dict:
    """单条 query 的完整指标。"""
    return {
        "recall": {f"@{k}": recall_at_k(ranked, relevant, k) for k in ks},
        "precision": {f"@{k}": precision_at_k(ranked, relevant, k) for k in ks},
        "mrr": mrr(ranked, relevant),
        "ndcg": {f"@{k}": ndcg_at_k(ranked, relevant, k) for k in ks},
        "hit_rate": {f"@{k}": hit_rate(ranked, relevant, k) for k in ks},
    }


def aggregate(per_query: list[dict], ks: tuple[int, ...] = DEFAULT_KS) -> dict:
    """对多 query 指标做宏平均。"""
    n = len(per_query) or 1
    agg: dict = {
        "recall": {},
        "precision": {},
        "ndcg": {},
        "hit_rate": {},
        "mrr": 0.0,
    }
    for k in ks:
        agg["recall"][f"@{k}"] = sum(q["recall"][f"@{k}"] for q in per_query) / n
        agg["precision"][f"@{k}"] = sum(q["precision"][f"@{k}"] for q in per_query) / n
        agg["ndcg"][f"@{k}"] = sum(q["ndcg"][f"@{k}"] for q in per_query) / n
        agg["hit_rate"][f"@{k}"] = sum(q["hit_rate"][f"@{k}"] for q in per_query) / n
    agg["mrr"] = sum(q["mrr"] for q in per_query) / n
    return agg
