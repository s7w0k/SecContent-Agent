"""ContextCache — 阶段二 Step 6：上下文缓存、版本化和主动失效。

设计要点：
  - 缓存键包含版本分量：user_id + purpose + product_ids + query_hash + model_id
    + token_budget + skill_snapshot_hash + knowledge_snapshot + memory_version，
    任一版本变化自动落新键（版本化失效），TTL 仅作兜底
  - user namespace 物理隔离（外键为 user_id），读取后再断言 user_id
  - single-flight：同一 key 并发构建只执行一次，防击穿
  - 事件日志只存 key hash / status，不存 Context 全文
  - 离线压缩默认关闭（CONTEXT_OFFLINE_COMPRESSION_ENABLED=false），
    启用时另行记录 source hash / model / prompt 等校验信息（预留）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("backend.agent.context_cache")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextCacheKey:
    """ContextPlan 缓存键（含全部版本分量）。"""

    user_id: str
    purpose: str
    product_ids: tuple[str, ...]
    query_hash: str
    model_id: str
    token_budget: int
    skill_snapshot_hash: str
    knowledge_snapshot: str
    memory_version: str

    @property
    def key_hash(self) -> str:
        payload = "|".join(
            [
                self.user_id or "",
                self.purpose,
                ",".join(sorted(self.product_ids)),
                self.query_hash,
                self.model_id,
                str(self.token_budget),
                self.skill_snapshot_hash,
                self.knowledge_snapshot,
                self.memory_version,
            ]
        )
        return "sha256:" + _sha256(payload)


@dataclass
class _Entry:
    plan: Any  # ContextPlan
    user_id: str
    purpose: str
    expires_at: float
    created_at: float


class ContextCache:
    """进程内上下文缓存（按用户命名空间隔离 + 版本化失效）。"""

    def __init__(self, ttl_seconds: int = 300, max_entries_per_user: int = 64):
        self._ttl = ttl_seconds
        self._max_per_user = max_entries_per_user
        # user_id -> key_hash -> _Entry
        self._store: dict[str, dict[str, _Entry]] = {}
        # key_hash -> asyncio.Lock（single-flight）
        self._locks: dict[str, asyncio.Lock] = {}
        # 事件日志：仅 key hash / status
        self._events: deque[dict[str, str]] = deque(maxlen=500)
        self._hits = 0
        self._misses = 0

    # ── 事件 / 统计（只含 hash 与 status）──────────────────

    def record(self, key_hash: str, status: str, mode: str = "") -> None:
        self._events.append({"key_hash": key_hash, "status": status, "mode": mode})
        if status == "hit":
            self._hits += 1
        elif status in ("miss", "built"):
            self._misses += 1

    def recent_events(self, limit: int = 50) -> list[dict[str, str]]:
        events = list(self._events)
        return events[-limit:]

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "namespaces": len(self._store),
            "entries": sum(len(ns) for ns in self._store.values()),
        }

    # ── 读写 ─────────────────────────────────────────────

    async def get(self, key: ContextCacheKey) -> Any | None:
        """读取缓存；返回 None 表示未命中/过期。读取后再次断言 user_id。"""
        namespace = self._store.get(key.user_id)
        if namespace is None:
            return None
        entry = namespace.get(key.key_hash)
        if entry is None:
            return None
        # user namespace 物理隔离 + 读取后断言
        if entry.user_id != key.user_id:
            self._delete_entry(key.user_id, key.key_hash)
            return None
        import time

        if time.monotonic() > entry.expires_at:
            self._delete_entry(key.user_id, key.key_hash)
            return None
        return entry.plan

    async def set(self, key: ContextCacheKey, plan: Any) -> None:
        import time

        namespace = self._store.setdefault(key.user_id, {})
        # 简单容量上限：超出时清理最早条目
        if len(namespace) >= self._max_per_user:
            oldest = min(
                namespace, key=lambda k: namespace[k].created_at, default=None
            )
            if oldest is not None:
                del namespace[oldest]
        namespace[key.key_hash] = _Entry(
            plan=plan,
            user_id=key.user_id,
            purpose=key.purpose,
            expires_at=time.monotonic() + self._ttl,
            created_at=time.monotonic(),
        )

    async def get_or_build(
        self,
        key: ContextCacheKey,
        builder: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, str]:
        """single-flight：命中直接返回；未命中在 per-key 锁内构建一次。"""
        lock = self._locks.setdefault(key.key_hash, asyncio.Lock())
        async with lock:
            cached = await self.get(key)
            if cached is not None:
                return cached, "hit"
            plan = await builder()
            await self.set(key, plan)
            return plan, "built"

    # ── 主动失效 ─────────────────────────────────────────

    async def invalidate(
        self,
        *,
        user_id: str | None = None,
        purpose: str | None = None,
    ) -> int:
        """主动失效：按用户命名空间 / purpose 清除。

        Args:
            user_id: 仅清除该用户命名空间（None=全部用户）
            purpose: 仅清除该 purpose 的条目（None=全部）

        Returns:
            清除条目数
        """
        removed = 0
        for ns_user, namespace in list(self._store.items()):
            if user_id is not None and ns_user != user_id:
                continue
            for key_hash in list(namespace.keys()):
                entry = namespace[key_hash]
                if purpose is not None and entry.purpose != purpose:
                    continue
                del namespace[key_hash]
                removed += 1
            if not namespace:
                self._store.pop(ns_user, None)
                for kh in list(self._locks.keys()):
                    self._locks.pop(kh, None)
        # 事件记录只保留 key hash/status
        self.record(f"invalidate:user={user_id or '*'}:purpose={purpose or '*'}", "invalidate")
        return removed

    # ── 内部 ─────────────────────────────────────────────

    def _delete_entry(self, user_id: str, key_hash: str) -> None:
        namespace = self._store.get(user_id)
        if namespace is not None:
            namespace.pop(key_hash, None)
            if not namespace:
                self._store.pop(user_id, None)
                self._locks.pop(key_hash, None)


_default_cache: ContextCache | None = None


def get_context_cache(ttl_seconds: int = 300) -> ContextCache:
    """获取全局 ContextCache（懒初始化，幂等）。"""
    global _default_cache
    if _default_cache is None:
        _default_cache = ContextCache(ttl_seconds=ttl_seconds)
    return _default_cache
