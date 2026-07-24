"""TemplateRanker：在合法候选模板内按用户偏好排序。

排序公式：
template_score =
    0.40 × category_match
  + 0.25 × historical_rating
  + 0.15 × apply_rate
  + 0.10 × download_rate
  + 0.10 × memory_preference
  - 0.15 × negative_feedback_rate
  - 0.10 × excessive_revision_rate

冷启动：没有用户数据时保持系统原顺序。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("backend.agent.template_ranker")


class TemplateRanker:
    """在 TemplateRepository.resolve() 返回的合法候选内排序。"""

    def __init__(self, db: Any):
        self.db = db

    async def rank(
        self,
        user_id: str,
        category_v2: str,
        candidates: list[Any],
    ) -> list[Any]:
        """对合法候选模板按用户偏好排序。

        Args:
            user_id: 用户 ID
            category_v2: 文章分类
            candidates: TemplateRepository.resolve() 返回的合法候选列表

        Returns:
            排序后的候选列表（不改变候选集合，只排序）
        """
        if not candidates or len(candidates) <= 1:
            return candidates

        # 查询用户历史数据
        stats = await self._load_template_stats(user_id, category_v2)

        if not stats:
            # 冷启动：保持原顺序
            return candidates

        scored = []
        for tpl in candidates:
            tpl_id = getattr(tpl, "template_id", None) or getattr(tpl, "name", "")
            stat = stats.get(tpl_id, {})
            score = self._compute_score(stat)
            scored.append((score, tpl))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [tpl for _, tpl in scored]

    async def _load_template_stats(self, user_id: str, category_v2: str) -> dict[str, dict]:
        """从 generation_runs 和 user_drafts 加载模板统计。"""
        stats: dict[str, dict] = {}

        # 从 user_drafts 查询历史草稿
        cursor = self.db["user_drafts"].find({
            "user_id": user_id,
            "category_v2": category_v2,
        })
        drafts = await cursor.to_list(length=200)

        for draft in drafts:
            tpl_id = draft.get("template_id") or draft.get("template", "")
            if not tpl_id:
                continue
            if tpl_id not in stats:
                stats[tpl_id] = {
                    "total": 0, "ratings": [], "applied": 0,
                    "downloaded": 0, "negative_feedback": 0,
                    "revisions": 0, "independent_tasks": set(),
                }
            s = stats[tpl_id]
            s["total"] += 1
            s["ratings"].append(draft.get("feedback_rating", 0))
            if draft.get("revision_applied"):
                s["applied"] += 1
            if draft.get("downloaded"):
                s["downloaded"] += 1
            if draft.get("revision_requested"):
                s["revisions"] += 1
            if draft.get("feedback_rating") and draft["feedback_rating"] <= 2:
                s["negative_feedback"] += 1
            article_hash = draft.get("article_url_hash", "")
            if article_hash:
                s["independent_tasks"].add(article_hash)

        # 计算独立任务数
        for s in stats.values():
            s["independent_task_count"] = len(s.pop("independent_tasks", set()))

        return stats

    def _compute_score(self, stat: dict) -> float:
        """计算模板排序分数。"""
        total = stat.get("total", 0)
        if total == 0:
            return 0.5  # 默认分数

        independent = stat.get("independent_task_count", 0)

        # 冷启动检查
        memory_weight = 0.05 if independent < 3 else 0.10

        ratings = stat.get("ratings", [])
        avg_rating = sum(ratings) / len(ratings) if ratings else 0
        historical_rating = avg_rating / 5.0  # 归一化到 [0, 1]

        apply_rate = stat.get("applied", 0) / total
        download_rate = stat.get("downloaded", 0) / total
        neg_rate = stat.get("negative_feedback", 0) / total
        revision_rate = stat.get("revisions", 0) / total

        # memory_preference 暂无独立信号，使用 apply_rate 作为代理
        memory_pref = apply_rate * 0.5

        score = (
            0.40 * 1.0  # category_match（所有候选都已匹配分类）
            + 0.25 * historical_rating
            + 0.15 * apply_rate
            + 0.10 * download_rate
            + memory_weight * memory_pref
            - 0.15 * neg_rate
            - 0.10 * revision_rate
        )

        return max(0.0, min(1.0, score))
