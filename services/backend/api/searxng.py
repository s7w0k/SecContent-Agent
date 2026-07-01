"""SearXNG self-hosted search API."""

import hashlib, os, logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api/searxng", tags=["SearXNG"])
logger = logging.getLogger("backend.api.searxng")

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080")


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(description="Search query"),
    limit: int = Query(default=20, le=50),
):
    """Search via SearXNG JSON API and save results to DB."""
    import httpx
    db = getattr(request.app.state, "db", None)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params={"q": q, "format": "json", "categories": "news"},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])

        tz = timezone(timedelta(hours=8))
        saved = 0
        for r in results:
            url = r.get("url", "")
            if not url:
                continue
            url_hash = hashlib.md5(url.encode()).hexdigest()
            if db:
                existing = await db["articles"].find_one({"url_hash": url_hash})
                if existing:
                    continue
                await db["articles"].insert_one({
                    "url_hash": url_hash,
                    "title": r.get("title", ""),
                    "url": url,
                    "source": r.get("source", r.get("engine", "SearXNG")),
                    "source_type": "overseas_news",
                    "published_at": r.get("publishedDate", "") or datetime.now(tz).strftime("%Y-%m-%d"),
                    "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "summary": r.get("content", "")[:500],
                    "content_md": "",
                    "pipeline_status": "crawled",
                })
                saved += 1

        return {"ok": True, "total": len(results), "saved": saved, "results": results}

    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
