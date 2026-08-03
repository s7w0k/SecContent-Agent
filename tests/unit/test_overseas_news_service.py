"""阶段十六 16.4/16.5 测试：统一入库服务 + 定时任务。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class MockCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class MockCollection:
    def __init__(self, docs=None):
        self._docs = docs or []
        self._upsert_results = []

    def find(self, query=None):
        return MockCursor(self._docs)

    async def update_one(self, query, update, upsert=False):
        # 模拟原子 upsert：如果是新 url_hash 则返回 upserted_id
        url_hash = query.get("url_hash")
        existing = any(d.get("url_hash") == url_hash for d in self._docs)
        if not existing:
            doc = {"url_hash": url_hash, **update.get("$setOnInsert", {})}
            self._docs.append(doc)
            result = MagicMock()
            result.upserted_id = doc["_id"] = f"id_{url_hash}"
            result.modified_count = 0
            return result
        else:
            result = MagicMock()
            result.upserted_id = None
            result.modified_count = 1
            return result


class MockDb:
    def __init__(self, articles=None):
        self._collections = {"articles": MockCollection(articles or [])}

    def __getitem__(self, key):
        return self._collections.get(key, MockCollection([]))


class TestOverseasNewsIngestionService:
    """统一入库服务测试。"""

    @pytest.mark.asyncio
    async def test_run_new_articles(self):
        """新文章全部入库。"""
        from agent.overseas_news_service import OverseasNewsIngestionService

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {
                "articles": [
                    {"url": "https://example.com/1", "title": "新闻1", "source": "THN",
                     "summary": "摘要1", "published_at": "2026-08-03"},
                    {"url": "https://example.com/2", "title": "新闻2", "source": "THN",
                     "summary": "摘要2", "published_at": "2026-08-03"},
                ],
                "per_site": {"THN": 2},
                "errors": {},
            }
        })

        tools = {"crawl_overseas_news": mock_tool}
        db = MockDb([])
        service = OverseasNewsIngestionService(db=db, tools=tools)

        result = await service.run(
            crawl_days=1, trigger="manual", actor_id="user-1",
            trace_id="trace-1", request_id="req-1", run_id="run-1",
        )

        assert result["ok"] is True
        assert result["total"] == 2
        assert result["saved"] == 2
        assert result["duplicates"] == 0
        assert result["invalid"] == 0
        assert result["per_site"] == {"THN": 2}

    @pytest.mark.asyncio
    async def test_run_duplicate_articles(self):
        """已存在文章不重复入库。"""
        import hashlib

        from agent.overseas_news_service import OverseasNewsIngestionService

        url1 = "https://example.com/1"
        hash1 = hashlib.md5(url1.encode()).hexdigest()
        existing = [{"url_hash": hash1, "url": url1, "content_md": "已有正文"}]
        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {
                "articles": [
                    {"url": "https://example.com/1", "title": "新闻1"},
                    {"url": "https://example.com/2", "title": "新闻2"},
                ],
                "per_site": {}, "errors": {},
            }
        })

        tools = {"crawl_overseas_news": mock_tool}
        db = MockDb(existing)
        service = OverseasNewsIngestionService(db=db, tools=tools)

        result = await service.run(
            crawl_days=1, trigger="manual", actor_id="user-1",
            trace_id="t", request_id="r", run_id="run-1",
        )

        assert result["saved"] == 1
        assert result["duplicates"] == 1

    @pytest.mark.asyncio
    async def test_run_invalid_url(self):
        """无效 URL 计入 invalid。"""
        from agent.overseas_news_service import OverseasNewsIngestionService

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {
                "articles": [
                    {"url": "", "title": "无URL"},
                    {"url": "ftp://bad", "title": "非HTTP"},
                    {"url": "https://good.com/1", "title": "正常"},
                ],
                "per_site": {}, "errors": {},
            }
        })

        service = OverseasNewsIngestionService(
            db=MockDb([]), tools={"crawl_overseas_news": mock_tool},
        )
        result = await service.run(
            crawl_days=1, trigger="manual", actor_id="u",
            trace_id="t", request_id="r", run_id="run-1",
        )

        assert result["invalid"] == 2
        assert result["saved"] == 1

    @pytest.mark.asyncio
    async def test_run_partial_status(self):
        """部分站点错误时 status=partial。"""
        from agent.overseas_news_service import OverseasNewsIngestionService

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {
                "articles": [{"url": "https://ok.com/1", "title": "OK"}],
                "per_site": {"site-a": 1},
                "errors": {"site-b": "timeout"},
            }
        })

        service = OverseasNewsIngestionService(
            db=MockDb([]), tools={"crawl_overseas_news": mock_tool},
        )
        result = await service.run(
            crawl_days=1, trigger="manual", actor_id="u",
            trace_id="t", request_id="r", run_id="run-1",
        )

        assert result["status"] == "partial"
        assert "site-b" in result["errors"]

    @pytest.mark.asyncio
    async def test_run_service_unavailable(self):
        """mcp-crawl 不可达时抛出可重试异常。"""
        from agent.overseas_news_service import CrawlServiceError, OverseasNewsIngestionService

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(side_effect=ConnectionError("timeout"))

        service = OverseasNewsIngestionService(
            db=MockDb([]), tools={"crawl_overseas_news": mock_tool},
        )

        with pytest.raises(CrawlServiceError) as exc_info:
            await service.run(
                crawl_days=1, trigger="scheduled", actor_id="system:scheduler",
                trace_id="t", request_id="r", run_id="run-1",
            )

        assert exc_info.value.retryable is True
        assert exc_info.value.code == "CRAWL_SERVICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_run_empty_articles(self):
        """返回 0 篇时正常完成。"""
        from agent.overseas_news_service import OverseasNewsIngestionService

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value={
            "ok": True, "data": {"articles": [], "per_site": {}, "errors": {}}
        })

        service = OverseasNewsIngestionService(
            db=MockDb([]), tools={"crawl_overseas_news": mock_tool},
        )
        result = await service.run(
            crawl_days=1, trigger="scheduled", actor_id="system:scheduler",
            trace_id="t", request_id="r", run_id="run-1",
        )

        assert result["ok"] is True
        assert result["total"] == 0
        assert result["saved"] == 0
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_no_arq_pool(self):
        """无 ARQ pool 时 fulltext_status=failed。"""
        from agent.overseas_news_service import OverseasNewsIngestionService

        mock_tool = AsyncMock()
        mock_tool.ainvoke = AsyncMock(return_value={
            "ok": True,
            "data": {
                "articles": [{"url": "https://new.com/1", "title": "新"}],
                "per_site": {}, "errors": {},
            }
        })

        service = OverseasNewsIngestionService(
            db=MockDb([]), tools={"crawl_overseas_news": mock_tool},
            arq_pool=None,
        )
        result = await service.run(
            crawl_days=1, trigger="manual", actor_id="u",
            trace_id="t", request_id="r", run_id="run-1",
        )

        assert result["fulltext_status"] == "failed"
        assert result["fulltext_queued"] == 0


class TestBuildCronJobsWithScheduledTask:
    """build_cron_jobs 包含定时任务测试。"""

    def test_cron_jobs_has_scheduled_overseas(self):
        """启用时包含 scheduled_overseas_news_crawl。"""
        from agent.task_queue import build_cron_jobs
        from config import Settings

        s = Settings(OVERSEAS_NEWS_SCHEDULE_ENABLED=True)
        jobs = build_cron_jobs(s)
        assert len(jobs) == 3

    def test_worker_settings_includes_scheduled_function(self):
        """WorkerSettings.functions 包含定时任务。"""
        from agent.task_queue import WorkerSettings

        func_names = [f.name for f in WorkerSettings.functions]
        assert "scheduled_overseas_news_crawl" in func_names
