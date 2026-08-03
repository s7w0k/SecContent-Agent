"""阶段十六 16.0/16.1 测试：调度配置与时区。"""

from __future__ import annotations

import pytest
from config import Settings


class TestOverseasNewsConfig:
    """调度配置测试。"""

    def test_defaults(self):
        """默认配置。"""
        s = Settings()
        assert s.OVERSEAS_NEWS_SCHEDULE_ENABLED is True
        assert s.OVERSEAS_NEWS_SCHEDULE_TIMEZONE == "Asia/Shanghai"
        assert s.OVERSEAS_NEWS_SCHEDULE_HOUR == 7
        assert s.OVERSEAS_NEWS_SCHEDULE_MINUTE == 0
        assert s.OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS == 1
        assert s.OVERSEAS_NEWS_JOB_TIMEOUT_SECONDS == 1200
        assert s.OVERSEAS_NEWS_LOCK_TTL_SECONDS == 1500
        assert s.OVERSEAS_NEWS_RUN_RETENTION_DAYS == 90
        assert s.OVERSEAS_NEWS_STARTUP_CATCHUP_ENABLED is True

    def test_invalid_hour(self):
        """非法小时。"""
        with pytest.raises(ValueError):
            Settings(OVERSEAS_NEWS_SCHEDULE_HOUR=24)

    def test_invalid_minute(self):
        """非法分钟。"""
        with pytest.raises(ValueError):
            Settings(OVERSEAS_NEWS_SCHEDULE_MINUTE=60)

    def test_invalid_timezone(self):
        """非法时区。"""
        with pytest.raises(ValueError, match="无效的 IANA 时区"):
            Settings(OVERSEAS_NEWS_SCHEDULE_TIMEZONE="Invalid/Zone")

    def test_lock_ttl_must_exceed_timeout(self):
        """锁 TTL 必须大于任务超时。"""
        with pytest.raises(ValueError, match="必须大于"):
            Settings(
                OVERSEAS_NEWS_JOB_TIMEOUT_SECONDS=1200,
                OVERSEAS_NEWS_LOCK_TTL_SECONDS=1200,
            )

    def test_valid_timezone(self):
        """合法时区。"""
        s = Settings(OVERSEAS_NEWS_SCHEDULE_TIMEZONE="America/New_York")
        assert s.OVERSEAS_NEWS_SCHEDULE_TIMEZONE == "America/New_York"

    def test_crawl_days_range(self):
        """抓取天数范围。"""
        with pytest.raises(ValueError):
            Settings(OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS=0)
        with pytest.raises(ValueError):
            Settings(OVERSEAS_NEWS_SCHEDULE_CRAWL_DAYS=8)

    def test_disabled(self, monkeypatch):
        """关闭调度。"""
        monkeypatch.setenv("OVERSEAS_NEWS_SCHEDULE_ENABLED", "false")
        s = Settings()
        assert s.OVERSEAS_NEWS_SCHEDULE_ENABLED is False


class TestBuildCronJobs:
    """build_cron_jobs 测试。"""

    def test_cron_jobs_include_maintenance(self):
        """包含维护任务（03:00 和 04:00）。"""
        from agent.task_queue import build_cron_jobs

        s = Settings(OVERSEAS_NEWS_SCHEDULE_ENABLED=False)
        jobs = build_cron_jobs(s)
        assert len(jobs) == 2  # 只有两个维护任务

    def test_cron_jobs_include_overseas_when_enabled(self):
        """启用时包含海外新闻任务。"""
        from agent.task_queue import build_cron_jobs

        s = Settings(OVERSEAS_NEWS_SCHEDULE_ENABLED=True)
        jobs = build_cron_jobs(s)
        assert len(jobs) == 3  # 两个维护 + 一个海外新闻

    def test_cron_jobs_exclude_overseas_when_disabled(self):
        """关闭时不包含海外新闻任务。"""
        from agent.task_queue import build_cron_jobs

        s = Settings(OVERSEAS_NEWS_SCHEDULE_ENABLED=False)
        jobs = build_cron_jobs(s)
        assert len(jobs) == 2

    def test_worker_settings_has_timezone(self):
        """WorkerSettings 设置了时区。"""
        from zoneinfo import ZoneInfo

        from agent.task_queue import WorkerSettings

        assert WorkerSettings.timezone == ZoneInfo("Asia/Shanghai")

    def test_worker_settings_cron_jobs_count(self):
        """WorkerSettings.cron_jobs 数量正确。"""
        from agent.task_queue import WorkerSettings

        assert len(WorkerSettings.cron_jobs) == 3  # 2 维护 + 1 海外新闻
