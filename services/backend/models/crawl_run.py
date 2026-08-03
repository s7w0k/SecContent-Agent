"""阶段十六 16.2：crawl_runs 模型 - 系统级抓取执行记录。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class CrawlRunStatus(StrEnum):
    """运行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class FulltextStatus(StrEnum):
    """全文阶段状态。"""

    NOT_REQUIRED = "not_required"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class CrawlRun(BaseModel):
    """crawl_runs 集合文档。"""

    run_key: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    job_type: str = Field(default="overseas_news", min_length=1, max_length=50)
    trigger: str = Field(min_length=1, max_length=20)  # scheduled / catchup / manual
    actor_id: str = Field(min_length=1, max_length=128)
    schedule_date: str | None = None  # 北京时间业务日期 YYYY-MM-DD
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=50)
    scheduled_for: datetime | None = None  # UTC 计划执行时刻
    crawl_days: int = Field(default=1, ge=1, le=7)
    status: CrawlRunStatus = CrawlRunStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    saved: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    invalid: int = Field(default=0, ge=0)
    fulltext_queued: int = Field(default=0, ge=0)
    fulltext_status: FulltextStatus = FulltextStatus.NOT_REQUIRED
    per_site: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    trace_id: str = Field(default="", max_length=200)
    lock_key: str = Field(default="", max_length=200)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_schedule_date_for_scheduled(self) -> CrawlRun:
        """scheduled/catchup 触发必须有 schedule_date。"""
        if self.trigger in ("scheduled", "catchup") and not self.schedule_date:
            raise ValueError(f"trigger={self.trigger} 需要 schedule_date")
        return self

    def compute_expires_at(self, retention_days: int = 90) -> datetime:
        """计算 TTL 过期时间。"""
        base = self.finished_at or self.started_at or datetime.now(UTC)
        return base + timedelta(days=retention_days)


def build_run_key(trigger: str, schedule_date: str) -> str:
    """构建幂等键。"""
    return f"overseas-news:{trigger}:{schedule_date}"


def build_run_id(schedule_date: str, suffix: str = "") -> str:
    """构建运行 ID。"""
    from uuid import uuid4

    short = uuid4().hex[:8]
    return f"crawl-overseas-{schedule_date.replace('-', '')}-{short}"


def build_lock_key(schedule_date: str) -> str:
    """构建共享锁键。"""
    return f"crawl-overseas-{schedule_date}"


def create_initial_run(
    *,
    run_id: str,
    run_key: str,
    trigger: str,
    actor_id: str,
    schedule_date: str,
    timezone: str,
    crawl_days: int,
    trace_id: str = "",
    lock_key: str = "",
    retention_days: int = 90,
) -> dict[str, Any]:
    """创建初始 crawl_run 文档（用于原子 upsert 的 $setOnInsert）。"""
    now = datetime.now(UTC)
    run = CrawlRun(
        run_key=run_key,
        run_id=run_id,
        trigger=trigger,
        actor_id=actor_id,
        schedule_date=schedule_date,
        timezone=timezone,
        crawl_days=crawl_days,
        status=CrawlRunStatus.PENDING,
        attempt=0,
        trace_id=trace_id,
        lock_key=lock_key,
        started_at=now,
        updated_at=now,
    )
    doc = run.model_dump(mode="json")
    doc["expires_at"] = run.compute_expires_at(retention_days)
    return doc
