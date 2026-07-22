"""Overseas security news API - host script imports articles via /api/overseas/import."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta, timezone

import httpx
from clients.mcp_crawl import McpCrawlClient, RequestContext
from fastapi import APIRouter, Query, Request
from logging_config import get_trace_id
from pydantic import BaseModel

router = APIRouter(prefix="/api/overseas", tags=["OverseasCrawl"])
logger = logging.getLogger("backend.api.overseas")

RSS_FEEDS = [
    {
        "name": "The Hacker News",
        "url": "https://feeds.feedburner.com/TheHackersNews",
        "domain": "thehackernews.com",
    },
    {
        "name": "BleepingComputer",
        "url": "https://www.bleepingcomputer.com/feed/",
        "domain": "bleepingcomputer.com",
    },
    {
        "name": "SecurityWeek",
        "url": "https://www.securityweek.com/feed/",
        "domain": "securityweek.com",
    },
    {
        "name": "Help Net Security",
        "url": "https://www.helpnetsecurity.com/feed/",
        "domain": "helpnetsecurity.com",
    },
]


def _crawler_context(request: Request) -> RequestContext:
    state = getattr(request, "state", None)
    request_id = getattr(state, "request_id", None)
    return RequestContext.create(
        request_id=request_id,
        trace_id=get_trace_id() or request_id,
        initiator_user_id=getattr(state, "user_id", None),
    )


def _crawler_client(request: Request) -> McpCrawlClient:
    return request.app.state.mcp_crawl_client


async def _fetch_fulltext(
    url: str,
    client: McpCrawlClient,
    context: RequestContext,
) -> str:
    """通过 mcp-crawl 服务抓取文章全文（使用 curl_cffi 浏览器指纹模拟）"""
    try:
        return await client.fetch_fulltext(url, context)
    except Exception as e:
        logger.warning("fetch_fulltext error: %s", e)
        return ""


class ArticleImport(BaseModel):
    title: str = ""
    url: str = ""
    source: str = ""
    source_type: str = "overseas_news"
    published_at: str = ""
    summary: str = ""
    content_md: str = ""


@router.post("/import")
async def import_article(art: ArticleImport, request: Request):
    """Import article from host crawler script."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"ok": False, "error": "DB not available"}
    url_hash = hashlib.md5(art.url.encode()).hexdigest()
    existing = await db["articles"].find_one({"url_hash": url_hash})
    if existing:
        return {"ok": True, "saved": False, "reason": "duplicate"}
    # 如果未提供 content_md，尝试抓取全文
    content_md = art.content_md
    if not content_md and art.url:
        content_md = await _fetch_fulltext(
            art.url,
            _crawler_client(request),
            _crawler_context(request),
        )
    await db["articles"].insert_one(
        {
            "url_hash": url_hash,
            "title": art.title,
            "url": art.url,
            "source": art.source,
            "source_type": art.source_type,
            "published_at": art.published_at,
            "summary": art.summary,
            "added_at": datetime.now(UTC),
            "content_md": content_md,
            "pipeline_status": "crawled",
        }
    )
    return {"ok": True, "saved": True}


@router.get("/crawl")
async def crawl_overseas(request: Request, hours: int = Query(default=24, le=72)):
    """Crawl RSS feeds from Docker. Use host script if blocked."""
    import feedparser

    db = getattr(request.app.state, "db", None)
    tz_cst = timezone(timedelta(hours=8))
    now_utc = datetime.now(UTC)
    cutoff = now_utc - timedelta(hours=hours)
    all_articles = []

    async with httpx.AsyncClient(timeout=15) as client:
        for cfg in RSS_FEEDS:
            try:
                r = await client.get(cfg["url"])
                if r.status_code != 200:
                    logger.warning(f"{cfg['name']}: HTTP {r.status_code} (use host script instead)")
                    continue
                feed = feedparser.parse(r.content)
                if not feed.entries:
                    logger.warning(f"{cfg['name']}: 0 entries")
                    continue
                count = 0
                for e in feed.entries:
                    pub = None
                    for f in ("published_parsed", "updated_parsed"):
                        v = e.get(f)
                        if v and hasattr(v, "tm_year"):
                            from calendar import timegm

                            pub = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=timegm(v))
                            break
                    if pub and cutoff <= pub <= now_utc:
                        all_articles.append(
                            {
                                "title": e.get("title", ""),
                                "url": e.get("link", ""),
                                "source": cfg["name"],
                                "source_type": "overseas_news",
                                "published_at": pub.strftime("%Y-%m-%d %H:%M"),
                                "summary": (e.get("summary", "") or e.get("description", "") or "")[
                                    :500
                                ],
                            }
                        )
                        count += 1
                logger.info(f"{cfg['name']}: {count} articles")
            except Exception as e:
                logger.warning(f"{cfg['name']}: {e}")

    saved, skipped = 0, 0
    new_articles: list[dict] = []
    for art in all_articles:
        url_hash = hashlib.md5(art["url"].encode()).hexdigest()
        if db is not None:
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                skipped += 1
                continue
            await db["articles"].insert_one(
                {
                    "url_hash": url_hash,
                    "title": art["title"],
                    "url": art["url"],
                    "source": art["source"],
                    "source_type": "overseas_news",
                    "published_at": art["published_at"],
                    "added_at": datetime.now(UTC),
                    "summary": art["summary"],
                    "content_md": "",
                    "pipeline_status": "crawled",
                }
            )
            new_articles.append({"url_hash": url_hash, "url": art["url"]})
            saved += 1

    # 异步批量抓取全文（不阻塞响应）
    if new_articles:
        import asyncio

        _task = asyncio.create_task(
            _batch_fetch_fulltext(
                db,
                new_articles,
                _crawler_client(request),
                _crawler_context(request),
            )
        )
        _fulltext_tasks.add(_task)
        _task.add_done_callback(_fulltext_tasks.discard)

    return {"ok": True, "total": len(all_articles), "saved": saved, "skipped": skipped}


# 任务引用集合（防止 GC）
_fulltext_tasks: set = set()


async def _batch_fetch_fulltext(
    db,
    articles: list[dict],
    client: McpCrawlClient,
    context: RequestContext,
) -> None:
    """调用 mcp-crawl 批量抓取全文并更新数据库"""
    try:
        urls = [a["url"] for a in articles]
        data = await client.fetch_fulltext_batch(urls, context)
        updated = 0
        for art in articles:
            content = data.get(art["url"], "")
            if content:
                await db["articles"].update_one(
                    {"url_hash": art["url_hash"]},
                    {"$set": {"content_md": content[:50000]}},
                )
                updated += 1
        logger.info("Batch fulltext: %d/%d updated", updated, len(articles))
    except Exception as e:
        logger.warning("Batch fulltext failed: %s", e)
