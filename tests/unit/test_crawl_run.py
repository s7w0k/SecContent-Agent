"""阶段十六 16.2/16.3 测试：crawl_runs 模型 + 去重迁移。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from models.crawl_run import (
    CrawlRun,
    CrawlRunStatus,
    FulltextStatus,
    build_lock_key,
    build_run_id,
    build_run_key,
    create_initial_run,
)


class TestCrawlRunModel:
    """crawl_runs 模型测试。"""

    def test_defaults(self):
        """默认值。"""
        run = CrawlRun(
            run_key="overseas-news:scheduled:2026-08-04",
            run_id="crawl-overseas-20260804-abc123",
            trigger="scheduled",
            actor_id="system:scheduler",
            schedule_date="2026-08-04",
        )
        assert run.job_type == "overseas_news"
        assert run.status == CrawlRunStatus.PENDING
        assert run.fulltext_status == FulltextStatus.NOT_REQUIRED
        assert run.crawl_days == 1
        assert run.timezone == "Asia/Shanghai"
        assert run.attempt == 0
        assert run.per_site == {}
        assert run.errors == {}

    def test_scheduled_requires_schedule_date(self):
        """scheduled 触发需要 schedule_date。"""
        with pytest.raises(ValueError, match="需要 schedule_date"):
            CrawlRun(
                run_key="k",
                run_id="r",
                trigger="scheduled",
                actor_id="system:scheduler",
            )

    def test_manual_does_not_require_schedule_date(self):
        """manual 触发不需要 schedule_date。"""
        run = CrawlRun(
            run_key="overseas-news:manual:abc",
            run_id="crawl-overseas-manual-abc",
            trigger="manual",
            actor_id="user-123",
        )
        assert run.schedule_date is None

    def test_compute_expires_at(self):
        """TTL 过期时间计算。"""
        now = datetime.now(UTC)
        run = CrawlRun(
            run_key="k",
            run_id="r",
            trigger="manual",
            actor_id="u",
            finished_at=now,
        )
        expires = run.compute_expires_at(retention_days=90)
        assert expires > now
        delta = expires - now
        assert 89 < delta.days < 91

    def test_all_status_values(self):
        """所有状态枚举值。"""
        assert CrawlRunStatus.PENDING.value == "pending"
        assert CrawlRunStatus.RUNNING.value == "running"
        assert CrawlRunStatus.COMPLETED.value == "completed"
        assert CrawlRunStatus.PARTIAL.value == "partial"
        assert CrawlRunStatus.FAILED.value == "failed"
        assert CrawlRunStatus.SKIPPED.value == "skipped"

    def test_all_fulltext_status_values(self):
        """所有全文状态枚举值。"""
        assert FulltextStatus.NOT_REQUIRED.value == "not_required"
        assert FulltextStatus.QUEUED.value == "queued"
        assert FulltextStatus.RUNNING.value == "running"
        assert FulltextStatus.COMPLETED.value == "completed"
        assert FulltextStatus.PARTIAL.value == "partial"
        assert FulltextStatus.FAILED.value == "failed"


class TestCrawlRunHelpers:
    """辅助函数测试。"""

    def test_build_run_key(self):
        """构建幂等键。"""
        key = build_run_key("scheduled", "2026-08-04")
        assert key == "overseas-news:scheduled:2026-08-04"

    def test_build_run_key_catchup(self):
        """补偿触发幂等键。"""
        key = build_run_key("catchup", "2026-08-04")
        assert key == "overseas-news:catchup:2026-08-04"

    def test_build_run_id_format(self):
        """运行 ID 格式。"""
        run_id = build_run_id("2026-08-04")
        assert run_id.startswith("crawl-overseas-20260804-")
        assert len(run_id) > len("crawl-overseas-20260804-")

    def test_build_run_id_unique(self):
        """运行 ID 唯一。"""
        id1 = build_run_id("2026-08-04")
        id2 = build_run_id("2026-08-04")
        assert id1 != id2

    def test_build_lock_key(self):
        """锁键格式。"""
        key = build_lock_key("2026-08-04")
        assert key == "crawl-overseas-2026-08-04"

    def test_create_initial_run(self):
        """创建初始文档。"""
        doc = create_initial_run(
            run_id="crawl-overseas-20260804-abc",
            run_key="overseas-news:scheduled:2026-08-04",
            trigger="scheduled",
            actor_id="system:scheduler",
            schedule_date="2026-08-04",
            timezone="Asia/Shanghai",
            crawl_days=1,
            trace_id="trace-123",
            lock_key="crawl-overseas-2026-08-04",
        )
        assert doc["run_id"] == "crawl-overseas-20260804-abc"
        assert doc["status"] == "pending"
        assert doc["expires_at"] is not None
        assert doc["started_at"] is not None


class TestDedupeScript:
    """去重迁移脚本测试。"""

    def test_select_primary_with_content(self):
        """有正文的文档优先。"""
        from scripts.dedupe_articles import select_primary

        doc_with_content = {"content_md": "内容", "category_v2": None}
        doc_without = {"content_md": "", "category_v2": None}
        assert select_primary(doc_with_content) > select_primary(doc_without)

    def test_select_primary_with_classification(self):
        """已分类的文档优先。"""
        from scripts.dedupe_articles import select_primary

        doc_classified = {"content_md": "", "category_v2": "threat"}
        doc_not = {"content_md": "", "category_v2": None}
        assert select_primary(doc_classified) > select_primary(doc_not)

    def test_select_primary_with_score(self):
        """已打分的文档优先。"""
        from scripts.dedupe_articles import select_primary

        doc_scored = {"content_md": "", "pr_total_score": 80}
        doc_not = {"content_md": "", "pr_total_score": None}
        assert select_primary(doc_scored) > select_primary(doc_not)
