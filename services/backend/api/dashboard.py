"""
仪表盘数据 REST API — 文章列表、统计、详情

端点:
  GET /api/articles            文章列表（分页+筛选+排序）
  GET /api/articles/{hash}     单篇文章详情
  GET /api/stats               统计概览
"""

from __future__ import annotations

import logging

from auth.deps import get_current_user
from clients.mcp_crawl import RequestContext
from config import get_settings
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from logging_config import get_trace_id

router = APIRouter(prefix="/api", tags=["Dashboard"])
logger = logging.getLogger("backend.api.dashboard")


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_db(request: Request):
    """从 app.state 获取 MongoDB 数据库实例"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


async def _attach_user_drafts(db, article: dict, user_id: str) -> dict:
    """用当前用户的独立草稿替换文章中的旧共享草稿。"""
    user_draft = await db["user_drafts"].find_one(
        {"user_id": user_id, "article_url_hash": article["url_hash"]}
    )
    drafts = user_draft.get("drafts", []) if user_draft else []
    article.pop("pr_drafts", None)
    article["pr_drafts"] = drafts
    article["can_generate"] = not bool(drafts)
    return article


# ═══════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════


@router.get("/articles", summary="文章列表")
async def list_articles(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    source_type: str | None = Query(
        default=None, description="来源类型: overseas_news / wechat_mp"
    ),
    category: str | None = Query(default=None, description="分类筛选"),
    min_score: int | None = Query(default=None, ge=0, le=200, description="最低综合分"),
    is_ai_security: bool | None = Query(default=None, description="是否AI安全相关"),
    is_high_value: bool | None = Query(default=None, description="是否高分文章(≥140)"),
    keyword: str | None = Query(default=None, description="标题/摘要关键词搜索"),
    sort_by: str = Query(default="added_at", description="排序字段"),
    order: str = Query(default="desc", description="排序方向: asc / desc"),
    user_id: str = Depends(get_current_user),
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
    allowed_sort_fields = {
        "added_at",
        "title",
        "source",
        "ai_relevance_score",
        "reportability_score",
    }
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
        await _attach_user_drafts(db, art, user_id)
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
async def get_article(
    url_hash: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """获取单篇文章完整详情（含原文 Markdown 全文）"""
    db = _get_db(request)

    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    await _attach_user_drafts(db, article, user_id)
    article["_id"] = str(article["_id"])
    total_score = article.get("ai_relevance_score", 0) + article.get("reportability_score", 0)
    article["total_score"] = total_score
    article["is_high_value"] = total_score >= 140

    return article


@router.delete("/articles/batch", summary="批量删除文章")
async def batch_delete_articles(
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """批量删除文章。"""
    body = await request.json()
    url_hashes = body.get("url_hashes", [])
    if not url_hashes:
        raise HTTPException(status_code=400, detail="url_hashes is required")
    db = _get_db(request)
    result = await db["articles"].delete_many({"url_hash": {"$in": url_hashes}})
    return {"ok": True, "deleted": result.deleted_count}


@router.delete("/articles/{url_hash}", summary="删除文章")
async def delete_article(
    url_hash: str,
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """删除指定文章。"""
    db = _get_db(request)
    result = await db["articles"].delete_one({"url_hash": url_hash})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"ok": True, "deleted": result.deleted_count}


@router.post("/articles/{url_hash}/fetch-content", summary="获取文章原文")
async def fetch_article_content(
    url_hash: str,
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """抓取文章原文并保存（支持公众号和海外新闻）。"""
    import httpx as _httpx

    db = _get_db(request)
    article = await db["articles"].find_one({"url_hash": url_hash})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    url = article.get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="Article has no URL")

    source_type = article.get("source_type", "")
    content = ""

    try:
        if source_type == "overseas_news":
            # 海外新闻：用 httpx + BeautifulSoup 抓取
            from api.overseas_crawl import _fetch_fulltext

            context = RequestContext.create(
                request_id=getattr(request.state, "request_id", None),
                trace_id=get_trace_id() or getattr(request.state, "request_id", None),
                initiator_user_id=_user_id,
            )
            content = await _fetch_fulltext(
                url,
                request.app.state.mcp_crawl_client,
                context,
            )
        else:
            # 公众号：调用 mcp-wewe 抓取全文
            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.post("http://mcp-wewe:8100/fetch-article", json={"link": url})
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict):
                content = (
                    data.get("text", "")
                    or data.get("content_md", "")
                    or data.get("content", "")
                    or data.get("fulltext", "")
                )
                if not content and "result" in data:
                    content = (
                        data["result"].get("content", "")
                        if isinstance(data["result"], dict)
                        else data["result"]
                    )
            if not content:
                # fallback: 直接 requests 抓取
                import requests as _req
                from bs4 import BeautifulSoup

                r = _req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                soup = BeautifulSoup(r.text, "html.parser")
                el = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
                content = el.get_text() if el else r.text[:5000]

        if not content:
            raise HTTPException(status_code=502, detail="抓取原文失败：内容为空")

        # 保存到 DB
        await db["articles"].update_one(
            {"url_hash": url_hash},
            {"$set": {"content_md": content[:50000]}},
        )
        return {"ok": True, "content": content[:5000]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/articles/batch-fetch-content", summary="批量补抓原文")
async def batch_fetch_content(
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """批量抓取 content_md 为空的文章全文，支持海外新闻和公众号。"""
    db = _get_db(request)
    cursor = (
        db["articles"]
        .find(
            {"$or": [{"content_md": ""}, {"content_md": {"$exists": False}}]},
            {"url_hash": 1, "url": 1, "title": 1, "source": 1, "source_type": 1},
        )
        .limit(50)
    )
    articles = await cursor.to_list(length=50)

    if not articles:
        return {"ok": True, "data": {"message": "没有需要补抓的文章", "updated": 0}}

    # 按类型分组
    overseas = [a for a in articles if a.get("source_type") == "overseas_news" and a.get("url")]
    wechat = [a for a in articles if a.get("source_type") != "overseas_news" and a.get("url")]
    updated = 0

    # 海外新闻：调用 mcp-crawl 批量抓取
    if overseas:
        try:
            urls = [a["url"] for a in overseas]
            context = RequestContext.create(
                request_id=getattr(request.state, "request_id", None),
                trace_id=get_trace_id() or getattr(request.state, "request_id", None),
                initiator_user_id=_user_id,
            )
            data = await request.app.state.mcp_crawl_client.fetch_fulltext_batch(urls, context)
            for art in overseas:
                content = data.get(art["url"], "")
                if content:
                    await db["articles"].update_one(
                        {"url_hash": art["url_hash"]},
                        {"$set": {"content_md": content[:50000]}},
                    )
                    updated += 1
                    logger.info(
                        "Batch fetch: %s -> %d chars",
                        art.get("title", "")[:40],
                        len(content),
                    )
        except Exception as e:
            logger.warning("Overseas batch fetch failed: %s", e)

    # 公众号：逐篇调用 mcp-wewe
    for art in wechat:
        url = art.get("url", "")
        try:
            import httpx as _httpx2

            async with _httpx2.AsyncClient(timeout=30) as client:
                resp = await client.post("http://mcp-wewe:8100/fetch-article", json={"link": url})
                resp.raise_for_status()
                data = resp.json()
            content = ""
            if isinstance(data, dict):
                content = (
                    data.get("text", "") or data.get("content_md", "") or data.get("content", "")
                )
            if content:
                await db["articles"].update_one(
                    {"url_hash": art["url_hash"]},
                    {"$set": {"content_md": content[:50000]}},
                )
                updated += 1
        except Exception:
            pass

    return {"ok": True, "data": {"total": len(articles), "updated": updated}}


@router.post("/articles/{url_hash}/summarize", summary="生成摘要")
async def summarize_article(
    url_hash: str,
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """用 DeepSeek 生成 150 字中文摘要并保存。"""
    db = _get_db(request)
    article = await db["articles"].find_one({"url_hash": url_hash})
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    content = article.get("content_md", "") or article.get("summary", "")
    if len(content) < 50:
        raise HTTPException(status_code=400, detail="Content too short, fetch original first")

    try:
        from openai import OpenAI

        settings = get_settings()
        client = OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "user",
                    "content": f"请用150个汉字以内的篇幅总结以下文章的核心内容：\n\n{content[:4000]}",
                }
            ],
            max_tokens=300,
            temperature=0.3,
        )
        summary = resp.choices[0].message.content.strip()
        await db["articles"].update_one(
            {"url_hash": url_hash},
            {"$set": {"summary_cn": summary}},
        )
        return {"ok": True, "summary_cn": summary}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/stats", summary="统计概览")
async def get_stats(
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """返回仪表盘统计数据：总数、分类分布、来源分布、评分分布"""
    db = _get_db(request)

    total = await db["articles"].count_documents({})
    ai_security_count = await db["articles"].count_documents({"is_ai_security": True})
    high_value_count = (
        await db["articles"].count_documents(
            {
                "$expr": {
                    "$gte": [
                        {"$add": ["$ai_relevance_score", "$reportability_score"]},
                        140,
                    ]
                }
            }
        )
        if total > 0
        else 0
    )

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
