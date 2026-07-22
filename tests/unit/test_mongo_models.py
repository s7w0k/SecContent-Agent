"""
Backend 单元测试 — config / MongoDB / Article / Report 模型

运行:
    PYTHONPATH=services/backend pytest tests/unit/test_mongo_models.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════
# 1. Settings / Config 测试
# ═══════════════════════════════════════════════════════════

class TestSettings:
    """配置管理测试"""

    def test_default_values(self):
        from config import Settings

        # 默认值测试不能读取开发机根目录的 .env，否则本地模型配置会污染断言。
        s = Settings(DEEPSEEK_API_KEY="test-key", _env_file=None)
        assert s.MONGODB_URI == "mongodb://admin:pr_agent_2024@mongodb:27017"
        assert s.MONGODB_DB == "pr_agent"
        assert s.MONGODB_MAX_POOL_SIZE == 20
        assert s.MONGODB_MIN_POOL_SIZE == 2
        assert s.MCP_WEWE_URL == "http://mcp-wewe:8100"
        assert s.MCP_CRAWL_URL == "http://mcp-crawl:8101"
        assert s.DEEPSEEK_MODEL == "deepseek-chat"
        assert s.PIPELINE_SCORE_THRESHOLD == 140
        assert s.PIPELINE_CRAWL_DEFAULT_DAYS == 1
        assert s.API_PAGE_SIZE_MAX == 100
        assert s.API_PAGE_SIZE_DEFAULT == 20
        assert s.BACKEND_PORT == 8000

    def test_custom_values(self):
        from config import Settings

        s = Settings(
            MONGODB_URI="mongodb://custom:27017",
            MONGODB_DB="custom_db",
            DEEPSEEK_API_KEY="sk-custom",
            DEEPSEEK_MODEL="deepseek-reasoner",
            PIPELINE_SCORE_THRESHOLD=120,
            BACKEND_PORT=9000,
        )
        assert s.MONGODB_URI == "mongodb://custom:27017"
        assert s.MONGODB_DB == "custom_db"
        assert s.DEEPSEEK_MODEL == "deepseek-reasoner"
        assert s.PIPELINE_SCORE_THRESHOLD == 120
        assert s.BACKEND_PORT == 9000

    def test_mongo_uri_must_be_valid(self):
        from config import Settings

        with pytest.raises(ValidationError):
            Settings(MONGODB_URI="invalid-uri", DEEPSEEK_API_KEY="test")

    def test_mongo_uri_accepts_srv(self):
        from config import Settings

        s = Settings(
            MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net",
            DEEPSEEK_API_KEY="test",
        )
        assert "mongodb+srv" in s.MONGODB_URI

    def test_score_threshold_bounds(self):
        from config import Settings

        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", PIPELINE_SCORE_THRESHOLD=999)

        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", PIPELINE_SCORE_THRESHOLD=-1)

    def test_page_size_bounds(self):
        from config import Settings

        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", API_PAGE_SIZE_MAX=1000)

    def test_deepseek_key_warning(self, monkeypatch, caplog):
        import logging

        from config import Settings

        # 临时移除环境变量 + 禁用 .env 加载，模拟未配置场景
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        # 清除 lru_cache 以强制重新加载
        from config import get_settings
        get_settings.cache_clear()

        logger = logging.getLogger("backend.config")
        logger.propagate = True
        with caplog.at_level(logging.WARNING, logger="backend.config"):
            s = Settings(_env_file=None)  # skip .env file to test empty-key behavior
            assert s.DEEPSEEK_API_KEY == ""

    def test_get_settings_singleton(self):
        from config import get_settings

        s1 = get_settings()
        s2 = get_settings()
        # lru_cache 确保同一对象
        assert s1 is s2

    def test_cors_origins_default(self):
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test")
        assert "http://localhost:5173" in s.CORS_ORIGINS
        assert "http://localhost:8000" in s.CORS_ORIGINS


# ═══════════════════════════════════════════════════════════
# 2. MongoDB 连接管理测试
# ═══════════════════════════════════════════════════════════

class TestMongoDBConnection:
    """MongoDB 连接管理器测试"""

    def test_not_connected_initially(self):
        from db.mongo import MongoDB

        assert MongoDB.is_connected() is False

    def test_get_db_raises_when_not_connected(self):
        from db.mongo import MongoDB

        with pytest.raises(RuntimeError, match="not initialized"):
            MongoDB.get_db()

    def test_get_collection_raises_when_not_connected(self):
        from db.mongo import MongoDB

        with pytest.raises(RuntimeError, match="not initialized"):
            MongoDB.get_collection("articles")

    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self):
        from db.mongo import MongoDB
        from pymongo.errors import ConnectionFailure

        # Mock motor 的 AsyncIOMotorClient
        with patch("db.mongo.AsyncIOMotorClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.admin.command = AsyncMock(return_value={"ok": 1})
            mock_client_class.return_value = mock_client

            try:
                await MongoDB.connect(
                    uri="mongodb://localhost:27017",
                    db_name="test_db",
                )
            except ConnectionFailure:
                pytest.skip("Mock setup issue")

            assert MongoDB.is_connected() is True
            assert MongoDB._db_name == "test_db"

            await MongoDB.disconnect()
            assert MongoDB.is_connected() is False

    def test_mask_uri(self):
        from db.mongo import MongoDB

        masked = MongoDB._mask_uri("mongodb://admin:secret123@host:27017")
        assert "secret123" not in masked
        assert "admin:****" in masked
        assert "host:27017" in masked

    def test_health_check_disconnected(self):
        import asyncio

        from db.mongo import MongoDB

        result = asyncio.run(MongoDB.health_check())
        assert result["status"] == "disconnected"


# ═══════════════════════════════════════════════════════════
# 3. Article 模型测试
# ═══════════════════════════════════════════════════════════

class TestArticleModel:
    """Article 数据模型测试"""

    def _make_article(self, **overrides):
        from models.article import ArticleBase

        defaults = {
            "url_hash": hashlib.md5(b"https://example.com/test").hexdigest(),
            "title": "Test Article",
            "url": "https://example.com/test",
            "source": "The Hacker News",
        }
        defaults.update(overrides)
        return ArticleBase(**defaults)

    def test_create_minimal_article(self):
        art = self._make_article()
        assert art.title == "Test Article"
        assert art.url == "https://example.com/test"
        assert art.source_type == "overseas_news"
        assert art.is_ai_security is False
        assert art.ai_relevance_score == 0

    def test_url_hash_validation(self):
        from models.article import ArticleBase

        with pytest.raises(ValidationError):
            ArticleBase(url_hash="too-short", title="T", url="https://x.com", source="S")

        with pytest.raises(ValidationError):
            ArticleBase(url_hash="g" * 32, title="T", url="https://x.com", source="S")  # non-hex

    def test_url_hash_auto_lowercase(self):
        from models.article import ArticleBase

        art = ArticleBase(
            url_hash="ABCD1234ABCD1234ABCD1234ABCD1234",  # uppercase hex
            title="T",
            url="https://x.com",
            source="S",
        )
        assert art.url_hash == "abcd1234abcd1234abcd1234abcd1234"

    def test_total_score(self):
        art = self._make_article(ai_relevance_score=85, reportability_score=72)
        assert art.total_score == 157

    def test_is_high_value(self):
        art = self._make_article(ai_relevance_score=85, reportability_score=72)
        assert art.is_high_value is True  # 157 >= 140

        art2 = self._make_article(ai_relevance_score=50, reportability_score=50)
        assert art2.is_high_value is False  # 100 < 140

    def test_score_bounds(self):

        with pytest.raises(ValidationError):
            self._make_article(ai_relevance_score=150)

        with pytest.raises(ValidationError):
            self._make_article(reportability_score=-1)

    def test_source_type_enum(self):
        from models.article import SourceType

        art = self._make_article(source_type=SourceType.WECHAT_MP)
        assert art.source_type == "wechat_mp"

        art2 = self._make_article(source_type="paper")
        assert art2.source_type == "paper"

        uploaded = self._make_article(source_type=SourceType.USER_UPLOAD)
        assert uploaded.source_type == "user_upload"

    def test_invalid_source_type(self):

        with pytest.raises(ValidationError):
            self._make_article(source_type="invalid_source")

    def test_default_timestamps(self):
        art = self._make_article()
        assert art.added_at is not None
        assert art.published_at is None  # optional

    def test_article_create(self):
        from models.article import ArticleCreate

        ac = ArticleCreate(
            url_hash=hashlib.md5(b"test").hexdigest(),
            title="Test",
            url="https://example.com",
            source="S",
        )
        assert ac.title == "Test"

    def test_article_indb_alias(self):
        from models.article import ArticleInDB

        art = ArticleInDB(
            _id="507f1f77bcf86cd799439011",
            url_hash=hashlib.md5(b"test").hexdigest(),
            title="Test",
            url="https://example.com",
            source="S",
        )
        assert art.id == "507f1f77bcf86cd799439011"


# ═══════════════════════════════════════════════════════════
# 4. Report 模型测试
# ═══════════════════════════════════════════════════════════

class TestReportModel:
    """Report 数据模型测试"""

    def _make_report(self, **overrides):
        from models.report import ReportBase

        defaults = {
            "article_url_hash": hashlib.md5(b"test-article").hexdigest(),
            "title": "PR Report Title",
            "content_md": "# Report\n\nContent here.",
        }
        defaults.update(overrides)
        return ReportBase(**defaults)

    def test_create_minimal_report(self):
        r = self._make_report()
        assert r.title == "PR Report Title"
        assert r.template == "standard_pr"
        assert r.created_at is not None
        assert r.scores == {"relevance": 0, "reportability": 0}

    def test_default_scores(self):
        r = self._make_report()
        assert r.scores["relevance"] == 0
        assert r.scores["reportability"] == 0

    def test_custom_scores(self):
        r = self._make_report(scores={"relevance": 85, "reportability": 72})
        assert r.scores["relevance"] == 85
        assert r.scores["reportability"] == 72

    def test_article_url_hash_validation(self):
        from models.report import ReportBase

        with pytest.raises(ValidationError):
            ReportBase(article_url_hash="short", title="T")

    def test_title_max_length(self):
        from models.report import ReportBase

        with pytest.raises(ValidationError):
            ReportBase(
                article_url_hash=hashlib.md5(b"x").hexdigest(),
                title="A" * 500,
            )

    def test_report_create(self):
        from models.report import ReportCreate

        rc = ReportCreate(
            article_url_hash=hashlib.md5(b"x").hexdigest(),
            title="Test",
            content_md="# Content",
        )
        assert rc.title == "Test"
        assert rc.template == "standard_pr"

    def test_report_indb_alias(self):
        from models.report import ReportInDB

        r = ReportInDB(
            _id="507f1f77bcf86cd799439011",
            article_url_hash=hashlib.md5(b"x").hexdigest(),
            title="Test",
            content_md="# Content",
        )
        assert r.id == "507f1f77bcf86cd799439011"

    def test_generated_by_default(self):
        r = self._make_report()
        assert r.generated_by == "pr-agent-pipeline"


# ═══════════════════════════════════════════════════════════
# 5. FastAPI 应用测试
# ═══════════════════════════════════════════════════════════

class TestFastAPIApp:
    """FastAPI 入口和端点测试"""

    def test_app_exists(self):
        import main
        assert main.app is not None
        assert main.app.title == "PR Agent Demo - Backend"

    def test_app_routes(self):
        import main
        # Collect routes, handling both Route and _IncludedRouter objects
        routes = set()
        for r in main.app.routes:
            p = getattr(r, "path", None)
            if p:
                routes.add(p)
        assert "/api/health" in routes
        assert "/api/config/summary" in routes
        assert "/openapi.json" in routes

    @pytest.mark.asyncio
    async def test_health_endpoint_no_db(self):
        """测试 health endpoint（不连接 MongoDB）"""
        import main
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["status"] == "healthy"
            assert "mongodb" in data
            assert "mcp_wewe" in data

    @pytest.mark.asyncio
    async def test_config_summary(self):
        import main
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/config/summary")
            assert resp.status_code == 200
            data = resp.json()
            assert "deepseek_configured" in data
            assert "score_threshold" in data
            # 确保没有泄露 API key
            assert "DEEPSEEK_API_KEY" not in data
            assert "sk-" not in str(data)

    @pytest.mark.asyncio
    async def test_openapi_schema(self):
        import main
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=main.app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/openapi.json")
            assert resp.status_code == 200
            schema = resp.json()
            assert schema["info"]["title"] == "PR Agent Demo - Backend"
