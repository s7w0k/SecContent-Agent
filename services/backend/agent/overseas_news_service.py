"""阶段十六 16.4：统一海外新闻入库服务。

从 API 路由中抽取的领域服务，手动抓取和定时抓取共用。
不依赖 FastAPI Request/Depends/HTTPException，API 和 Worker 均可调用。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("backend.overseas_news_service")


class CrawlServiceError(Exception):
    """领域异常基类。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class OverseasNewsIngestionService:
    """统一海外新闻抓取与入库服务。"""

    def __init__(self, *, db, tools, arq_pool=None):
        self.db = db
        self.tools = tools
        self.arq_pool = arq_pool

    async def run(
        self,
        *,
        crawl_days: int,
        trigger: str,
        actor_id: str,
        trace_id: str,
        request_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """执行抓取 -> 规范化 -> 原子 upsert -> 提交全文任务。

        Returns:
            结构化结果，包含 ok, run_id, status, total, saved, duplicates,
            invalid, fulltext_queued, per_site, errors
        """
        # 1. 调用 mcp-crawl
        request_context = {
            "request_id": request_id,
            "trace_id": trace_id,
            "initiator_user_id": actor_id,
        }

        try:
            result = await self.tools["crawl_overseas_news"].ainvoke(
                {
                    "payload": {
                        "days": crawl_days,
                        "_request_context": request_context,
                    }
                }
            )
        except Exception as exc:
            logger.error("mcp-crawl call failed: %s", exc)
            raise CrawlServiceError(
                "CRAWL_SERVICE_UNAVAILABLE",
                f"mcp-crawl 不可达: {exc}",
                retryable=True,
            ) from exc

        # 2. 解析响应
        data: dict[str, Any] = {}
        articles: list[dict] = []
        if result.get("ok") and result.get("data"):
            data = result["data"]
            if isinstance(data, dict):
                articles = data.get("articles", [])
            elif isinstance(data, list):
                articles = data

        if not articles and result.get("ok") is False:
            raise CrawlServiceError(
                "CRAWL_RESPONSE_INVALID",
                "mcp-crawl 返回失败且无文章",
                retryable=False,
            )

        # 3. 规范化 + 原子 upsert
        saved = 0
        duplicates = 0
        invalid = 0
        returned_hashes: list[str] = []

        for art in articles:
            url = (art.get("url") or "").strip()
            if not url or not url.startswith(("http://", "https://")):
                invalid += 1
                continue

            url_hash = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()
            returned_hashes.append(url_hash)

            now = datetime.now(UTC)
            doc = {
                "url_hash": url_hash,
                "title": (art.get("title") or "")[:500],
                "url": url,
                "source": (art.get("source") or "")[:200],
                "source_type": "overseas_news",
                "published_at": art.get("published_at", ""),
                "added_at": now,
                "summary": (art.get("summary") or "")[:2000],
                "content_md": "",
                "pipeline_status": "crawled",
                "content_fetch_status": "pending",
                "content_fetch_error": None,
                "crawl_run_id": run_id,
                "crawl_trigger": trigger,
            }

            try:
                res = await self.db["articles"].update_one(
                    {"url_hash": url_hash},
                    {"$setOnInsert": doc},
                    upsert=True,
                )
                if res.upserted_id is not None:
                    saved += 1
                else:
                    duplicates += 1
            except Exception as exc:
                logger.warning("upsert failed for url_hash=%s: %s", url_hash, exc)
                duplicates += 1

        # 4. 查询待补全文文章（恢复安全逻辑）
        pending_articles: list[dict] = []
        if returned_hashes:
            cursor = self.db["articles"].find(
                {
                    "url_hash": {"$in": returned_hashes},
                    "$or": [
                        {"content_md": ""},
                        {"content_md": {"$exists": False}},
                    ],
                    "content_fetch_status": {"$in": [None, "pending", "failed"]},
                }
            )
            async for doc in cursor:
                pending_articles.append(
                    {
                        "url_hash": doc["url_hash"],
                        "url": doc.get("url", ""),
                    }
                )

        # 5. 提交全文任务
        fulltext_queued = 0
        fulltext_status = "not_required"
        if pending_articles:
            if self.arq_pool is not None:
                try:
                    await self.arq_pool.enqueue_job(
                        "fetch_fulltext_batch",
                        pending_articles,
                        trace_id=trace_id,
                        user_id=actor_id,
                        request_id=request_id,
                        _job_id=f"fulltext:{run_id}",
                    )
                    fulltext_queued = len(pending_articles)
                    fulltext_status = "queued"
                except Exception as exc:
                    logger.error("enqueue fulltext failed: %s", exc)
                    fulltext_status = "failed"
            else:
                logger.warning("arq_pool not available, skipping fulltext")
                fulltext_status = "failed"

        # 6. 确定状态
        errors = data.get("errors", {}) if isinstance(data, dict) else {}
        per_site = data.get("per_site", {}) if isinstance(data, dict) else {}
        status = "completed" if not errors else "partial"

        return {
            "ok": True,
            "run_id": run_id,
            "status": status,
            "total": len(articles),
            "saved": saved,
            "duplicates": duplicates,
            "invalid": invalid,
            "fulltext_queued": fulltext_queued,
            "fulltext_status": fulltext_status,
            "per_site": per_site,
            "errors": errors,
        }
