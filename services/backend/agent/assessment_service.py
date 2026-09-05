"""用户级文章评估服务 - 读写 user_article_assessments。

替代直接写入共享 articles 集合的个性化字段。
实现多租户隔离：每个用户对每篇文章有独立的分类和评分结果。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from models.article_assessment import (
    ClassificationAssessment,
    ProductSnapshot,
    PromptRefSnapshot,
    ScoringAssessment,
    UserArticleAssessment,
)
from models.generation_config import ScoreMode

logger = logging.getLogger("backend.assessment_service")


class AssessmentService:
    """用户级文章评估读写服务。"""

    def __init__(self, db):
        self._db = db

    async def get_assessment(
        self,
        user_id: str,
        article_url_hash: str,
    ) -> UserArticleAssessment | None:
        """获取当前用户对文章的评估，不存在返回 None。"""
        doc = await self._db["user_article_assessments"].find_one(
            {"user_id": user_id, "article_url_hash": article_url_hash}
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return UserArticleAssessment(**doc)

    async def get_assessment_by_fingerprint(
        self,
        user_id: str,
        article_url_hash: str,
        input_fingerprint: str,
    ) -> UserArticleAssessment | None:
        """按输入指纹查找已有评估（幂等复用）。"""
        doc = await self._db["user_article_assessments"].find_one(
            {
                "user_id": user_id,
                "article_url_hash": article_url_hash,
                "input_fingerprint": input_fingerprint,
            }
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return UserArticleAssessment(**doc)

    async def upsert_classification(
        self,
        user_id: str,
        article_url_hash: str,
        classification: ClassificationAssessment,
        *,
        prompt_refs: list[dict] | None = None,
        input_fingerprint: str = "",
    ) -> UserArticleAssessment:
        """写入或更新分类结果。"""
        now = datetime.now(UTC)
        existing = await self.get_assessment(user_id, article_url_hash)

        if existing is None:
            assessment = UserArticleAssessment(
                assessment_id=f"assess-{uuid4()}",
                user_id=user_id,
                article_url_hash=article_url_hash,
                version=1,
                classification=classification,
                prompt_refs=[PromptRefSnapshot(**ref) for ref in (prompt_refs or [])],
                input_fingerprint=input_fingerprint,
                created_at=now,
                updated_at=now,
            )
            await self._db["user_article_assessments"].insert_one(assessment.model_dump())
            return assessment

        # 更新现有评估的分类部分
        await self._db["user_article_assessments"].update_one(
            {"user_id": user_id, "article_url_hash": article_url_hash},
            {
                "$set": {
                    "classification": classification.model_dump(),
                    "input_fingerprint": input_fingerprint,
                    "updated_at": now,
                    "version": existing.version + 1,
                    **({"prompt_refs": prompt_refs} if prompt_refs is not None else {}),
                }
            },
        )
        existing.classification = classification
        existing.input_fingerprint = input_fingerprint
        existing.updated_at = now
        existing.version += 1
        return existing

    async def upsert_scoring(
        self,
        user_id: str,
        article_url_hash: str,
        scoring: ScoringAssessment,
        *,
        product_snapshot: dict | None = None,
        prompt_refs: list[dict] | None = None,
        input_fingerprint: str = "",
    ) -> UserArticleAssessment:
        """写入或更新评分结果。"""
        now = datetime.now(UTC)
        existing = await self.get_assessment(user_id, article_url_hash)

        update_set: dict[str, Any] = {
            "scoring": scoring.model_dump(),
            "input_fingerprint": input_fingerprint,
            "updated_at": now,
        }
        if product_snapshot is not None:
            update_set["product_snapshot"] = product_snapshot
        if prompt_refs is not None:
            update_set["prompt_refs"] = prompt_refs

        if existing is None:
            assessment = UserArticleAssessment(
                assessment_id=f"assess-{uuid4()}",
                user_id=user_id,
                article_url_hash=article_url_hash,
                version=1,
                scoring=scoring,
                product_snapshot=ProductSnapshot(**(product_snapshot or {})),
                prompt_refs=[PromptRefSnapshot(**ref) for ref in (prompt_refs or [])],
                input_fingerprint=input_fingerprint,
                created_at=now,
                updated_at=now,
            )
            await self._db["user_article_assessments"].insert_one(assessment.model_dump())
            return assessment

        await self._db["user_article_assessments"].update_one(
            {"user_id": user_id, "article_url_hash": article_url_hash},
            {
                "$set": update_set,
                "$inc": {"version": 1},
            },
        )
        existing.scoring = scoring
        existing.input_fingerprint = input_fingerprint
        existing.updated_at = now
        existing.version += 1
        return existing

    async def list_by_user(
        self,
        user_id: str,
        *,
        category: str | None = None,
        min_score: int | None = None,
        limit: int = 50,
        skip: int = 0,
    ) -> list[UserArticleAssessment]:
        """查询当前用户的评估列表。"""
        query: dict[str, Any] = {"user_id": user_id}
        if category is not None:
            query["classification.category_v2"] = category
        if min_score is not None:
            query["scoring.candidate_score"] = {"$gte": min_score}

        cursor = (
            self._db["user_article_assessments"]
            .find(query)
            .sort("updated_at", -1)
            .skip(skip)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
        results = []
        for doc in docs:
            doc.pop("_id", None)
            results.append(UserArticleAssessment(**doc))
        return results

    async def get_hot_articles(
        self,
        user_id: str,
        *,
        limit: int = 10,
    ) -> list[UserArticleAssessment]:
        """获取当前用户的热点文章排行。"""
        cursor = (
            self._db["user_article_assessments"]
            .find({"user_id": user_id, "scoring.is_pr_candidate": True})
            .sort("scoring.candidate_score", -1)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
        results = []
        for doc in docs:
            doc.pop("_id", None)
            results.append(UserArticleAssessment(**doc))
        return results

    @staticmethod
    def compute_scoring_result(
        *,
        product_relevance: int | None,
        event_impact: int,
        product_relevance_enabled: bool,
        threshold: int,
    ) -> ScoringAssessment:
        """计算评分结果（含 candidate_score 和 score_mode）。

        product_relevance_enabled=False 时：
        - product_relevance 返回 None（不是伪 0）
        - candidate_score = event_impact（0-100 范围）
        - score_mode = event_only

        product_relevance_enabled=True 时：
        - candidate_score = product_relevance + event_impact（0-200 范围）
        - score_mode = product_event
        """
        if product_relevance_enabled:
            pr = product_relevance or 0
            return ScoringAssessment(
                score_mode=ScoreMode.PRODUCT_EVENT.value,
                product_relevance=product_relevance,
                event_impact=event_impact,
                candidate_score=pr + event_impact,
                candidate_threshold=threshold,
                is_pr_candidate=(pr + event_impact) >= threshold,
            )
        return ScoringAssessment(
            score_mode=ScoreMode.EVENT_ONLY.value,
            product_relevance=None,
            event_impact=event_impact,
            candidate_score=event_impact,
            candidate_threshold=threshold,
            is_pr_candidate=event_impact >= threshold,
        )
