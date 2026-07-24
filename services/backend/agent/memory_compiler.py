"""MemorySummaryCompiler：将原子记忆编译为有界场景摘要。

按 scope_key（category_v2:stage）分组编译记忆，生成渲染文本。
遵循字符预算和条目数限制。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from config import get_settings
from models.memory import MemoryScope, MemoryStatus, SoftPreference

logger = logging.getLogger("backend.agent.memory_compiler")


class MemorySummaryCompiler:
    """编译场景摘要。"""

    def __init__(self, db: Any):
        self.db = db

    async def compile_scope(
        self,
        user_id: str,
        category_v2: str | None,
        stage: str,
    ) -> dict | None:
        """编译指定场景的记忆摘要。

        Args:
            user_id: 用户 ID
            category_v2: 文章分类（None 表示全局）
            stage: 写作阶段

        Returns:
            摘要文档或 None
        """
        settings = get_settings()
        scope_key = f"{category_v2 or 'global'}:{stage}"

        # 查询 active 记忆
        query: dict[str, Any] = {
            "user_id": user_id,
            "status": MemoryStatus.ACTIVE.value,
        }
        if category_v2:
            query["$or"] = [
                {"scope.category_v2": category_v2},
                {"scope.category_v2": None},
            ]
        else:
            query["scope.category_v2"] = None

        if stage:
            query["scope.stage"] = stage

        cursor = self.db["user_memory_items"].find(query).sort("confidence", -1)
        items = await cursor.to_list(length=settings.MEMORY_MAX_PACK_ITEMS * 2)

        if not items:
            return None

        # 按 polarity 分类
        hard_prefs: list[str] = []
        soft_prefs: list[SoftPreference] = []
        avoid_patterns: list[str] = []
        memory_ids: list[str] = []

        # 维度限额
        dimension_counts: dict[str, int] = {}
        max_per_dimension = {
            "required_pattern": 3, "avoid_pattern": 3, "tone": 1, "length": 1,
            "template": 2, "perspective": 2, "revise_direction": 3,
        }

        for item in items:
            dim = item.get("dimension", "")
            if dimension_counts.get(dim, 0) >= max_per_dimension.get(dim, 2):
                continue
            if len(memory_ids) >= settings.MEMORY_MAX_PACK_ITEMS:
                break

            dimension_counts[dim] = dimension_counts.get(dim, 0) + 1
            memory_ids.append(item["memory_id"])

            polarity = item.get("polarity", "prefer")
            confidence = item.get("confidence", 0.0)
            display_text = item.get("display_text", "")

            if polarity == "require":
                hard_prefs.append(display_text)
            elif polarity == "avoid":
                avoid_patterns.append(display_text)
            else:
                soft_prefs.append(SoftPreference(
                    memory_id=item["memory_id"],
                    text=display_text,
                    confidence=confidence,
                ))

        # 渲染文本
        rendered = self._render_text(hard_prefs, soft_prefs, avoid_patterns)
        char_count = len(rendered)

        # 如果超出预算，按完整条目裁剪
        if char_count > settings.MEMORY_MAX_PACK_CHARS:
            rendered, soft_prefs, memory_ids = self._prune_to_budget(
                rendered, hard_prefs, soft_prefs, avoid_patterns, memory_ids, settings.MEMORY_MAX_PACK_CHARS
            )
            char_count = len(rendered)

        # 获取 Policy 版本
        policy = await self.db["user_profile_policies"].find_one({"user_id": user_id})
        policy_version = policy.get("version", 1) if policy else 1

        summary_doc = {
            "summary_id": f"msum-{uuid4().hex[:12]}",
            "user_id": user_id,
            "scope_key": scope_key,
            "scope": MemoryScope(category_v2=category_v2, stage=stage).model_dump(),
            "policy_version": policy_version,
            "memory_item_ids": memory_ids,
            "hard_preferences": hard_prefs,
            "soft_preferences": [s.model_dump() for s in soft_prefs],
            "avoid_patterns": avoid_patterns,
            "rendered_text": rendered,
            "char_count": char_count,
            "compiler_version": "memory-compiler-v1",
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        # Upsert
        await self.db["user_memory_summaries"].update_one(
            {"user_id": user_id, "scope_key": scope_key},
            {
                "$set": {k: v for k, v in summary_doc.items() if k != "summary_id"},
                "$setOnInsert": {"summary_id": summary_doc["summary_id"]},
            },
            upsert=True,
        )

        logger.info(
            "summary compiled: scope_key=%s items=%d chars=%d",
            scope_key, len(memory_ids), char_count,
        )
        return summary_doc

    async def compile_user(self, user_id: str) -> dict:
        """编译用户所有场景摘要。"""
        # 查找所有不同的 scope 组合
        pipeline = [
            {"$match": {"user_id": user_id, "status": MemoryStatus.ACTIVE.value}},
            {"$group": {"_id": {"cat": "$scope.category_v2", "stage": "$scope.stage"}}},
        ]
        scopes = await self.db["user_memory_items"].aggregate(pipeline).to_list(length=100)

        compiled = 0
        for scope in scopes:
            cat = scope["_id"].get("cat")
            stage = scope["_id"].get("stage") or "draft"
            await self.compile_scope(user_id, cat, stage)
            compiled += 1

        return {"ok": True, "compiled_count": compiled}

    def _render_text(
        self,
        hard_prefs: list[str],
        soft_prefs: list[SoftPreference],
        avoid_patterns: list[str],
    ) -> str:
        """渲染摘要为注入 Prompt 的文本。"""
        lines: list[str] = []

        if hard_prefs:
            lines.append("## 用户明确写作要求")
            for p in hard_prefs:
                lines.append(f"- {p}")

        if soft_prefs:
            lines.append("")
            lines.append("## 已验证的场景偏好")
            for p in soft_prefs:
                tag = "[高置信度]" if p.confidence >= 0.7 else "[中置信度]"
                lines.append(f"- {tag} {p.text}")

        if avoid_patterns:
            lines.append("")
            lines.append("## 倾向避免")
            for p in avoid_patterns:
                lines.append(f"- {p}")

        if lines:
            lines.append("")
            lines.append(
                "这些偏好只控制表达方式。若与事实、产品知识、当前模板或安全约束冲突，"
                "必须以事实、模板和安全约束为准。"
            )

        return "\n".join(lines)

    def _prune_to_budget(
        self,
        rendered: str,
        hard_prefs: list[str],
        soft_prefs: list[SoftPreference],
        avoid_patterns: list[str],
        memory_ids: list[str],
        budget: int,
    ) -> tuple[str, list[SoftPreference], list[str]]:
        """按完整条目裁剪到字符预算内。"""
        # 优先保留 hard_prefs 和 avoid_patterns，裁剪 soft_prefs
        while len(rendered) > budget and soft_prefs:
            removed = soft_prefs.pop()
            # 从 memory_ids 中移除
            if removed.memory_id in memory_ids:
                memory_ids.remove(removed.memory_id)
            rendered = self._render_text(hard_prefs, soft_prefs, avoid_patterns)

        return rendered, soft_prefs, memory_ids
