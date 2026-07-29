"""Article ingestion service - URL normalization, dedup, and article creation for web search imports."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from utils.url_safety import canonicalize_url, compute_url_hash, extract_domain

logger = logging.getLogger("backend.ingestion")


class ArticleIngestionService:
    """Handles article creation with URL normalization and idempotent dedup."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self._db = db

    async def insert_or_get_existing(
        self,
        url: str,
        title: str,
        snippet: str = "",
        published_at: str | None = None,
        engines: list[str] | None = None,
        category: str = "general",
    ) -> dict[str, Any]:
        """Insert a new article or return existing one.

        Returns dict with:
            - status: "imported" or "duplicate"
            - article_url_hash: the url_hash
            - canonical_url: normalized URL
        """
        # Canonicalize URL
        canonical = canonicalize_url(url)
        url_hash = compute_url_hash(canonical)
        domain = extract_domain(canonical)

        # Check if already exists by url_hash
        existing = await self._db["articles"].find_one(
            {"url_hash": url_hash},
            {"url_hash": 1, "canonical_url": 1},
        )
        if existing:
            return {
                "status": "duplicate",
                "article_url_hash": url_hash,
                "canonical_url": canonical,
            }

        # Build article document
        now = datetime.now(UTC)
        article = {
            "url_hash": url_hash,
            "canonical_url": canonical,
            "url": url,
            "title": title[:500],
            "source": domain,
            "source_type": "web_search",
            "published_at": published_at,
            "added_at": now,
            "summary": snippet[:2000],
            "summary_cn": "",
            "content_md": "",
            "is_ai_security": False,
            "is_agent_security": False,
            "category": "",
            "category_v2": "",
            "category_v2_confidence": 0,
            "category_v2_reason": "",
            "category_v2_fallback": False,
            "is_pr_eligible": False,
            "product_relevance": 0,
            "event_impact": 0,
            "pr_total_score": 0,
            "ai_relevance_score": 0,
            "reportability_score": 0,
            "has_report": False,
            "report_id": None,
            "pipeline_status": "pending_enrichment",
            "content_fetch_status": "queued",
            "content_fetch_error": None,
            "search_provenance": {
                "engines": engines or [],
                "category": category,
            },
        }

        try:
            await self._db["articles"].insert_one(article)
            logger.info("Article imported: url_hash=%s domain=%s", url_hash, domain)
            return {
                "status": "imported",
                "article_url_hash": url_hash,
                "canonical_url": canonical,
            }
        except DuplicateKeyError:
            # Race condition - another request inserted the same URL
            return {
                "status": "duplicate",
                "article_url_hash": url_hash,
                "canonical_url": canonical,
            }

    async def create_import_batch(
        self,
        user_id: str,
        search_id: str,
        idempotency_key: str,
        result_ids: list[str],
    ) -> dict[str, Any] | None:
        """Create or get an import batch for idempotency.

        Returns:
            - Existing terminal batch (completed/partial/failed) -> return saved response
            - Existing processing batch -> return with status="processing"
            - New batch -> return {"batch_id": ..., "status": "new"} (caller should process)
        """
        now = datetime.now(UTC)
        audit_retention_days = 180  # Default, can be from settings

        # Try to find existing batch
        existing = await self._db["search_import_batches"].find_one(
            {"user_id": user_id, "idempotency_key": idempotency_key}
        )
        if existing:
            if existing.get("status") in ("completed", "partial", "failed"):
                return existing
            # Still processing
            return existing

        # Create new processing batch
        batch_id = f"simp_{now.strftime('%Y%m%d')}_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:8]}"
        batch = {
            "batch_id": batch_id,
            "user_id": user_id,
            "search_id": search_id,
            "idempotency_key": idempotency_key,
            "requested_result_ids": result_ids,
            "status": "processing",
            "summary": {"requested": len(result_ids), "imported": 0, "duplicate": 0, "failed": 0},
            "items": [],
            "created_at": now,
            "completed_at": None,
            "expires_at": now + timedelta(days=audit_retention_days),
        }
        try:
            await self._db["search_import_batches"].insert_one(batch)
            # Return minimal dict with batch_id so caller can proceed
            return {"batch_id": batch_id, "status": "new"}
        except DuplicateKeyError:
            # Race condition - return the existing one
            return await self._db["search_import_batches"].find_one(
                {"user_id": user_id, "idempotency_key": idempotency_key}
            )

    async def complete_import_batch(
        self,
        batch_id: str,
        user_id: str,
        summary: dict,
        items: list[dict],
        status: str = "completed",
    ) -> None:
        """Mark batch as complete with final response."""
        now = datetime.now(UTC)
        await self._db["search_import_batches"].update_one(
            {"batch_id": batch_id, "user_id": user_id},
            {
                "$set": {
                    "status": status,
                    "summary": summary,
                    "items": items,
                    "completed_at": now,
                }
            },
        )

    async def save_import_item(
        self,
        user_id: str,
        batch_id: str,
        search_id: str,
        result_id: str,
        canonical_url_hash: str,
        article_url_hash: str | None,
        status: str,
        error_code: str | None = None,
    ) -> None:
        """Save individual import item for audit."""
        now = datetime.now(UTC)
        item = {
            "import_item_id": f"sitem_{now.strftime('%Y%m%d%H%M%S')}_{hashlib.sha256(result_id.encode()).hexdigest()[:8]}",
            "batch_id": batch_id,
            "user_id": user_id,
            "search_id": search_id,
            "result_id": result_id,
            "canonical_url_hash": canonical_url_hash,
            "article_url_hash": article_url_hash,
            "status": status,
            "error_code": error_code,
            "created_at": now,
            "expires_at": now + timedelta(days=180),
        }
        with contextlib.suppress(DuplicateKeyError):
            await self._db["search_import_items"].insert_one(item)
