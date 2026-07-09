"""阶段六反馈、操作记录、风格画像模型与 MongoDB 索引测试。"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _feedback_payload(**overrides):
    payload = {
        "target_type": "draft",
        "target_ref": {"article_url_hash": ARTICLE_HASH, "draft_index": 0},
        "rating": 5,
    }
    payload.update(overrides)
    return payload


class TestFeedbackModels:
    """反馈模型校验。"""

    def test_target_and_action_enums(self):
        from models.feedback import ActionType, TargetType

        assert {item.value for item in TargetType} == {
            "draft",
            "revision",
            "article_score",
            "pipeline",
        }
        assert {item.value for item in ActionType} == {
            "draft_view",
            "draft_download",
            "draft_revise",
            "revision_apply",
            "feedback_submit",
            "pipeline_run",
        }

    def test_feedback_create_valid(self):
        from models.feedback import FeedbackCreate

        feedback = FeedbackCreate(
            **_feedback_payload(
                rating_dimensions={"readability": 4, "product_alignment": 5},
                comment="结构清晰",
                tags=["结构清晰", "角度好"],
            )
        )

        assert feedback.target_type == "draft"
        assert feedback.target_ref.article_url_hash == ARTICLE_HASH
        assert feedback.target_ref.draft_index == 0
        assert feedback.rating == 5
        assert feedback.rating_dimensions == {"readability": 4, "product_alignment": 5}

    @pytest.mark.parametrize("rating", [0, 6])
    def test_feedback_rating_out_of_range(self, rating):
        from models.feedback import FeedbackCreate

        with pytest.raises(ValidationError):
            FeedbackCreate(**_feedback_payload(rating=rating))

    @pytest.mark.parametrize("rating", [0, 6])
    def test_feedback_dimension_rating_out_of_range(self, rating):
        from models.feedback import FeedbackCreate

        with pytest.raises(ValidationError):
            FeedbackCreate(
                **_feedback_payload(rating_dimensions={"readability": rating})
            )

    def test_feedback_invalid_target_type(self):
        from models.feedback import FeedbackCreate

        with pytest.raises(ValidationError):
            FeedbackCreate(**_feedback_payload(target_type="unknown"))

    def test_feedback_invalid_article_hash(self):
        from models.feedback import FeedbackCreate

        with pytest.raises(ValidationError):
            FeedbackCreate(
                **_feedback_payload(target_ref={"article_url_hash": "invalid"})
            )

    def test_feedback_defaults_and_alias(self):
        from models.feedback import Feedback

        feedback = Feedback(_id="mongo-id", **_feedback_payload())

        assert feedback.id == "mongo-id"
        assert feedback.user_id == "local-user"
        assert feedback.status == "active"
        assert feedback.feedback_id
        assert feedback.created_at.tzinfo is UTC
        assert feedback.updated_at.tzinfo is UTC

    def test_feedback_ids_are_unique(self):
        from models.feedback import Feedback

        first = Feedback(**_feedback_payload())
        second = Feedback(**_feedback_payload())

        assert first.feedback_id != second.feedback_id

    def test_feedback_update_is_partial(self):
        from models.feedback import FeedbackUpdate

        update = FeedbackUpdate(comment="更新后的反馈")

        assert update.comment == "更新后的反馈"
        assert update.rating is None
        assert update.status is None


class TestActivityAndProfileModels:
    """操作记录与风格画像模型校验。"""

    def test_user_activity_defaults(self):
        from models.feedback import UserActivity

        activity = UserActivity(
            action="draft_download",
            target={
                "article_url_hash": ARTICLE_HASH,
                "draft_index": 0,
                "template": "爆点A",
            },
            metadata={"file_format": "md"},
        )

        assert activity.action == "draft_download"
        assert activity.user_id == "local-user"
        assert activity.activity_id
        assert activity.context == {}
        assert activity.metadata == {"file_format": "md"}
        assert activity.created_at.tzinfo is UTC

    def test_user_activity_rejects_unknown_action(self):
        from models.feedback import UserActivityCreate

        with pytest.raises(ValidationError):
            UserActivityCreate(
                action="unknown",
                target={"article_url_hash": ARTICLE_HASH},
            )

    def test_pipeline_activity_allows_pipeline_target(self):
        from models.feedback import UserActivityCreate

        activity = UserActivityCreate(
            action="pipeline_run",
            target={"pipeline_id": "pipeline-1"},
        )

        assert activity.target.article_url_hash is None
        assert activity.target.pipeline_id == "pipeline-1"

    def test_non_pipeline_activity_requires_article(self):
        from models.feedback import UserActivityCreate

        with pytest.raises(ValidationError):
            UserActivityCreate(
                action="draft_download",
                target={},
            )

    def test_style_profile_defaults(self):
        from models.feedback import StyleProfile

        profile = StyleProfile()

        assert profile.user_id == "local-user"
        assert profile.style_hints.preferred_length == "medium"
        assert profile.style_hints.preferred_tone == "market_oriented"
        assert profile.preference_scores.template_scores == {}
        assert profile.feedback_summary.total_feedbacks == 0
        assert profile.activity_summary.last_active_at is None
        assert profile.version == 1

    def test_style_profile_nested_metrics(self):
        from models.feedback import StyleProfile

        profile = StyleProfile(
            preference_scores={
                "template_scores": {
                    "爆点A": {
                        "count": 3,
                        "avg_rating": 4.5,
                        "download_count": 2,
                    }
                }
            }
        )

        metric = profile.preference_scores.template_scores["爆点A"]
        assert metric.count == 3
        assert metric.avg_rating == 4.5
        assert metric.download_count == 2

    def test_style_profile_rejects_invalid_average(self):
        from models.feedback import StyleProfile

        with pytest.raises(ValidationError):
            StyleProfile(feedback_summary={"avg_rating": 6})


class TestMongoDBIndexes:
    """阶段六索引初始化测试。"""

    @pytest.mark.asyncio
    async def test_ensure_indexes_creates_expected_indexes(self):
        from db.mongo import MongoDB

        collections: dict[str, MagicMock] = {}

        def get_collection(name):
            collection = MagicMock()

            async def create_indexes(indexes):
                collection.received_indexes = indexes
                return [index.document["name"] for index in indexes]

            collection.create_indexes = AsyncMock(side_effect=create_indexes)
            collections[name] = collection
            return collection

        with patch.object(MongoDB, "get_collection", side_effect=get_collection):
            result = await MongoDB.ensure_indexes()

        assert set(result) == {"feedbacks", "user_activities", "user_profiles"}
        assert len(result["feedbacks"]) == 4
        assert len(result["user_activities"]) == 4
        assert len(result["user_profiles"]) == 2

        feedback_indexes = {
            index.document["name"]: index.document
            for index in collections["feedbacks"].received_indexes
        }
        assert feedback_indexes["idx_feedback_id"]["unique"] is True
        assert list(feedback_indexes["idx_feedback_article_draft"]["key"].items()) == [
            ("target_ref.article_url_hash", 1),
            ("target_ref.draft_index", 1),
        ]

        profile_indexes = {
            index.document["name"]: index.document
            for index in collections["user_profiles"].received_indexes
        }
        assert profile_indexes["idx_profile_user_id"]["unique"] is True

    @pytest.mark.asyncio
    async def test_ensure_indexes_is_repeatable(self):
        from db.mongo import MongoDB

        collection = MagicMock()
        collection.create_indexes = AsyncMock(return_value=["existing-index"])

        with patch.object(MongoDB, "get_collection", return_value=collection):
            first = await MongoDB.ensure_indexes()
            second = await MongoDB.ensure_indexes()

        assert first == second
        assert collection.create_indexes.await_count == 6
