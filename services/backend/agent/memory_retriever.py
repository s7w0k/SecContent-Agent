"""MemoryRetriever：检索 Policy + 记忆，构建冻结 MemoryPack。

检索顺序：
1. 读取 user_profile_policies
2. 应用分类级 Policy Override
3. 查询 active 的场景记忆
4. 查询 active 的全局记忆
5. 排除与 Policy 冲突或状态非 Active 的记忆
6. 按相关性评分排序
7. 按维度去重和限额
8. 生成冻结 MemoryPack

实验分流：基于 user_id 稳定哈希分配实验组。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from config import get_settings
from models.memory import (
    MemoryItem,
    MemoryPack,
    MemoryScope,
    MemoryStage,
    MemoryStatus,
    ProfilePolicy,
    SoftPreference,
)

logger = logging.getLogger("backend.agent.memory_retriever")


def assign_experiment_group(user_id: str, enabled: bool = False) -> tuple[str, str]:
    """基于 user_id 稳定哈希分配实验组。

    Returns:
        (experiment_id, group) - group: "control" | "treatment"
    """
    if not enabled:
        return ("", "control")

    # 稳定哈希
    hash_val = int(hashlib.md5(user_id.encode()).hexdigest(), 16)
    percentage = (hash_val % 100) + 1  # 1-100

    if percentage <= 5:
        return ("memory-pack-v1", "treatment")
    return ("memory-pack-v1", "control")


class MemoryRetriever:
    """检索用户记忆并构建冻结 MemoryPack。"""

    def __init__(self, db: Any):
        self.db = db

    async def retrieve(
        self,
        user_id: str,
        category_v2: str | None = None,
        template_id: str | None = None,
        stage: MemoryStage = MemoryStage.DRAFT,
        target_audience: str | None = None,
    ) -> MemoryPack:
        """检索 Policy + 记忆，返回冻结 MemoryPack。"""
        settings = get_settings()

        # 1. 读取 Policy
        policy_doc = await self.db["user_profile_policies"].find_one({"user_id": user_id})
        policy = ProfilePolicy(**policy_doc) if policy_doc else None

        # 如果 auto_learning_enabled=False，不检索自动记忆
        auto_learning = policy.auto_learning_enabled if policy else True

        # 2. 查询 active 记忆
        query: dict[str, Any] = {
            "user_id": user_id,
            "status": MemoryStatus.ACTIVE.value,
        }

        if auto_learning and category_v2:
            query["$or"] = [
                {"scope.category_v2": category_v2},
                {"scope.category_v2": None},
            ]
        elif auto_learning:
            query["scope.category_v2"] = None

        if stage:
            query["scope.stage"] = stage

        cursor = self.db["user_memory_items"].find(query).sort("confidence", -1)
        items_raw = await cursor.to_list(length=settings.MEMORY_MAX_PACK_ITEMS * 3)

        # 3. 排除与 Policy 冲突的记忆
        if policy:
            items_raw = self._filter_policy_conflicts(items_raw, policy)

        # 4. 相关性评分排序
        scored = []
        for item in items_raw:
            score = self._relevance_score(item, category_v2, template_id, stage)
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)

        # 5. 维度去重和限额
        selected = self._dedupe_and_limit(scored, settings.MEMORY_MAX_PACK_ITEMS)

        # 6. 构建 MemoryPack
        memory_items = [MemoryItem(**item) for item in selected]
        memory_ids = [item["memory_id"] for item in selected]

        hard_prefs: list[str] = []
        soft_prefs: list[SoftPreference] = []
        avoid_patterns: list[str] = []

        # Policy 中的显式偏好
        if policy:
            if policy.required_patterns:
                hard_prefs.extend(policy.required_patterns)
            if policy.avoid_patterns:
                avoid_patterns.extend(policy.avoid_patterns)

        # 自动学习记忆
        for item in selected:
            polarity = item.get("polarity", "prefer")
            display = item.get("display_text", "")
            confidence = item.get("confidence", 0.0)
            mid = item.get("memory_id", "")

            if polarity == "require" and display not in hard_prefs:
                hard_prefs.append(display)
            elif polarity == "avoid" and display not in avoid_patterns:
                avoid_patterns.append(display)
            elif polarity == "prefer":
                soft_prefs.append(SoftPreference(memory_id=mid, text=display, confidence=confidence))

        # 7. 渲染有界文本
        rendered = self._render(hard_prefs, soft_prefs, avoid_patterns)
        if len(rendered) > settings.MEMORY_MAX_PACK_CHARS:
            # 裁剪 soft_prefs
            while len(rendered) > settings.MEMORY_MAX_PACK_CHARS and soft_prefs:
                removed = soft_prefs.pop()
                if removed.memory_id in memory_ids:
                    memory_ids.remove(removed.memory_id)
                rendered = self._render(hard_prefs, soft_prefs, avoid_patterns)

        scope_key = f"{category_v2 or 'global'}:{stage.value}"

        # 实验分流
        settings = get_settings()
        exp_id, exp_group = assign_experiment_group(
            user_id, settings.PERSONALIZATION_EXPERIMENT_ENABLED
        )

        pack = MemoryPack(
            user_id=user_id,
            scope_key=scope_key,
            policy=policy,
            memory_items=memory_items,
            hard_preferences=hard_prefs,
            soft_preferences=soft_prefs,
            avoid_patterns=avoid_patterns,
            rendered_text=rendered,
            char_count=len(rendered),
            item_count=len(memory_ids),
            pruned_count=len(items_raw) - len(selected),
            experiment={"experiment_id": exp_id, "group": exp_group} if exp_id else {},
        )

        logger.info(
            "memory pack retrieved: user=%s scope=%s items=%d chars=%d pruned=%d",
            user_id, scope_key, pack.item_count, pack.char_count, pack.pruned_count,
        )

        return pack

    def _filter_policy_conflicts(self, items: list[dict], policy: ProfilePolicy) -> list[dict]:
        """排除与 Policy 冲突的记忆。"""
        policy_avoid = set(p.lower() for p in policy.avoid_patterns)
        result = []
        for item in items:
            display = item.get("display_text", "").lower()
            # 简单冲突检测：如果记忆文本与 Policy avoid_patterns 高度相似
            if any(avoid in display or display in avoid for avoid in policy_avoid):
                continue
            result.append(item)
        return result

    def _relevance_score(
        self,
        item: dict,
        category_v2: str | None,
        template_id: str | None,
        stage: MemoryStage,
    ) -> float:
        """计算相关性评分。"""
        confidence = item.get("confidence", 0.0)
        scope = item.get("scope", {})

        # scope_match
        cat_match = scope.get("category_v2") == category_v2 if category_v2 else False
        tpl_match = scope.get("template_id") == template_id if template_id else False
        stage_match = scope.get("stage") == stage.value if scope.get("stage") else False
        is_global = scope.get("category_v2") is None

        if cat_match and tpl_match and stage_match:
            scope_score = 1.0
        elif cat_match and stage_match:
            scope_score = 0.8
        elif tpl_match and stage_match:
            scope_score = 0.6
        elif is_global and stage_match:
            scope_score = 0.4
        elif is_global:
            scope_score = 0.2
        else:
            scope_score = 0.1

        # recency
        last_seen = item.get("last_seen_at")
        if isinstance(last_seen, datetime):
            age_days = (datetime.now(UTC) - last_seen).total_seconds() / 86400
            recency = max(0, 1.0 - age_days / 90)
        else:
            recency = 0.5

        # outcome_quality
        pos = item.get("positive_outcome_count", 0)
        neg = item.get("negative_outcome_count", 0)
        outcome = (pos + 1) / (pos + neg + 2)

        # explicit confirmation
        confirmed = 1.0 if item.get("confirmed_by_user") else 0.0

        score = (
            0.35 * confidence
            + 0.25 * scope_score
            + 0.15 * recency
            + 0.15 * outcome
            + 0.10 * confirmed
        )
        return score

    def _dedupe_and_limit(self, scored: list[tuple[float, dict]], limit: int) -> list[dict]:
        """按维度去重和限额。"""
        max_per_dim = {
            "required_pattern": 3, "avoid_pattern": 3, "tone": 1, "length": 1,
            "template": 2, "perspective": 2, "revise_direction": 3, "structure": 2,
            "title_style": 1, "content_order": 2,
        }
        dim_counts: dict[str, int] = {}
        result = []

        for score, item in scored:
            dim = item.get("dimension", "")
            if dim_counts.get(dim, 0) >= max_per_dim.get(dim, 2):
                continue
            dim_counts[dim] = dim_counts.get(dim, 0) + 1
            result.append(item)
            if len(result) >= limit:
                break

        return result

    def _render(
        self,
        hard_prefs: list[str],
        soft_prefs: list[SoftPreference],
        avoid_patterns: list[str],
    ) -> str:
        """渲染 Memory Pack 为注入 Prompt 的文本。"""
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
