"""
仪表盘数据 REST API — 文章列表、统计、详情

端点:
  GET /api/articles            文章列表（分页+筛选+排序）
  GET /api/articles/{hash}     单篇文章详情
  GET /api/stats               统计概览
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api", tags=["Dashboard"])


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_db(request: Request):
    """从 app.state 获取 MongoDB 数据库实例"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


# ═══════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════


@router.get("/articles", summary="文章列表")
async def list_articles(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    source_type: Optional[str] = Query(default=None, description="来源类型: overseas_news / wechat_mp"),
    category: Optional[str] = Query(default=None, description="分类筛选"),
    min_score: Optional[int] = Query(default=None, ge=0, le=200, description="最低综合分"),
    is_ai_security: Optional[bool] = Query(default=None, description="是否AI安全相关"),
    is_high_value: Optional[bool] = Query(default=None, description="是否高分文章(≥140)"),
    keyword: Optional[str] = Query(default=None, description="标题/摘要关键词搜索"),
    sort_by: str = Query(default="added_at", description="排序字段"),
    order: str = Query(default="desc", description="排序方向: asc / desc"),
):
    """分页查询文章列表，支持多条件筛选和排序。

    示例:
      GET /api/articles?page=1&page_size=20&source_type=overseas_news&min_score=100
    """
    db = _get_db(request)

    # 构建查询条件
    query: dict = {}

    if source_type:
        query["source_type"] = source_type
    if category:
        query["category"] = category
    if is_ai_security is not None:
        query["is_ai_security"] = is_ai_security

    # 排序
    sort_order = -1 if order == "desc" else 1
    allowed_sort_fields = {"added_at", "title", "source", "ai_relevance_score", "reportability_score"}
    if sort_by not in allowed_sort_fields:
        sort_by = "added_at"

    # 查询总数
    total = await db["articles"].count_documents(query)

    # 分页查询
    skip = (page - 1) * page_size
    cursor = db["articles"].find(query).sort(sort_by, sort_order).skip(skip).limit(page_size)
    articles = await cursor.to_list(length=page_size)

    # 后过滤（MongoDB 不支持动态计算字段筛选）
    items = []
    for art in articles:
        art["_id"] = str(art["_id"])
        total_score = art.get("ai_relevance_score", 0) + art.get("reportability_score", 0)
        art["total_score"] = total_score
        art["is_high_value"] = total_score >= 140

        # min_score 过滤
        if min_score is not None and total_score < min_score:
            continue
        # is_high_value 过滤
        if is_high_value is not None and art["is_high_value"] != is_high_value:
            continue
        # keyword 过滤
        if keyword:
            kw_lower = keyword.lower()
            title = (art.get("title") or "").lower()
            summary = (art.get("summary") or "").lower()
            summary_cn = (art.get("summary_cn") or "").lower()
            if kw_lower not in title and kw_lower not in summary and kw_lower not in summary_cn:
                continue

        items.append(art)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/articles/{url_hash}", summary="文章详情")
async def get_article(url_hash: str, request: Request):
    """获取单篇文章完整详情（含原文 Markdown 全文）"""
    db = _get_db(request)

    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    article["_id"] = str(article["_id"])
    total_score = article.get("ai_relevance_score", 0) + article.get("reportability_score", 0)
    article["total_score"] = total_score
    article["is_high_value"] = total_score >= 140

    return article


@router.get("/stats", summary="统计概览")
async def get_stats(request: Request):
    """返回仪表盘统计数据：总数、分类分布、来源分布、评分分布"""
    db = _get_db(request)

    total = await db["articles"].count_documents({})
    ai_security_count = await db["articles"].count_documents({"is_ai_security": True})
    high_value_count = await db["articles"].count_documents({
        "$expr": {
            "$gte": [
                {"$add": ["$ai_relevance_score", "$reportability_score"]},
                140,
            ]
        }
    }) if total > 0 else 0

    # 来源分布（聚合）
    source_pipeline = [
        {"$group": {"_id": "$source_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    source_cursor = db["articles"].aggregate(source_pipeline)
    source_dist = {}
    try:
        async for doc in source_cursor:
            source_dist[doc["_id"]] = doc["count"]
    except Exception:
        pass

    # 分类分布
    cat_pipeline = [
        {"$match": {"is_ai_security": True, "category": {"$ne": ""}}},
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    cat_cursor = db["articles"].aggregate(cat_pipeline)
    cat_dist = {}
    try:
        async for doc in cat_cursor:
            cat_dist[doc["_id"]] = doc["count"]
    except Exception:
        pass

    return {
        "total_articles": total,
        "ai_security_count": ai_security_count,
        "high_value_count": high_value_count,
        "source_distribution": source_dist,
        "category_distribution": cat_dist,
    }
