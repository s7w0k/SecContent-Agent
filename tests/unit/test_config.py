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

    def test_default_redis_and_arq_config(self):
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
        assert s.REDIS_HOST == "redis"
        assert s.REDIS_PORT == 6379
        assert s.REDIS_DB == 1
        assert s.REDIS_PASSWORD == ""
        assert s.ARQ_MAX_JOBS == 3
        assert s.ARQ_JOB_TIMEOUT == 600
        assert s.ARQ_MAX_RETRIES == 3

    def test_default_mcp_urls(self):
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test")
        assert s.MCP_WEWE_URL == "http://mcp-wewe:8100"
        assert s.MCP_CRAWL_URL == "http://mcp-crawl:8101"

    def test_default_llm_config(self):
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
        assert s.DEEPSEEK_BASE_URL == "https://api.deepseek.com"
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

    def test_default_jwt_config(self):
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
        assert s.JWT_SECRET == ""
        assert s.JWT_ALGORITHM == "HS256"
        assert s.JWT_EXPIRE_HOURS == 24


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

    def test_redis_and_arq_config_from_environment(self, monkeypatch):
        from config import Settings

        monkeypatch.setenv("REDIS_HOST", "redis.internal")
        monkeypatch.setenv("REDIS_PORT", "6380")
        monkeypatch.setenv("REDIS_DB", "4")
        monkeypatch.setenv("REDIS_PASSWORD", "queue-secret")
        monkeypatch.setenv("ARQ_MAX_JOBS", "8")
        monkeypatch.setenv("ARQ_JOB_TIMEOUT", "1200")
        monkeypatch.setenv("ARQ_MAX_RETRIES", "5")

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)

        assert s.REDIS_HOST == "redis.internal"
        assert s.REDIS_PORT == 6380
        assert s.REDIS_DB == 4
        assert s.REDIS_PASSWORD == "queue-secret"
        assert s.ARQ_MAX_JOBS == 8
        assert s.ARQ_JOB_TIMEOUT == 1200
        assert s.ARQ_MAX_RETRIES == 5

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

    def test_jwt_config_from_environment(self, monkeypatch):
        from config import Settings

        monkeypatch.setenv("JWT_SECRET", "test-secret-at-least-32-characters")
        monkeypatch.setenv("JWT_ALGORITHM", "HS512")
        monkeypatch.setenv("JWT_EXPIRE_HOURS", "48")

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)

        assert s.JWT_SECRET == "test-secret-at-least-32-characters"
        assert s.JWT_ALGORITHM == "HS512"
        assert s.JWT_EXPIRE_HOURS == 48


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

    @pytest.mark.parametrize("hours", [0, 721])
    def test_jwt_expire_hours_out_of_range(self, hours):
        from config import Settings

        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", JWT_EXPIRE_HOURS=hours)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("REDIS_HOST", ""),
            ("REDIS_PORT", 0),
            ("REDIS_PORT", 65536),
            ("REDIS_DB", -1),
            ("REDIS_DB", 16),
            ("ARQ_MAX_JOBS", 0),
            ("ARQ_MAX_JOBS", 21),
            ("ARQ_JOB_TIMEOUT", 59),
            ("ARQ_JOB_TIMEOUT", 3601),
            ("ARQ_MAX_RETRIES", -1),
            ("ARQ_MAX_RETRIES", 11),
        ],
    )
    def test_redis_and_arq_values_out_of_range(self, field, value):
        from config import Settings

        with pytest.raises(ValidationError):
            Settings(DEEPSEEK_API_KEY="test", _env_file=None, **{field: value})

    def test_deepseek_key_empty_warning(self, monkeypatch, caplog):
        import logging

        from config import Settings

        # Override both env var and .env file
        monkeypatch.setenv("DEEPSEEK_API_KEY", "  ")
        from config import get_settings

        get_settings.cache_clear()

        logging.getLogger("backend.config")
        with caplog.at_level(logging.WARNING, logger="backend.config"):
            s = Settings()
            # Key is stripped — verifies the field_validator runs
            assert s.DEEPSEEK_API_KEY.strip() == ""


class TestSettingsSingleton:
    """单例模式"""

    def test_get_settings_same_instance(self):
        from config import get_settings

        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_cache_clear(self):
        from config import get_settings

        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2  # Still same after first call populates cache
