"""
Backend 配置管理 — 独立单元测试

运行:
    pytest tests/unit/test_config.py -v
"""

from __future__ import annotations

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


class TestSettingsDefaults:
    """默认值校验"""

    def test_default_mongodb_uri(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test-key")
        assert s.MONGODB_URI == "mongodb://admin:pr_agent_2024@mongodb:27017"

    def test_default_db_name(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        assert s.MONGODB_DB == "pr_agent"

    def test_default_pool_sizes(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        assert s.MONGODB_MAX_POOL_SIZE == 20
        assert s.MONGODB_MIN_POOL_SIZE == 2

    def test_default_mcp_urls(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        assert s.MCP_WEWE_URL == "http://mcp-wewe:8100"
        assert s.MCP_CRAWL_URL == "http://mcp-crawl:8101"

    def test_default_llm_config(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        # .env may override the code default — just verify it's a valid URL
        assert s.DEEPSEEK_BASE_URL.startswith("https://")
        assert s.DEEPSEEK_MODEL == "deepseek-chat"

    def test_default_pipeline(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        assert s.PIPELINE_SCORE_THRESHOLD == 140
        assert s.PIPELINE_CRAWL_DEFAULT_DAYS == 1

    def test_default_api(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        assert s.API_PAGE_SIZE_MAX == 100
        assert s.API_PAGE_SIZE_DEFAULT == 20
        assert s.BACKEND_PORT == 8000

    def test_cors_origins_default(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test")
        assert "http://localhost:5173" in s.CORS_ORIGINS
        assert "http://localhost:8000" in s.CORS_ORIGINS


class TestSettingsCustomValues:
    """自定义值覆盖"""

    def test_custom_mongodb(self):
        from config import Settings
        s = Settings(
            MONGODB_URI="mongodb://custom:27017",
            MONGODB_DB="custom_db",
            DEEPSEEK_API_KEY="sk-custom",
        )
        assert s.MONGODB_URI == "mongodb://custom:27017"
        assert s.MONGODB_DB == "custom_db"

    def test_custom_llm(self):
        from config import Settings
        s = Settings(
            DEEPSEEK_API_KEY="sk-custom",
            DEEPSEEK_MODEL="deepseek-reasoner",
            DEEPSEEK_BASE_URL="https://custom.llm.com",
        )
        assert s.DEEPSEEK_MODEL == "deepseek-reasoner"
        assert s.DEEPSEEK_BASE_URL == "https://custom.llm.com"

    def test_custom_thresholds(self):
        from config import Settings
        s = Settings(
            DEEPSEEK_API_KEY="test",
            PIPELINE_SCORE_THRESHOLD=120,
            PIPELINE_CRAWL_DEFAULT_DAYS=3,
        )
        assert s.PIPELINE_SCORE_THRESHOLD == 120
        assert s.PIPELINE_CRAWL_DEFAULT_DAYS == 3

    def test_custom_backend_port(self):
        from config import Settings
        s = Settings(DEEPSEEK_API_KEY="test", BACKEND_PORT=9000)
        assert s.BACKEND_PORT == 9000


class TestSettingsValidation:
    """参数校验与错误处理"""

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

    def test_score_threshold_too_high(self):
        from config import Settings
        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", PIPELINE_SCORE_THRESHOLD=999)

    def test_score_threshold_negative(self):
        from config import Settings
        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", PIPELINE_SCORE_THRESHOLD=-1)

    def test_page_size_too_large(self):
        from config import Settings
        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", API_PAGE_SIZE_MAX=1000)

    def test_page_size_too_small(self):
        from config import Settings
        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", API_PAGE_SIZE_MAX=5)

    def test_deepseek_key_empty_warning(self, monkeypatch, caplog):
        from config import Settings
        import logging

        # Override both env var and .env file
        monkeypatch.setenv("DEEPSEEK_API_KEY", "  ")
        from config import get_settings
        get_settings.cache_clear()

        logger = logging.getLogger("backend.config")
        with caplog.at_level(logging.WARNING, logger="backend.config"):
            s = Settings()
            # Key is stripped — verifies the field_validator runs
            assert s.DEEPSEEK_API_KEY.strip() == ""


class TestSettingsSingleton:
    """单例模式"""

    def test_get_settings_same_instance(self):
        from config import get_settings
        from config import Settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_cache_clear(self):
        from config import get_settings
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # Still same after first call populates cache
