"""
仪表盘数据 REST API — 文章列表、统计、详情

端点:
  GET /api/articles            文章列表（分页+筛选+排序）
  GET /api/articles/hot        热点文章排行
  GET /api/articles/{hash}     单篇文章详情
  GET /api/stats               统计概览
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

from agent.template_compat import normalize_legacy_drafts
from auth.deps import get_current_user
from clients.mcp_crawl import RequestContext
from config import get_settings
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from logging_config import get_trace_id

router = APIRouter(prefix="/api", tags=["Dashboard"])
logger = logging.getLogger("backend.api.dashboard")

DateRange = Literal["1d", "7d", "30d", "all"]


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_db(request: Request):
    """从 app.state 获取 MongoDB 数据库实例"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _today_start_utc(now: datetime | None = None) -> datetime:
    """返回 UTC+8 当日零点对应的 UTC 时间。"""
    utc_now = now or datetime.now(UTC)
    if utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=UTC)
    utc8_now = utc_now.astimezone(timezone(timedelta(hours=8)))
    return utc8_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _date_range_start(date_range: DateRange, now: datetime | None = None) -> datetime | None:
    """将热点排行时间范围转换为 UTC 起始时间。"""
    utc_now = now or datetime.now(UTC)
    if utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=UTC)
    utc_now = utc_now.astimezone(UTC)
    if date_range == "1d":
        return _today_start_utc(utc_now)
    if date_range == "7d":
        return utc_now - timedelta(days=7)
    if date_range == "30d":
        return utc_now - timedelta(days=30)
    return None


async def _attach_user_drafts(db, article: dict, user_id: str) -> dict:
    """用当前用户的独立草稿替换文章中的旧共享草稿。"""
    user_draft = await db["user_drafts"].find_one(
        {"user_id": user_id, "article_url_hash": article["url_hash"]}
    )
    drafts = normalize_legacy_drafts(user_draft.get("drafts", [])) if user_draft else []
    article.pop("pr_drafts", None)
    article["pr_drafts"] = drafts
    article["can_generate"] = not bool(drafts)
    article["draft_created_at"] = user_draft.get("created_at") if user_draft else None
    article["draft_updated_at"] = user_draft.get("updated_at") if user_draft else None
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
        default=None, description="来源类型: overseas_news / wechat_mp / paper / user_upload"
    ),
    category: str | None = Query(default=None, description="分类筛选"),
    min_score: int | None = Query(default=None, ge=0, le=200, description="最低综合分"),
    is_ai_security: bool | None = Query(default=None, description="是否AI安全相关"),
    is_high_value: bool | None = Query(default=None, description="是否高分文章(≥140)"),
    has_drafts: bool | None = Query(default=None, description="是否已生成初稿"),
    keyword: str | None = Query(default=None, description="标题/摘要关键词搜索"),
    draft_date_from: str | None = Query(default=None, description="初稿生成日期起 (YYYY-MM-DD)"),
    draft_date_to: str | None = Query(default=None, description="初稿生成日期止 (YYYY-MM-DD)"),
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
        query["category_v2"] = category
    if is_ai_security is not None:
        query["is_ai_security"] = is_ai_security

    # 按是否已生成初稿筛选（仅在已打分且达到初稿生成阈值的文章中筛选）
    if has_drafts is not None or draft_date_from or draft_date_to:
        draft_query: dict = {"user_id": user_id, "drafts.0": {"$exists": True}}
        if draft_date_from:
            try:
                from_dt = datetime.strptime(draft_date_from, "%Y-%m-%d").replace(
                    hour=0, minute=0, second=0, tzinfo=UTC
                )
                draft_query.setdefault("created_at", {})["$gte"] = from_dt
            except ValueError:
                pass
        if draft_date_to:
            try:
                to_dt = datetime.strptime(draft_date_to, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=UTC
                )
                draft_query.setdefault("created_at", {})["$lte"] = to_dt
            except ValueError:
                pass
        draft_hashes = await db["user_drafts"].distinct(
            "article_url_hash",
            draft_query,
        )
        if has_drafts is False:
            # 未生成初稿：排除已有初稿的，且只看达到初稿生成阈值的已打分文章
            query["url_hash"] = {"$nin": draft_hashes}
            query["pr_total_score"] = {"$gte": 80}
        else:
            query["url_hash"] = {"$in": draft_hashes}

    # 排序
    sort_order = -1 if order == "desc" else 1
    allowed_sort_fields = {
        "added_at",
        "title",
        "source",
        "ai_relevance_score",
        "reportability_score",
        "pr_total_score",
    }
    if sort_by not in allowed_sort_fields:
        sort_by = "added_at"

    # 查询总数
    total = await db["articles"].count_documents(query)

    # 分页查询
    skip = (page - 1) * page_size
    cursor = db["articles"].find(query).sort(sort_by, sort_order).skip(skip).limit(page_size)
    articles = await cursor.to_list(length=page_size)

    # 批量查询当前用户的评分
    url_hashes = [art.get("url_hash") for art in articles if art.get("url_hash")]
    user_scores_map: dict[str, dict] = {}
    if url_hashes:
        score_docs = (
            await db["user_article_scores"]
            .find(
                {
                    "user_id": user_id,
                    "url_hash": {"$in": url_hashes},
                }
            )
            .to_list(length=len(url_hashes))
        )
        user_scores_map = {d["url_hash"]: d for d in score_docs}

    # 后过滤（MongoDB 不支持动态计算字段筛选）
    items = []
    for art in articles:
        await _attach_user_drafts(db, art, user_id)
        art["_id"] = str(art["_id"])

        # 合并用户级评分（覆盖文章上的全局评分）
        uh = art.get("url_hash")
        if uh and uh in user_scores_map:
            us = user_scores_map[uh]
            art["product_relevance"] = us.get("product_relevance")
            art["event_impact"] = us.get("event_impact")
            art["pr_total_score"] = us.get("pr_total_score")
            art["product_scores"] = us.get("product_scores", [])
            art["score_reason"] = us.get("score_reason", "")

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


@router.get("/articles/hot", summary="热点文章排行")
async def hot_articles(
    request: Request,
    limit: int = Query(default=10, ge=1, le=20, description="返回条数"),
    category: str = Query(default="all", description="category_v2 分类，all 表示全部"),
    date_range: DateRange = Query(default="7d", description="1d / 7d / 30d / all"),
    _user_id: str = Depends(get_current_user),
):
    """返回指定分类和时间范围内按综合分降序排列的热点文章。"""
    db = _get_db(request)

    # 历史文章可能只有双维度评分，added_at 也可能是 ISO 字符串。
    # 在数据库侧统一出兼容字段，避免因数据版本不同导致排行为空。
    pipeline: list[dict] = []
    if category != "all":
        pipeline.append({"$match": {"category_v2": category}})
    pipeline.append(
        {
            "$set": {
                "_hot_score": {
                    "$cond": [
                        {"$gt": [{"$ifNull": ["$pr_total_score", 0]}, 0]},
                        {"$ifNull": ["$pr_total_score", 0]},
                        {
                            "$add": [
                                {"$ifNull": ["$ai_relevance_score", 0]},
                                {"$ifNull": ["$reportability_score", 0]},
                            ]
                        },
                    ]
                },
                "_hot_added_at": {
                    "$convert": {
                        "input": "$added_at",
                        "to": "date",
                        "onError": None,
                        "onNull": None,
                    }
                },
            }
        }
    )

    match: dict = {"_hot_score": {"$gt": 0}}
    since = _date_range_start(date_range)
    if since is not None:
        match["_hot_added_at"] = {"$gte": since}
    pipeline.extend(
        [
            {"$match": match},
            # 分数相同时按入库时间和文章哈希固定顺序，保证分页/刷新结果稳定。
            {"$sort": {"_hot_score": -1, "_hot_added_at": -1, "url_hash": 1}},
            {"$limit": limit},
            {
                "$project": {
                    "_id": 0,
                    "url_hash": 1,
                    "title": 1,
                    "url": 1,
                    "pr_total_score": "$_hot_score",
                    "category_v2": 1,
                    "added_at": "$_hot_added_at",
                    "source_type": 1,
                }
            },
        ]
    )
    cursor = db["articles"].aggregate(pipeline)
    items = await cursor.to_list(length=limit)

    return {"ok": True, "data": {"items": items, "total": len(items)}}


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


@router.delete("/articles/irrelevant", summary="删除所有不相关文章")
async def delete_irrelevant_articles(
    request: Request,
    _user_id: str = Depends(get_current_user),
):
    """删除所有 category_v2 为「不相关」的文章。"""
    db = _get_db(request)
    result = await db["articles"].delete_many({"category_v2": "不相关"})
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
        if source_type == "user_upload":
            content = article.get("content_md", "")
            if not content:
                raise HTTPException(status_code=409, detail="用户上传文章没有可补抓的远程地址")
        elif source_type == "overseas_news":
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
        elif source_type == "wechat_mp":
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
        elif source_type == "web_search":
            # Web search: fetch via httpx with safety checks
            from utils.url_safety import is_safe_url

            if not is_safe_url(url):
                raise HTTPException(status_code=422, detail="URL 不安全，无法抓取")

            async with _httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": "PR-Agent-Fetch/1.0"},
            ) as client:
                resp = await client.get(url)
                content_type = resp.headers.get("content-type", "")
                if not any(
                    t in content_type for t in ("text/html", "text/plain", "application/xhtml")
                ):
                    raise HTTPException(status_code=422, detail=f"不支持的内容类型: {content_type}")

                from bs4 import BeautifulSoup

                soup = BeautifulSoup(resp.text, "html.parser")
                # Remove script and style elements
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                content = soup.get_text(separator="\n", strip=True)
        else:
            raise HTTPException(status_code=422, detail=f"不支持补抓来源类型: {source_type}")

        if not content:
            raise HTTPException(status_code=502, detail="抓取原文失败：内容为空")

        # 保存到 DB
        await db["articles"].update_one(
            {"url_hash": url_hash},
            {
                "$set": {
                    "content_md": content[:50000],
                    "content_fetch_status": "completed",
                    "content_fetch_error": None,
                }
            },
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
    wechat = [a for a in articles if a.get("source_type") == "wechat_mp" and a.get("url")]
    web_search = [a for a in articles if a.get("source_type") == "web_search" and a.get("url")]
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

    # Web search articles: fetch directly with safety checks
    import httpx as _httpx_ws
    from utils.url_safety import is_safe_url

    for article in web_search:
        try:
            url = article.get("url", "")
            if not url or not is_safe_url(url):
                await db["articles"].update_one(
                    {"url_hash": article["url_hash"]},
                    {
                        "$set": {
                            "content_fetch_status": "blocked",
                            "content_fetch_error": "URL不安全",
                        }
                    },
                )
                continue

            async with _httpx_ws.AsyncClient(
                timeout=30, follow_redirects=True, max_redirects=5
            ) as client:
                resp = await client.get(url, headers={"User-Agent": "PR-Agent-Fetch/1.0"})
                content_type = resp.headers.get("content-type", "")
                if not any(
                    t in content_type for t in ("text/html", "text/plain", "application/xhtml")
                ):
                    await db["articles"].update_one(
                        {"url_hash": article["url_hash"]},
                        {
                            "$set": {
                                "content_fetch_status": "blocked",
                                "content_fetch_error": f"不支持的内容类型: {content_type}",
                            }
                        },
                    )
                    continue

                from bs4 import BeautifulSoup

                soup = BeautifulSoup(resp.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                content = soup.get_text(separator="\n", strip=True)

            await db["articles"].update_one(
                {"url_hash": article["url_hash"]},
                {
                    "$set": {
                        "content_md": content[:50000],
                        "content_fetch_status": "completed",
                        "content_fetch_error": None,
                    }
                },
            )
            updated += 1
        except Exception as e:
            await db["articles"].update_one(
                {"url_hash": article["url_hash"]},
                {
                    "$set": {
                        "content_fetch_status": "failed",
                        "content_fetch_error": str(e)[:200],
                    }
                },
            )

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
    """返回仪表盘统计数据，包括以 UTC+8 为基准的今日统计。"""
    db = _get_db(request)

    total = await db["articles"].count_documents({})
    ai_security_count = await db["articles"].count_documents(
        {"is_ai_agent_security_relevant": True}
    )
    high_value_count = (
        await db["articles"].count_documents(
            {
                "$or": [
                    {
                        "$expr": {
                            "$gte": [
                                {"$add": ["$ai_relevance_score", "$reportability_score"]},
                                140,
                            ]
                        }
                    },
                    {"pr_total_score": {"$gte": 80}},
                ]
            }
        )
        if total > 0
        else 0
    )

    today_filter = {"added_at": {"$gte": _today_start_utc()}}
    today_count = await db["articles"].count_documents(today_filter)
    today_ai_security_count = await db["articles"].count_documents(
        {**today_filter, "is_ai_agent_security_relevant": True}
    )
    today_high_value_count = await db["articles"].count_documents(
        {
            **today_filter,
            "$or": [
                {
                    "$expr": {
                        "$gte": [
                            {"$add": ["$ai_relevance_score", "$reportability_score"]},
                            140,
                        ]
                    }
                },
                {"pr_total_score": {"$gte": 80}},
            ],
        }
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
        {"$match": {"category_v2": {"$nin": ["", None]}}},
        {"$group": {"_id": "$category_v2", "count": {"$sum": 1}}},
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
        "today_count": today_count,
        "today_ai_security_count": today_ai_security_count,
        "today_high_value_count": today_high_value_count,
    }
