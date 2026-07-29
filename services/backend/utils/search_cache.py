"""搜索频率限制与结果缓存（基于 Redis）。"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis
from config import get_settings

logger = logging.getLogger("backend.search_cache")

_pool: aioredis.ConnectionPool | None = None


def _get_redis() -> aioredis.Redis:
    """获取共享的 Redis 连接（使用与 ARQ 相同的 DB）。"""
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = aioredis.ConnectionPool(
            host=s.REDIS_HOST,
            port=s.REDIS_PORT,
            db=s.REDIS_DB,
            password=s.REDIS_PASSWORD or None,
            max_connections=8,
        )
    return aioredis.Redis(connection_pool=_pool)


# ── 频率限制 ──────────────────────────────────────────


async def check_rate_limit(user_id: str) -> bool:
    """检查用户是否超过搜索频率限制。

    Returns:
        True 表示允许搜索，False 表示被限流。
    """
    s = get_settings()
    limit = s.WEB_SEARCH_RATE_LIMIT_PER_MINUTE
    key = f"search:rate:{user_id}"

    r = _get_redis()
    current = await r.incr(key)
    if current == 1:
        await r.expire(key, 60)

    if current > limit:
        logger.warning(
            "search rate limited: user=%s count=%d limit=%d",
            user_id, current, limit,
        )
        return False
    return True


# ── 结果缓存 ──────────────────────────────────────────


def _cache_key(
    q: str,
    categories: list[str] | None,
    language: str,
    time_range: str | None,
    safesearch: int,
    pageno: int,
) -> str:
    """生成搜索缓存键。"""
    raw = json.dumps(
        {
            "q": q.strip().lower(),
            "categories": sorted(categories) if categories else [],
            "language": language,
            "time_range": time_range or "",
            "safesearch": safesearch,
            "pageno": pageno,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"search:cache:{digest}"


async def get_cached_result(
    q: str,
    categories: list[str] | None,
    language: str,
    time_range: str | None,
    safesearch: int,
    pageno: int,
) -> dict[str, Any] | None:
    """从 Redis 获取缓存的搜索结果。"""
    s = get_settings()
    if s.WEB_SEARCH_CACHE_TTL_MINUTES <= 0:
        return None

    key = _cache_key(q, categories, language, time_range, safesearch, pageno)
    r = _get_redis()
    raw = await r.get(key)
    if raw is None:
        return None

    logger.info("search cache hit: q_len=%d", len(q))
    return json.loads(raw)


async def set_cached_result(
    q: str,
    categories: list[str] | None,
    language: str,
    time_range: str | None,
    safesearch: int,
    pageno: int,
    data: dict[str, Any],
) -> None:
    """将搜索结果写入 Redis 缓存。"""
    s = get_settings()
    ttl = s.WEB_SEARCH_CACHE_TTL_MINUTES
    if ttl <= 0:
        return

    key = _cache_key(q, categories, language, time_range, safesearch, pageno)
    r = _get_redis()
    await r.set(key, json.dumps(data, ensure_ascii=False), ex=ttl * 60)
    logger.info("search cache set: q_len=%d ttl=%dmin", len(q), ttl)
