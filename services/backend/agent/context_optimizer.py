"""上下文与工具结果 Token 优化 — 阶段1 3.1 / 3.2 / 3.4 节（WBS 1.4）。

措施（禁止以牺牲事实和安全为代价盲目压缩）：
  3.2 工具结果缓存：
    - 工具结果按权限边界（tenant/user）和 freshness（TTL）缓存；
    - 缓存 key 必须包含 tenant/user/权限范围；
    - 发布、撤回和权限变化时主动失效（invalidate）。
  3.4 历史与工具结果压缩：
    - 工具正文不得在每轮重复回传：同一 run 内按 content hash 去重，
      后续轮只回传 source_id 引用；
    - 工具结果只保留结构化摘要、source_id 和必要片段；
    - 对话历史采用结构化摘要：保留用户明确约束、已确认事实、待核实项和失败模式。

安全约束：缓存 key 包含权限边界；不缓存不含权限标识的敏感结果。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("backend.agent.context_optimizer")


# ═══════════════════════════════════════════════════════════════
# ToolResultCache（3.2）
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CachedToolResult:
    """一次缓存的工具结果。"""

    content: str
    source_ids: tuple[str, ...] = ()
    result_hash: str = ""
    created_at: float = 0.0


class ToolResultCache:
    """进程内工具结果缓存（按权限边界 + freshness）。

    - key = sha256(tenant_id, user_id, tool_name, args)；
    - 同一权限边界内相同参数的工具结果直接复用；
    - TTL 过期自动失效；invalidate / invalidate_all 主动失效。
    """

    def __init__(self, *, ttl_seconds: int = 300, max_entries: int = 256) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._entries: dict[str, CachedToolResult] = {}

    # ── 读写 ──────────────────────────────────────────────

    def get(
        self,
        *,
        tenant_id: str = "",
        user_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> CachedToolResult | None:
        key = self._key(tenant_id=tenant_id, user_id=user_id, tool_name=tool_name, args=args)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.created_at + self.ttl_seconds < time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry

    def set(
        self,
        *,
        tenant_id: str = "",
        user_id: str,
        tool_name: str,
        args: dict[str, Any],
        content: str,
        source_ids: list[str] | None = None,
        result_hash: str = "",
    ) -> None:
        key = self._key(tenant_id=tenant_id, user_id=user_id, tool_name=tool_name, args=args)
        if len(self._entries) >= self.max_entries:
            # 简单驱逐：移除最早写入的一条
            try:
                oldest_key = min(self._entries, key=lambda k: self._entries[k].created_at)
                self._entries.pop(oldest_key, None)
            except ValueError:
                pass
        self._entries[key] = CachedToolResult(
            content=content,
            source_ids=tuple(source_ids or ()),
            result_hash=result_hash,
            created_at=time.monotonic(),
        )

    # ── 失效 ──────────────────────────────────────────────

    def invalidate(
        self,
        *,
        tenant_id: str = "",
        user_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> bool:
        key = self._key(tenant_id=tenant_id, user_id=user_id, tool_name=tool_name, args=args)
        return self._entries.pop(key, None) is not None

    def invalidate_all(self, *, tenant_id: str = "", user_id: str) -> int:
        """权限变化 / 发布撤回时按用户失效全部缓存。"""
        removed = 0
        prefix = self._prefix(tenant_id=tenant_id, user_id=user_id)
        for key in [k for k in self._entries if k.startswith(prefix)]:
            self._entries.pop(key, None)
            removed += 1
        return removed

    def clear(self) -> None:
        self._entries.clear()

    def size(self) -> int:
        return len(self._entries)

    # ── key ───────────────────────────────────────────────

    @staticmethod
    def _prefix(*, tenant_id: str, user_id: str) -> str:
        return f"t:{tenant_id or ''}|u:{user_id}|"

    @classmethod
    def _key(cls, *, tenant_id: str, user_id: str, tool_name: str, args: dict[str, Any]) -> str:
        prefix = cls._prefix(tenant_id=tenant_id, user_id=user_id)
        raw = json.dumps(
            {"tool": tool_name, "args": args},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ═══════════════════════════════════════════════════════════════
# ContextCompressor（3.4）
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolResultSummary:
    """压缩后的工具结果（模型可见）。"""

    content: str
    truncated: bool = False
    original_chars: int = 0
    source_ids: tuple[str, ...] = ()


class ContextCompressor:
    """同一 run 内的工具结果去重与摘要压缩。

    - 工具正文不得在每轮重复回传：重复 hash 的工具结果只回传 source_id 引用；
    - 工具结果按 max_chars 截断，保留结构化摘要与 source_id。
    """

    def __init__(self, *, max_tool_result_chars: int = 1500, max_history_chars: int = 2000) -> None:
        self.max_tool_result_chars = max(64, int(max_tool_result_chars))
        self.max_history_chars = max(128, int(max_history_chars))
        self._seen_hashes: dict[str, str] = {}  # result_hash -> source 摘要

    def has_seen(self, result_hash: str) -> bool:
        return result_hash in self._seen_hashes

    def mark_seen(self, result_hash: str, note: str) -> None:
        self._seen_hashes.setdefault(result_hash, note)

    def compress_tool_result(
        self,
        *,
        content: str,
        result_hash: str = "",
        source_ids: list[str] | None = None,
    ) -> ToolResultSummary:
        """压缩一条工具结果：去重 + 截断。

        Returns:
            ToolResultSummary；content 为去重引用（已存在）或截断后的内容。
        """
        ids = list(source_ids or [])
        original_chars = len(content)
        if result_hash and self.has_seen(result_hash):
            ref = f"[已获取: 结果来源 {' '.join(ids) if ids else '同前'}] (内容与之前相同，不再重复)"
            return ToolResultSummary(
                content=ref,
                truncated=True,
                original_chars=original_chars,
                source_ids=tuple(ids),
            )
        if result_hash:
            self.mark_seen(result_hash, " ".join(ids))

        if original_chars <= self.max_tool_result_chars:
            return ToolResultSummary(
                content=content,
                truncated=False,
                original_chars=original_chars,
                source_ids=tuple(ids),
            )
        return ToolResultSummary(
            content=content[: self.max_tool_result_chars] + "...(truncated)",
            truncated=True,
            original_chars=original_chars,
            source_ids=tuple(ids),
        )

    def summarize_history(
        self,
        history: list[dict[str, Any]],
        *,
        keep_recent: int = 4,
    ) -> str:
        """对话历史结构化摘要：保留最近 N 条并整体截断。

        保留用户明确约束、已确认事实等关键信息（简单实现：保留最近轮次，
        过长时截断，不做事实抽取）。
        """
        if not history:
            return ""
        recent = history[-max(1, keep_recent):]
        parts: list[str] = []
        for msg in recent:
            role = msg.get("role", "user")
            content = str(msg.get("content", ""))
            label = "用户" if role == "user" else "助手"
            parts.append(f"{label}: {content}")
        joined = "\n".join(parts)
        if len(joined) <= self.max_history_chars:
            return joined
        return joined[: self.max_history_chars] + "...(历史已压缩)"
