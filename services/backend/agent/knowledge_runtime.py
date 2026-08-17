"""任务边界知识热刷新编排。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("backend.agent.knowledge_runtime")

# 发布锁最长等待秒数
_LOCK_WAIT_MAX_SECONDS = 30
_LOCK_POLL_INTERVAL = 1
_LOCK_KEY = "global-knowledge-publication"


class KnowledgeRuntimeRefresher:
    """在任务边界检测知识库变更并刷新 Agent 的 Prompt 缓存。

    不改变文件发现、Prompt 模板和拼接逻辑，
    只调用现有 _build_system_prompt() 和重新绑定缓存。
    """

    def __init__(self, app_state: Any):
        self.app_state = app_state

    async def refresh_if_changed(self) -> bool:
        """检测文件变更并刷新所有 Agent 的知识缓存。

        Returns:
            True 如果检测到变更并已刷新。
        """
        loader = getattr(self.app_state, "knowledge_loader", None)
        if loader is None:
            return False

        changed = await loader.reload_if_changed()
        if not changed:
            return False

        logger.info("Knowledge changed, refreshing agent prompts...")
        self._refresh_agents()
        return True

    def _refresh_agents(self) -> None:
        """重新构建所有依赖知识的 Agent 的 Prompt 缓存。"""
        # V2 Scorer - rebuild system_prompt
        scorer_v2 = getattr(self.app_state, "scorer_v2", None)
        if scorer_v2 is not None:
            scorer_v2.refresh_prompt()
            logger.info("ScoringAgentV2 system_prompt refreshed")

        # V1 Scorer / ReportAgent / DraftGenerator 使用 knowledge._cache，
        # reload_if_changed() 已更新 _cache，无需显式重建。

        # Log the new hash
        loader = getattr(self.app_state, "knowledge_loader", None)
        if loader:
            logger.info(
                "Knowledge refreshed: hash=%s",
                loader._last_hash[:8] if loader._last_hash else "unknown",
            )

    async def get_current_hash(self) -> str:
        """返回当前知识库哈希。"""
        loader = getattr(self.app_state, "knowledge_loader", None)
        if loader:
            return loader._last_hash or ""
        return ""

    async def get_index_version(self) -> str:
        """返回当前知识索引版本（阶段四 S4-5：任务边界加载新版本）。"""
        index_path = self._index_path()
        if index_path is None or not index_path.exists():
            return ""
        try:
            data = index_path.read_text(encoding="utf-8")

            payload = json.loads(data)
            return payload.get("index_version", "")
        except Exception as exc:
            logger.warning("读取知识索引版本失败: %s", exc)
            return ""

    def _index_path(self) -> Path | None:
        """定位知识索引文件路径（复用 knowledge_index 默认路径）。"""
        try:
            from agent.knowledge_index import DEFAULT_INDEX_FILENAME

            loader = getattr(self.app_state, "knowledge_loader", None)
            docs_dir = getattr(loader, "docs_dir", None)
            if docs_dir is None:
                return None
            return Path(docs_dir) / "_index" / DEFAULT_INDEX_FILENAME
        except Exception:
            return None

    async def prepare_for_task(self) -> str:
        """任务开始前准备知识（等待发布锁释放 + 刷新）。

        Returns:
            当前知识哈希。
        """
        db = getattr(self.app_state, "db", None)
        if db is not None:
            for _ in range(_LOCK_WAIT_MAX_SECONDS):
                lock = await db["knowledge_publish_locks"].find_one(
                    {"lock_key": _LOCK_KEY}
                )
                if lock is None:
                    break
                # Check if expired
                expires = lock.get("expires_at")
                if expires and expires < datetime.now(UTC):
                    break
                logger.info("Waiting for knowledge publication lock...")
                await asyncio.sleep(_LOCK_POLL_INTERVAL)

        await self.refresh_if_changed()
        return await self.get_current_hash()
