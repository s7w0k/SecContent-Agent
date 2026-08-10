"""阶段六反馈、操作记录、风格画像模型与 MongoDB 索引测试。"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from pymongo.errors import OperationFailure

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
            "article_upload",
            "web_search",
            "search_result_import",
            "search_content_retry",
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
            FeedbackCreate(**_feedback_payload(rating_dimensions={"readability": rating}))

    def test_feedback_invalid_target_type(self):
        from models.feedback import FeedbackCreate

        with pytest.raises(ValidationError):
            FeedbackCreate(**_feedback_payload(target_type="unknown"))

    def test_feedback_invalid_article_hash(self):
        from models.feedback import FeedbackCreate

        with pytest.raises(ValidationError):
            FeedbackCreate(**_feedback_payload(target_ref={"article_url_hash": "invalid"}))

    def test_feedback_defaults_and_alias(self):
        from models.feedback import Feedback

        feedback = Feedback(
            _id="mongo-id",
            user_id="test-user",
            template_id="tpl-user-breaking-a",
            template_key="breaking_a",
            template_version=3,
            template_name="我的模板",
            **_feedback_payload(),
        )

        assert feedback.id == "mongo-id"
        assert feedback.user_id == "test-user"
        assert feedback.status == "active"
        assert feedback.feedback_id
        assert feedback.template_id == "tpl-user-breaking-a"
        assert feedback.template_key == "breaking_a"
        assert feedback.template_version == 3
        assert feedback.template_name == "我的模板"
        assert feedback.created_at.tzinfo is UTC
        assert feedback.updated_at.tzinfo is UTC

    def test_feedback_ids_are_unique(self):
        from models.feedback import Feedback

        first = Feedback(user_id="test-user", **_feedback_payload())
        second = Feedback(user_id="test-user", **_feedback_payload())

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
            user_id="test-user",
            action="draft_download",
            target={
                "article_url_hash": ARTICLE_HASH,
                "draft_index": 0,
                "template": "爆点A",
                "template_id": "tpl-user-breaking-a",
                "template_key": "breaking_a",
                "template_version": 3,
                "template_name": "爆点A",
            },
            metadata={"file_format": "md"},
        )

        assert activity.action == "draft_download"
        assert activity.user_id == "test-user"
        assert activity.activity_id
        assert activity.target.template_id == "tpl-user-breaking-a"
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

        profile = StyleProfile(user_id="test-user")

        assert profile.user_id == "test-user"
        assert profile.style_hints.preferred_length == "medium"
        assert profile.style_hints.preferred_tone == "market_oriented"
        assert profile.preference_scores.template_scores == {}
        assert profile.feedback_summary.total_feedbacks == 0
        assert profile.activity_summary.last_active_at is None
        assert profile.version == 1

    def test_style_profile_nested_metrics(self):
        from models.feedback import StyleProfile

        profile = StyleProfile(
            user_id="test-user",
            preference_scores={
                "template_scores": {
                    "爆点A": {
                        "count": 3,
                        "avg_rating": 4.5,
                        "download_count": 2,
                    }
                }
            },
        )

        metric = profile.preference_scores.template_scores["爆点A"]
        assert metric.count == 3
        assert metric.avg_rating == 4.5
        assert metric.download_count == 2

    def test_style_profile_rejects_invalid_average(self):
        from models.feedback import StyleProfile

        with pytest.raises(ValidationError):
            StyleProfile(user_id="test-user", feedback_summary={"avg_rating": 6})

    @pytest.mark.parametrize("model_name", ["Feedback", "UserActivity", "StyleProfile"])
    def test_user_id_is_required(self, model_name):
        from models import feedback as feedback_models

        model = getattr(feedback_models, model_name)
        payload = {
            "Feedback": _feedback_payload(),
            "UserActivity": {
                "action": "draft_download",
                "target": {"article_url_hash": ARTICLE_HASH},
            },
            "StyleProfile": {},
        }[model_name]
        with pytest.raises(ValidationError):
            model(**payload)


class TestMultiTenantModels:
    def test_chat_session_and_user_draft(self):
        from models.feedback import ChatSession, UserDraft

        session = ChatSession(
            user_id="user-a",
            article_url_hash=ARTICLE_HASH,
            draft_index=0,
        )
        user_draft = UserDraft(
            user_id="user-a",
            article_url_hash=ARTICLE_HASH,
            drafts=[{"title": "草稿"}],
        )

        assert session.messages == []
        assert session.created_at.tzinfo is UTC
        assert user_draft.drafts[0]["title"] == "草稿"
        assert user_draft.updated_at.tzinfo is UTC

    def test_pipeline_lock_defaults_to_five_minutes(self):
        from models.feedback import PipelineLock

        lock = PipelineLock(
            lock_key="crawl-wewe-2026-07-10",
            lock_type="crawl",
            user_id="user-a",
        )

        assert lock.status == "running"
        assert 295 <= (lock.expires_at - lock.created_at).total_seconds() <= 305

    def test_pipeline_task_defaults_to_one_hour(self):
        from models.feedback import PipelineTask

        task = PipelineTask(user_id="user-a", task_type="run-v2")

        assert task.task_id.startswith("task-")
        assert task.status == "pending"
        assert task.progress.current == 0
        assert 3595 <= (task.expires_at - task.created_at).total_seconds() <= 3605

    def test_pipeline_log_requires_user(self):
        from models.feedback import PipelineLog

        with pytest.raises(ValidationError):
            PipelineLog(
                level="INFO",
                phase="crawl",
                message="started",
                date="2026-07-10",
            )


class TestMongoDBIndexes:
    """阶段六索引初始化测试。"""

    @pytest.mark.asyncio
    async def test_ensure_indexes_creates_expected_indexes(self):
        from db.mongo import MongoDB

        collections: dict[str, MagicMock] = {}

        def get_collection(name):
            if name in collections:
                return collections[name]
            collection = MagicMock()

            async def create_indexes(indexes):
                collection.received_indexes = indexes
                return [index.document["name"] for index in indexes]

            collection.create_indexes = AsyncMock(side_effect=create_indexes)
            collection.drop_index = AsyncMock()
            collections[name] = collection
            return collection

        with patch.object(MongoDB, "get_collection", side_effect=get_collection):
            result = await MongoDB.ensure_indexes()

        assert set(result) == {
            "users",
            "feedbacks",
            "user_activities",
            "user_profiles",
            "chat_sessions",
            "user_drafts",
            "user_pr_templates",
            "user_pr_template_versions",
            "user_prompts",
            "user_prompt_versions",
            "user_generation_preferences",
            "user_article_assessments",
            "pipeline_locks",
            "pipeline_tasks",
            "pipeline_logs",
            "llm_call_logs",
            "agent_run_events",
            "execution_runs",
            "execution_events",
            "execution_links",
            "execution_step_ledger",
            "ledger_repair_queue",
            "pipeline_events",
            "user_profile_policies",
            "user_memory_events",
            "user_memory_items",
            "user_memory_summaries",
            "generation_runs",
            "personalization_candidates",
            "personalization_feedbacks",
            "knowledge_drafts",
            "knowledge_revisions",
            "knowledge_publications",
            "knowledge_publish_locks",
            "knowledge_audit_logs",
            "search_sessions",
            "search_import_batches",
            "search_import_items",
            "articles",
            "crawl_runs",
            "user_article_scores",
            "user_knowledge_entries",
            "user_products",
        }
        assert len(result["users"]) == 3
        assert len(result["feedbacks"]) == 4
        assert len(result["user_activities"]) == 4
        assert len(result["user_profiles"]) == 2
        assert len(result["chat_sessions"]) == 1
        assert len(result["user_drafts"]) == 2
        assert len(result["user_prompts"]) == 1
        assert len(result["pipeline_locks"]) == 2
        assert len(result["pipeline_tasks"]) == 4
        assert len(result["pipeline_logs"]) == 5
        assert len(result["llm_call_logs"]) == 7
        assert len(result["agent_run_events"]) == 4
        assert len(result["execution_runs"]) == 6
        assert len(result["execution_events"]) == 7
        assert len(result["execution_links"]) == 4
        assert len(result["execution_step_ledger"]) == 6
        assert len(result["ledger_repair_queue"]) == 2

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

        llm_indexes = {
            index.document["name"]: index.document
            for index in collections["llm_call_logs"].received_indexes
        }
        assert llm_indexes["idx_llm_call_id"]["unique"] is True
        assert llm_indexes["idx_llm_expires"]["expireAfterSeconds"] == 0

        user_indexes = {
            index.document["name"]: index.document
            for index in collections["users"].received_indexes
        }
        assert user_indexes["idx_user_id"]["unique"] is True
        assert user_indexes["idx_user_username"]["unique"] is True
        assert user_indexes["idx_user_email"]["sparse"] is True

        chat_indexes = {
            index.document["name"]: index.document
            for index in collections["chat_sessions"].received_indexes
        }
        assert chat_indexes["idx_chat_user_article_draft"]["unique"] is True
        collections["chat_sessions"].drop_index.assert_awaited_once_with(
            "article_url_hash_1_draft_index_1"
        )

        prompt_indexes = {
            index.document["name"]: index.document
            for index in collections["user_prompts"].received_indexes
        }
        assert prompt_indexes["uq_user_prompt_key"]["unique"] is True

        lock_indexes = {
            index.document["name"]: index.document
            for index in collections["pipeline_locks"].received_indexes
        }
        assert lock_indexes["idx_pipeline_lock_key"]["unique"] is True
        assert lock_indexes["idx_pipeline_lock_expires"]["expireAfterSeconds"] == 0

        task_indexes = {
            index.document["name"]: index.document
            for index in collections["pipeline_tasks"].received_indexes
        }
        assert task_indexes["idx_pipeline_task_id"]["unique"] is True
        assert task_indexes["idx_pipeline_task_expires"]["expireAfterSeconds"] == 0
        assert task_indexes["idx_pipeline_task_thread_id"]["sparse"] is True

        pipeline_log_indexes = {
            index.document["name"]: index.document
            for index in collections["pipeline_logs"].received_indexes
        }
        assert pipeline_log_indexes["idx_pipeline_log_trace_created"]
        assert pipeline_log_indexes["idx_pipeline_log_phase_date"]
        assert pipeline_log_indexes["idx_pipeline_log_level_date"]
        assert pipeline_log_indexes["idx_pipeline_log_date_created"]

    @pytest.mark.asyncio
    async def test_ensure_indexes_is_repeatable(self):
        from db.mongo import MongoDB

        collection = MagicMock()
        collection.create_indexes = AsyncMock(return_value=["existing-index"])
        collection.drop_index = AsyncMock(side_effect=OperationFailure("index not found", code=27))

        with patch.object(MongoDB, "get_collection", return_value=collection):
            first = await MongoDB.ensure_indexes()
            second = await MongoDB.ensure_indexes()

        assert first == second
        assert collection.create_indexes.await_count == 86
        assert collection.drop_index.await_count == 2
