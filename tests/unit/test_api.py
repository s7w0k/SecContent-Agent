"""
REST API 端点 — 单元测试

运行:
    pytest tests/unit/test_api.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _make_app(pipeline_manager=None, db=None, knowledge_loader=None):
    """创建测试用 FastAPI app，注入 mock 依赖"""
    from auth.deps import get_current_user
    from main import app as _app

    async def override_current_user():
        return "local-user"

    _app.dependency_overrides[get_current_user] = override_current_user

    # 深拷贝避免修改原始 app
    # 使用 app.state 注入 mock
    _app.state.pipeline_manager = pipeline_manager
    _app.state.db = db
    _app.state.knowledge_loader = knowledge_loader
    return _app


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_db():
    db = MagicMock()

    # 稳定 getitem
    articles_mock = MagicMock()
    reports_mock = MagicMock()
    user_activities_mock = MagicMock()
    pipeline_locks_mock = MagicMock()

    def _getitem(key):
        return {
            "articles": articles_mock,
            "reports": reports_mock,
            "user_activities": user_activities_mock,
            "pipeline_locks": pipeline_locks_mock,
        }.get(key, MagicMock())

    db.__getitem__.side_effect = _getitem

    # articles collection
    articles_mock.count_documents = AsyncMock(return_value=0)
    articles_mock.find_one = AsyncMock(return_value=None)
    articles_mock.aggregate = MagicMock(return_value=MagicMock())

    # cursor chain: find → sort → skip → limit → to_list
    list_result = AsyncMock(return_value=[])
    mock_cursor = MagicMock()
    mock_cursor.to_list = list_result
    mock_cursor.sort = MagicMock(return_value=mock_cursor)
    mock_cursor.skip = MagicMock(return_value=mock_cursor)
    mock_cursor.limit = MagicMock(return_value=mock_cursor)
    articles_mock.find = MagicMock(return_value=mock_cursor)

    # reports collection
    reports_mock.count_documents = AsyncMock(return_value=0)
    reports_mock.find_one = AsyncMock(return_value=None)
    reports_mock.find = MagicMock(return_value=mock_cursor)
    user_activities_mock.insert_one = AsyncMock(return_value=MagicMock(inserted_id="activity-id"))
    pipeline_locks_mock.delete_one = AsyncMock(return_value=MagicMock(deleted_count=0))
    pipeline_locks_mock.insert_one = AsyncMock(return_value=MagicMock(inserted_id="lock-id"))
    pipeline_locks_mock.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    pipeline_locks_mock.find_one = AsyncMock(return_value=None)
    db._user_activities = user_activities_mock

    return db


@pytest.fixture
def mock_manager():
    mgr = MagicMock()
    mgr.run_full = AsyncMock(return_value={
        "pipeline_id": "test-123",
        "status": "completed",
        "state": {"crawled_count": 0},
    })
    mgr.run_phase = AsyncMock(return_value={
        "pipeline_id": "test-456",
        "status": "completed",
        "state": {},
    })
    mgr.get_status = MagicMock(return_value={
        "status": "idle",
        "current_phase": "",
        "state": {},
        "errors": [],
    })
    return mgr


@pytest.fixture
def mock_knowledge():
    """使用简单类避免 MagicMock 序列化递归"""
    class FakeKnowledge:
        is_loaded = True

        async def load(self):
            return self._cache

    knowledge = FakeKnowledge()

    class FakeCache:
        product_name = "测试产品"
        core_features = ["功能A", "功能B"]
        tech_barriers = ["壁垒X"]
        control_points = ["控标1"]
        customer_cases = ["客户A"]
        key_terms = ["MCP", "Agent"]
        loaded_at = "2026-06-29 12:00:00"

    knowledge._cache = FakeCache()
    return knowledge


@pytest.fixture
def app(mock_manager, mock_db, mock_knowledge):
    return _make_app(
        pipeline_manager=mock_manager,
        db=mock_db,
        knowledge_loader=mock_knowledge,
    )


# ═══════════════════════════════════════════════════════════════
# 1. System Endpoints
# ═══════════════════════════════════════════════════════════════


class TestSystemEndpoints:
    """系统端点：health / config"""

    @pytest.mark.asyncio
    async def test_health_returns_200(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_config_summary(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/config/summary")
            assert resp.status_code == 200
            data = resp.json()
            assert "mongodb_db" in data
            assert "deepseek_model" in data


# ═══════════════════════════════════════════════════════════════
# 2. Pipeline Endpoints
# ═══════════════════════════════════════════════════════════════


class TestPipelineAPI:
    """流水线 API 测试"""

    @pytest.mark.asyncio
    async def test_run_full(self, app, mock_db):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/pipeline/run", json={"crawl_days": 1})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"
            app.state.pipeline_manager.run_full.assert_awaited_once_with(
                crawl_days=1,
                user_id="local-user",
            )
            activity = mock_db._user_activities.insert_one.await_args.args[0]
            assert activity["action"] == "pipeline_run"
            assert activity["target"]["pipeline_id"] == "test-123"

    @pytest.mark.asyncio
    async def test_run_crawl(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/pipeline/crawl", json={"crawl_days": 2})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_run_score(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/pipeline/score", json={})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_run_report(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/pipeline/report", json={})
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_status(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/pipeline/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data

    @pytest.mark.asyncio
    async def test_pipeline_not_initialized(self):
        """未初始化时返回 503"""
        app_no_mgr = _make_app(pipeline_manager=None, db=MagicMock())
        async with AsyncClient(transport=ASGITransport(app=app_no_mgr), base_url="http://test") as client:
            resp = await client.get("/api/pipeline/status")
            assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════
# 3. Dashboard Endpoints
# ═══════════════════════════════════════════════════════════════


class TestDashboardAPI:
    """仪表盘 API 测试"""

    @pytest.mark.asyncio
    async def test_list_articles_empty(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/articles")
            assert resp.status_code == 200
            data = resp.json()
            assert data["items"] == []
            assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_articles_with_filters(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/articles?source_type=overseas_news&category=MCP&min_score=100&page=1&page_size=10"
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_article_detail_not_found(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/articles/nonexistent_hash_1234567890ab")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_stats(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/stats")
            assert resp.status_code == 200
            data = resp.json()
            assert "total_articles" in data
            assert "ai_security_count" in data

    @pytest.mark.asyncio
    async def test_db_not_available(self):
        app_no_db = _make_app(pipeline_manager=MagicMock(), db=None)
        async with AsyncClient(transport=ASGITransport(app_no_db), base_url="http://test") as client:
            resp = await client.get("/api/articles")
            assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════
# 4. Reports Endpoints
# ═══════════════════════════════════════════════════════════════


class TestReportsAPI:
    """报道 API 测试"""

    @pytest.mark.asyncio
    async def test_list_reports_empty(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/reports")
            assert resp.status_code == 200
            data = resp.json()
            assert data["items"] == []
            assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_report_detail_invalid_id(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/reports/invalid-id")
            assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_report_detail_not_found(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/reports/507f1f77bcf86cd799439011")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_knowledge(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/knowledge")
            assert resp.status_code == 200
            data = resp.json()
            assert data["loaded"] is True
            assert data["product_name"] == "测试产品"
            assert data["features_count"] == 2

    @pytest.mark.asyncio
    async def test_knowledge_not_loaded(self):
        knowledge = MagicMock()
        knowledge.is_loaded = False
        app_no_k = _make_app(pipeline_manager=MagicMock(), db=MagicMock(), knowledge_loader=knowledge)
        async with AsyncClient(transport=ASGITransport(app_no_k), base_url="http://test") as client:
            resp = await client.get("/api/knowledge")
            assert resp.status_code == 200
            data = resp.json()
            assert data["loaded"] is False


# ═══════════════════════════════════════════════════════════════
# 5. 参数校验测试
# ═══════════════════════════════════════════════════════════════


class TestParameterValidation:
    """参数校验"""

    @pytest.mark.asyncio
    async def test_articles_pagination_bounds(self, app):
        """page_size 超限"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/articles?page_size=999")
            # FastAPI 返回 422 参数校验错误
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_pipeline_crawl_days_bounds(self, app):
        """crawl_days 超限"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/pipeline/crawl", json={"crawl_days": 999})
            assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_articles_invalid_sort(self, app):
        """无效排序字段使用默认值"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/articles?sort_by=invalid_field")
            assert resp.status_code == 200  # 降级为默认排序
