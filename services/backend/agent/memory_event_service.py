"""Memory Event 幂等写入服务。

提供统一的 create_memory_event() 函数，供反馈、改稿、下载等入口调用。
实现幂等写入（idempotency_key 唯一约束）和 Feature Flag 控制。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from config import get_settings
from models.memory import MemoryEventStatus, MemorySourceType, MemoryStage

logger = logging.getLogger("backend.agent.memory_event_service")


async def create_memory_event(
    db: Any,
    user_id: str,
    source_type: MemorySourceType,
    *,
    source_id: str = "",
    article_url_hash: str | None = None,
    draft_index: int | None = None,
    revision_id: str | None = None,
    generation_id: str | None = None,
    template_id: str | None = None,
    template_key: str | None = None,
    template_version: int | None = None,
    category_v2: str | None = None,
    stage: MemoryStage = MemoryStage.DRAFT,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    arq_pool: Any = None,
) -> str | None:
    """幂等创建记忆事件。

    Args:
        db: MongoDB 数据库实例
        user_id: 用户 ID
        source_type: 信号来源类型
        ...: 其他上下文字段
        idempotency_key: 幂等键，默认由 source_type:source_id 生成
        arq_pool: ARQ 连接池，传入时自动 enqueue process_memory_event 任务

    Returns:
        event_id 或 None（Feature Flag 关闭或重复事件时返回 None）

    安全保证：
    - Feature Flag MEMORY_DUAL_WRITE_ENABLED 关闭时不写入
    - 幂等键重复时静默跳过
    - 事件写入失败不影响主请求
    """
    settings = get_settings()
    if not settings.MEMORY_DUAL_WRITE_ENABLED:
        return None

    if idempotency_key is None:
        idempotency_key = f"{source_type.value}:{source_id}" if source_id else f"{source_type.value}:{uuid4().hex[:12]}"

    event_id = f"mevt-{uuid4().hex[:12]}"
    now = datetime.now(UTC)

    event_doc = {
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "user_id": user_id,
        "source_type": source_type.value,
        "source_id": source_id,
        "article_url_hash": article_url_hash,
        "draft_index": draft_index,
        "revision_id": revision_id,
        "generation_id": generation_id,
        "template_id": template_id,
        "template_key": template_key,
        "template_version": template_version,
        "category_v2": category_v2,
        "stage": stage.value,
        "payload": payload or {},
        "status": MemoryEventStatus.PENDING.value,
        "attempts": 0,
        "candidate_memory_ids": [],
        "processor_version": "memory-learner-v1",
        "error": None,
        "created_at": now,
        "processed_at": None,
    }

    try:
        await db["user_memory_events"].insert_one(event_doc)
        logger.info(
            "memory event created: event_id=%s source_type=%s user_id=%s",
            event_id,
            source_type.value,
            user_id,
        )
    except Exception as exc:
        # 幂等键重复是预期行为
        if "idempotency_key" in str(exc) or "E11000" in str(exc):
            logger.debug("memory event already exists: idempotency_key=%s", idempotency_key)
            return None
        # 其他错误不阻塞主请求
        logger.warning("memory event creation failed: %s", exc)
        return None

    # 触发 ARQ 异步处理任务
    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "process_memory_event",
                event_id=event_id,
                user_id=user_id,
            )
            logger.info("memory event enqueued: event_id=%s", event_id)
        except Exception as exc:
            logger.warning("failed to enqueue process_memory_event: %s", exc)

    return event_id
