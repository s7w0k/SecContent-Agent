"""Web search API - SearXNG-backed search, session, import and status endpoints."""

from __future__ import annotations

import logging
import uuid

from auth.deps import get_current_user
from clients.searxng import SearXNGError
from config import get_settings
from fastapi import APIRouter, Depends, HTTPException, Request
from models.web_search import SearchImportRequest, WebSearchRequest

router = APIRouter(prefix="/api/search", tags=["Web Search"])
logger = logging.getLogger("backend.web_search")


def _get_search_deps(request: Request):
    """Return (settings, searxng_client, db) from app state."""
    settings = get_settings()
    client = getattr(request.app.state, "searxng_client", None)
    db = getattr(request.app.state, "db", None)
    return settings, client, db


def _searxng_error_response(exc: SearXNGError) -> HTTPException:
    """Map SearXNG exceptions to HTTP responses."""
    status_map = {
        "SEARCH_PROVIDER_UNAVAILABLE": 503,
        "SEARCH_PROVIDER_TIMEOUT": 504,
        "SEARCH_PROVIDER_BAD_RESPONSE": 502,
        "SEARCH_RATE_LIMITED": 429,
    }
    status = status_map.get(exc.code, 502)
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc)},
    )


def _get_services(db, settings):
    try:
        from services.article_ingestion import ArticleIngestionService
        from services.web_search_service import SearchSessionService
    except ImportError:
        from services.backend.services.article_ingestion import ArticleIngestionService
        from services.backend.services.web_search_service import SearchSessionService

    ingestion_svc = ArticleIngestionService(db)
    session_svc = SearchSessionService(db, ttl_minutes=settings.WEB_SEARCH_SESSION_TTL_MINUTES)
    return ingestion_svc, session_svc


# ── Status ──────────────────────────────────────────────


@router.get("/status", summary="搜索功能状态")
async def search_status(request: Request, user_id: str = Depends(get_current_user)):
    settings, client, _ = _get_search_deps(request)
    enabled = settings.WEB_SEARCH_ENABLED
    available = False
    if enabled and client is not None:
        available = await client.health_check()
    return {
        "ok": True,
        "data": {
            "enabled": enabled,
            "available": available,
            "allowed_categories": settings.WEB_SEARCH_ALLOWED_CATEGORIES.split(","),
            "allowed_languages": settings.WEB_SEARCH_ALLOWED_LANGUAGES.split(","),
            "max_import_items": settings.WEB_SEARCH_IMPORT_BATCH_LIMIT,
        },
    }


# ── Search Query ────────────────────────────────────────


@router.post("/query", summary="发起搜索")
async def search_query(
    request: Request,
    body: WebSearchRequest,
    user_id: str = Depends(get_current_user),
):
    settings, client, db = _get_search_deps(request)

    if not settings.WEB_SEARCH_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_DISABLED", "message": "搜索功能暂未开放"},
        )
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_PROVIDER_UNAVAILABLE", "message": "搜索服务暂不可用"},
        )
    if db is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "DB_UNAVAILABLE", "message": "数据库不可用"},
        )

    # 频率限制
    from utils.search_cache import check_rate_limit, get_cached_result, set_cached_result

    if not await check_rate_limit(user_id):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "SEARCH_RATE_LIMITED",
                "message": f"搜索过于频繁，每分钟限 {settings.WEB_SEARCH_RATE_LIMIT_PER_MINUTE} 次",
            },
        )

    # 尝试命中缓存
    cached = await get_cached_result(
        body.q, body.categories, body.language,
        body.time_range, body.safesearch, body.pageno,
    )
    if cached is not None:
        # 用缓存结果创建新会话（会话仍归属于当前用户）
        _, session_svc = _get_services(db, settings)
        allowed_cats = settings.WEB_SEARCH_ALLOWED_CATEGORIES.split(",")
        search_id = session_svc._generate_search_id()
        results = session_svc.normalize_results(
            cached.get("results", []), search_id, allowed_cats,
        )
        warnings = session_svc.build_warnings(cached)
        results = await session_svc.mark_imported_results(results)
        limit = settings.WEB_SEARCH_RESULT_LIMIT
        has_more = len(results) >= limit
        results = results[:limit]

        query_dict = {
            "q": body.q,
            "categories": body.categories,
            "language": body.language,
            "time_range": body.time_range,
            "safesearch": body.safesearch,
            "pageno": body.pageno,
        }
        session = await session_svc.create_session(
            user_id=user_id, query=query_dict, results=results, warnings=warnings,
        )
        await db["search_sessions"].update_one(
            {"search_id": session["search_id"]},
            {"$set": {"search_id": search_id, "results": results}},
        )
        expires_at = session["expires_at"]
        expires_str = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)
        return {
            "ok": True,
            "data": {
                "search_id": search_id,
                "query": query_dict,
                "results": results,
                "page": body.pageno,
                "has_more": has_more,
                "warnings": warnings,
                "expires_at": expires_str,
                "cached": True,
            },
        }

    try:
        raw = await client.search(
            q=body.q,
            categories=body.categories,
            language=body.language,
            time_range=body.time_range,
            safesearch=body.safesearch,
            pageno=body.pageno,
        )
    except SearXNGError as e:
        raise _searxng_error_response(e) from e

    # 写入缓存
    await set_cached_result(
        body.q, body.categories, body.language,
        body.time_range, body.safesearch, body.pageno, raw,
    )

    _, session_svc = _get_services(db, settings)
    allowed_cats = settings.WEB_SEARCH_ALLOWED_CATEGORIES.split(",")

    # Generate search_id first so result_ids are deterministic
    search_id = session_svc._generate_search_id()
    results = session_svc.normalize_results(
        raw.get("results", []), search_id, allowed_cats,
    )
    warnings = session_svc.build_warnings(raw)

    # Mark already imported
    results = await session_svc.mark_imported_results(results)

    # Limit results - if we got at least `limit` results, there may be more pages
    limit = settings.WEB_SEARCH_RESULT_LIMIT
    has_more = len(results) >= limit
    results = results[:limit]

    # Create session
    query_dict = {
        "q": body.q,
        "categories": body.categories,
        "language": body.language,
        "time_range": body.time_range,
        "safesearch": body.safesearch,
        "pageno": body.pageno,
    }
    session = await session_svc.create_session(
        user_id=user_id,
        query=query_dict,
        results=results,
        warnings=warnings,
    )

    # Update session with the pre-generated search_id
    await db["search_sessions"].update_one(
        {"search_id": session["search_id"]},
        {"$set": {"search_id": search_id, "results": results}},
    )
    session["search_id"] = search_id

    expires_at = session["expires_at"]
    expires_str = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)

    return {
        "ok": True,
        "data": {
            "search_id": search_id,
            "query": query_dict,
            "results": results,
            "page": body.pageno,
            "has_more": has_more,
            "warnings": warnings,
            "expires_at": expires_str,
            "cached": False,
        },
    }


# ── Get Session ─────────────────────────────────────────


@router.get("/sessions/{search_id}", summary="获取搜索会话")
async def get_session(
    search_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    settings, _, db = _get_search_deps(request)

    if db is None:
        raise HTTPException(status_code=503, detail={"code": "DB_UNAVAILABLE", "message": "数据库不可用"})

    _, session_svc = _get_services(db, settings)

    session = await session_svc.get_session(search_id, user_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SEARCH_SESSION_NOT_FOUND", "message": "搜索结果已过期，请重新搜索"},
        )

    # Re-check imported status
    results = session.get("results", [])
    results = await session_svc.mark_imported_results(results)

    expires_at = session["expires_at"]
    expires_str = expires_at.isoformat() if hasattr(expires_at, "isoformat") else str(expires_at)

    return {
        "ok": True,
        "data": {
            "search_id": session["search_id"],
            "query": session.get("query", {}),
            "results": results,
            "page": session.get("query", {}).get("pageno", 1),
            "has_more": len(results) >= settings.WEB_SEARCH_RESULT_LIMIT,
            "warnings": session.get("warnings", []),
            "expires_at": expires_str,
        },
    }


# ── Import ──────────────────────────────────────────────


@router.post("/import", summary="批量导入搜索结果")
async def import_results(
    request: Request,
    body: SearchImportRequest,
    user_id: str = Depends(get_current_user),
):
    """Import selected search results into the article library."""
    settings, _client, db = _get_search_deps(request)

    if not settings.WEB_SEARCH_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_DISABLED", "message": "搜索功能暂未开放"},
        )
    if db is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "DB_UNAVAILABLE", "message": "数据库不可用"},
        )

    idempotency_key = request.headers.get("Idempotency-Key", "")
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())

    if len(body.result_ids) > settings.WEB_SEARCH_IMPORT_BATCH_LIMIT:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "IMPORT_BATCH_TOO_LARGE",
                "message": f"单次最多导入 {settings.WEB_SEARCH_IMPORT_BATCH_LIMIT} 条",
            },
        )

    from utils.url_safety import is_safe_url

    ingestion_svc, session_svc = _get_services(db, settings)

    # Check/create idempotency batch
    existing_batch = await ingestion_svc.create_import_batch(
        user_id=user_id,
        search_id=body.search_id,
        idempotency_key=idempotency_key,
        result_ids=body.result_ids,
    )
    if existing_batch and existing_batch.get("status") in ("completed", "partial", "failed"):
        return {
            "ok": True,
            "data": {
                "batch_id": existing_batch.get("batch_id", ""),
                "summary": existing_batch.get("summary", {}),
                "items": existing_batch.get("items", []),
            },
        }
    if existing_batch and existing_batch.get("status") == "processing":
        raise HTTPException(
            status_code=409,
            detail={"code": "IMPORT_IN_PROGRESS", "message": "正在导入，请稍候"},
        )

    batch_id = existing_batch["batch_id"] if existing_batch else f"simp_{idempotency_key[:8]}"

    # Get search session
    session = await session_svc.get_session(body.search_id, user_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SEARCH_SESSION_NOT_FOUND", "message": "搜索结果已过期，请重新搜索"},
        )

    session_results = {r["result_id"]: r for r in session.get("results", [])}

    items = []
    imported_count = 0
    duplicate_count = 0
    failed_count = 0
    enrichment_queued = 0

    for result_id in body.result_ids:
        result = session_results.get(result_id)
        if result is None:
            items.append({
                "result_id": result_id,
                "status": "failed",
                "article_url_hash": None,
                "message": "结果不在当前搜索会话中",
            })
            failed_count += 1
            continue

        url = result.get("url", "")
        title = result.get("title", "")
        snippet = result.get("snippet", "")
        published_at = result.get("published_at")
        engines = result.get("engines", [])
        category = result.get("category", "general")

        if not is_safe_url(url):
            items.append({
                "result_id": result_id,
                "status": "invalid_url",
                "article_url_hash": None,
                "message": "URL 不安全或协议不允许",
            })
            failed_count += 1
            await ingestion_svc.save_import_item(
                user_id, batch_id, body.search_id, result_id,
                "", None, "invalid_url", "UNSAFE_URL",
            )
            continue

        try:
            result_data = await ingestion_svc.insert_or_get_existing(
                url=url, title=title, snippet=snippet,
                published_at=published_at, engines=engines, category=category,
            )
            status = result_data["status"]
            article_url_hash = result_data["article_url_hash"]

            if status == "imported":
                imported_count += 1
                enrichment_queued += 1
                message = "已加入文章库，正在后台获取全文"
            else:
                duplicate_count += 1
                message = "文章库中已存在"

            items.append({
                "result_id": result_id,
                "status": status,
                "article_url_hash": article_url_hash,
                "message": message,
            })

            await ingestion_svc.save_import_item(
                user_id, batch_id, body.search_id, result_id,
                result_data.get("canonical_url", ""),
                article_url_hash, status,
            )
            await session_svc.update_imported_status(
                body.search_id, user_id, result_id, article_url_hash,
            )

        except Exception as e:
            logger.error("Import failed for result %s: %s", result_id, e)
            items.append({
                "result_id": result_id,
                "status": "failed",
                "article_url_hash": None,
                "message": "导入失败",
            })
            failed_count += 1
            await ingestion_svc.save_import_item(
                user_id, batch_id, body.search_id, result_id,
                "", None, "failed", "INTERNAL_ERROR",
            )

    if failed_count == len(body.result_ids):
        batch_status = "failed"
    elif failed_count > 0:
        batch_status = "partial"
    else:
        batch_status = "completed"

    summary = {
        "requested": len(body.result_ids),
        "imported": imported_count,
        "duplicate": duplicate_count,
        "failed": failed_count,
        "enrichment_queued": enrichment_queued,
    }

    await ingestion_svc.complete_import_batch(
        batch_id, user_id, summary, items, batch_status,
    )

    # Enqueue async enrichment for newly imported articles
    if enrichment_queued > 0 and settings.WEB_SEARCH_ENRICH_ON_IMPORT:
        try:
            arq_pool = getattr(request.app.state, "arq_pool", None)
            if arq_pool is not None:
                new_hashes = [
                    item["article_url_hash"] for item in items
                    if item.get("status") == "imported" and item.get("article_url_hash")
                ]
                if new_hashes:
                    await arq_pool.enqueue_job(
                        "enrich_web_search_articles",
                        article_url_hashes=new_hashes,
                        user_id=user_id,
                    )
                    logger.info("Enqueued enrichment for %d articles", len(new_hashes))
        except Exception as e:
            logger.warning("Failed to enqueue enrichment: %s", e)

    return {
        "ok": True,
        "data": {
            "batch_id": batch_id,
            "summary": summary,
            "items": items,
        },
    }
