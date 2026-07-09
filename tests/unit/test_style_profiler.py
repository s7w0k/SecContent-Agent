"""StyleProfiler 单元测试。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agent.style_profiler import StyleProfiler, _parse_json_object, _response_text
from langchain_core.messages import AIMessage
from models.feedback import StyleProfile

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _matches(document: dict, query: dict) -> bool:
    return all(document.get(key) == value for key, value in query.items())


class FakeCursor:
    def __init__(self, documents: list[dict]):
        self.documents = documents

    async def to_list(self, length=None):
        items = deepcopy(self.documents)
        return items if length is None else items[:length]


class FakeCollection:
    def __init__(self, documents: list[dict] | None = None):
        self.documents = deepcopy(documents or [])
        self.replace_calls: list[tuple[dict, dict, bool]] = []

    def find(self, query: dict):
        return FakeCursor([item for item in self.documents if _matches(item, query)])

    async def find_one(self, query: dict):
        return next(
            (deepcopy(item) for item in self.documents if _matches(item, query)),
            None,
        )

    async def replace_one(self, query: dict, document: dict, upsert: bool = False):
        self.replace_calls.append((deepcopy(query), deepcopy(document), upsert))
        for index, current in enumerate(self.documents):
            if _matches(current, query):
                self.documents[index] = deepcopy(document)
                break
        else:
            if upsert:
                self.documents.append(deepcopy(document))
        return SimpleNamespace(modified_count=1)


class FakeDatabase:
    def __init__(self, **collections):
        self.collections = {
            name: FakeCollection(documents) for name, documents in collections.items()
        }

    def __getitem__(self, name: str):
        return self.collections.setdefault(name, FakeCollection())


@pytest.fixture
def sample_db():
    now = datetime.now(UTC)
    return FakeDatabase(
        feedbacks=[
            {
                "feedback_id": "fb-1",
                "user_id": "local-user",
                "status": "active",
                "target_ref": {
                    "article_url_hash": ARTICLE_HASH,
                    "draft_index": 0,
                },
                "rating": 5,
                "tags": ["角度好", "传播性强"],
            },
            {
                "feedback_id": "fb-2",
                "user_id": "local-user",
                "status": "active",
                "target_ref": {
                    "article_url_hash": ARTICLE_HASH,
                    "draft_index": 1,
                },
                "rating": 2,
                "tags": ["技术细节过多", "角度好"],
            },
            {
                "feedback_id": "archived",
                "user_id": "local-user",
                "status": "archived",
                "target_ref": {"article_url_hash": ARTICLE_HASH, "draft_index": 0},
                "rating": 1,
                "tags": [],
            },
        ],
        articles=[
            {
                "url_hash": ARTICLE_HASH,
                "pr_drafts": [
                    {"template": "爆点A", "perspective": "市场传播视角"},
                    {"template": "爆点B", "perspective": "技术深度视角"},
                ],
            },
        ],
        user_activities=[
            {
                "user_id": "local-user",
                "action": "draft_download",
                "target": {"template": "爆点A", "perspective": "市场传播视角"},
                "created_at": now - timedelta(days=1),
            },
            {
                "user_id": "local-user",
                "action": "revision_apply",
                "target": {"template": "爆点A", "perspective": "市场传播视角"},
                "created_at": now,
            },
            {
                "user_id": "local-user",
                "action": "draft_revise",
                "target": {"template": "爆点B", "perspective": "技术深度视角"},
                "created_at": now - timedelta(hours=1),
            },
            {
                "user_id": "local-user",
                "action": "feedback_submit",
                "target": {"template": "爆点A"},
                "created_at": now,
            },
        ],
        chat_sessions=[
            {
                "messages": [
                    {"role": "user", "content": "减少技术细节"},
                    {"role": "assistant", "content": "已修改"},
                    {"role": "user", "content": "标题更有冲击力"},
                    {"role": "user", "content": "减少技术细节"},
                ],
            },
            {
                "messages": [
                    {"role": "user", "content": "补充客户案例"},
                    {"role": "user", "content": "   "},
                ],
            },
        ],
        user_profiles=[],
    )


@pytest.fixture
def llm():
    mock = AsyncMock()
    mock.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="""```json
{
  "common_revise_directions": ["减少技术细节", "增强传播性", "减少技术细节"],
  "avoid_patterns": ["过多代码片段"],
  "preferred_tone": "market_oriented",
  "preferred_length": "short",
  "analysis": "用户偏好简洁且有传播力的稿件。"
}
```""",
        ),
    )
    return mock


class TestAggregation:
    @pytest.mark.asyncio
    async def test_aggregate_feedbacks(self, sample_db, llm):
        profiler = StyleProfiler(llm, sample_db)
        result = await profiler.aggregate_feedbacks()

        assert result["summary"] == {
            "total_feedbacks": 2,
            "avg_rating": 3.5,
            "positive_count": 1,
            "negative_count": 1,
            "neutral_count": 0,
            "top_tags": ["角度好", "传播性强", "技术细节过多"],
        }
        assert result["items"][0]["template"] == "爆点A"
        assert result["items"][1]["perspective"] == "技术深度视角"

    @pytest.mark.asyncio
    async def test_aggregate_feedbacks_handles_missing_article_and_rating(self):
        db = FakeDatabase(
            feedbacks=[
                {
                    "user_id": "local-user",
                    "status": "active",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 8,
                    },
                    "tags": [],
                }
            ],
            articles=[],
        )
        result = await StyleProfiler(None, db).aggregate_feedbacks()

        assert result["summary"]["avg_rating"] == 0
        assert "template" not in result["items"][0]

    @pytest.mark.asyncio
    async def test_aggregate_activities_and_instructions(self, sample_db, llm):
        profiler = StyleProfiler(llm, sample_db)
        activities = await profiler.aggregate_activities()
        instructions = await profiler.aggregate_revise_instructions()

        assert activities["summary"]["total_downloads"] == 1
        assert activities["summary"]["total_applies"] == 1
        assert activities["summary"]["total_revises"] == 1
        assert activities["summary"]["total_feedbacks"] == 1
        assert activities["summary"]["last_active_at"] is not None
        assert instructions == ["减少技术细节", "标题更有冲击力", "补充客户案例"]

    @pytest.mark.asyncio
    async def test_empty_activity_summary_and_non_local_session_query(self):
        db = FakeDatabase(user_activities=[], chat_sessions=[])
        profiler = StyleProfiler(None, db)

        result = await profiler.aggregate_activities("another-user")
        instructions = await profiler.aggregate_revise_instructions("another-user")

        assert result["summary"]["last_active_at"] is None
        assert instructions == []


class TestPreferenceRules:
    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ((2, 4.5, 1, 1, 1), 18.0),
            ((1, 1.0, 0, 0, 0), -6.0),
            ((1, 1.0, 1, 0, 0), 0),
            ((1, 3.0, 0, 0, 1), 4.0),
            ((0, 0, 2, 0, 0), 6.0),
        ],
    )
    def test_calculate_preference_score(self, args, expected):
        assert StyleProfiler.calculate_preference_score(*args) == expected

    @pytest.mark.asyncio
    async def test_calculate_scores_and_rank(self, sample_db, llm):
        profiler = StyleProfiler(llm, sample_db)
        feedbacks = (await profiler.aggregate_feedbacks())["items"]
        activities = (await profiler.aggregate_activities())["items"]
        scores = profiler.calculate_preference_scores(feedbacks, activities)

        assert scores["template_scores"]["爆点A"]["avg_rating"] == 5
        assert scores["template_scores"]["爆点A"]["download_count"] == 1
        assert scores["template_scores"]["爆点A"]["apply_count"] == 1
        assert scores["template_scores"]["爆点B"]["revise_count"] == 1
        assert profiler._rank_preferences(scores["template_scores"])[0] == "爆点A"
        assert (
            profiler._rank_preferences(
                {
                    "不喜欢": {
                        "count": 1,
                        "avg_rating": 1,
                        "download_count": 0,
                        "apply_count": 0,
                        "revise_count": 0,
                    }
                }
            )
            == []
        )


class TestLLMAnalysis:
    def test_response_and_json_parser_guardrails(self):
        response = SimpleNamespace(content=[{"text": '{"ok":'}, " true}"])

        assert _response_text(response) == '{"ok": true}'
        assert _parse_json_object(_response_text(response)) == {"ok": True}
        with pytest.raises(ValueError, match="does not contain"):
            _parse_json_object("not json")
        with (
            patch("agent.style_profiler.json.loads", return_value=[]),
            pytest.raises(ValueError, match="must be"),
        ):
            _parse_json_object("{}")

    @pytest.mark.asyncio
    async def test_extract_patterns_success(self, sample_db, llm):
        result = await StyleProfiler(llm, sample_db).extract_revise_patterns(
            ["指令一", "指令二", "指令三"],
        )

        assert result["common_revise_directions"] == ["减少技术细节", "增强传播性"]
        assert result["preferred_length"] == "short"
        assert result["preferred_tone"] == "market_oriented"
        llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extract_patterns_skips_llm_for_insufficient_samples(self, sample_db, llm):
        result = await StyleProfiler(llm, sample_db).extract_revise_patterns(["一", "二"])

        assert result["common_revise_directions"] == []
        assert result["preferred_length"] == "medium"
        llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_patterns_invalid_values_use_defaults(self, sample_db, llm):
        llm.ainvoke.return_value = AIMessage(
            content='{"preferred_tone":"casual","preferred_length":"huge","analysis":12}',
        )
        result = await StyleProfiler(llm, sample_db).extract_revise_patterns(["一", "二", "三"])

        assert result["preferred_tone"] == "market_oriented"
        assert result["preferred_length"] == "medium"
        assert result["analysis"] == "12"

    @pytest.mark.asyncio
    async def test_extract_patterns_llm_failure_falls_back(self, sample_db, llm):
        llm.ainvoke.side_effect = RuntimeError("network error")
        result = await StyleProfiler(llm, sample_db).extract_revise_patterns(["一", "二", "三"])

        assert result["analysis"] == ""
        assert result["avoid_patterns"] == []

    @pytest.mark.asyncio
    async def test_analyze_feedback_success(self, sample_db, llm):
        llm.ainvoke.return_value = AIMessage(content='{"tags":["角度好","角度好","传播性强"]}')
        result = await StyleProfiler(llm, sample_db).analyze_feedback(
            {"rating": 5, "comment": "很好"},
        )

        assert result == ["角度好", "传播性强"]

    @pytest.mark.asyncio
    async def test_analyze_feedback_fallback(self, sample_db):
        result = await StyleProfiler(None, sample_db).analyze_feedback(
            {"rating": 2, "tags": ["技术细节过多"]},
        )

        assert result == ["技术细节过多", "负向反馈"]


class TestProfileBuild:
    @pytest.mark.asyncio
    async def test_build_profile_and_persist(self, sample_db, llm):
        profiler = StyleProfiler(llm, sample_db)
        profile = await profiler.build_profile()

        assert profile["style_hints"]["preferred_templates"][0] == "爆点A"
        assert profile["style_hints"]["preferred_perspectives"][0] == "市场传播视角"
        assert profile["style_hints"]["common_revise_directions"] == [
            "减少技术细节",
            "增强传播性",
        ]
        assert profile["feedback_summary"]["total_feedbacks"] == 2
        assert profile["revise_instruction_patterns"][0] == {
            "pattern": "减少技术细节",
            "count": 1,
        }
        stored = sample_db["user_profiles"].documents[0]
        assert stored["user_id"] == "local-user"
        assert stored["version"] == 1

    @pytest.mark.asyncio
    async def test_build_profile_increments_version(self, sample_db, llm):
        created_at = datetime.now(UTC) - timedelta(days=10)
        sample_db["user_profiles"].documents.append(
            {
                "user_id": "local-user",
                "version": 4,
                "created_at": created_at,
            },
        )

        profile = await StyleProfiler(llm, sample_db).build_profile()

        assert profile["version"] == 5
        assert datetime.fromisoformat(profile["created_at"]) == created_at
        assert sample_db["user_profiles"].replace_calls[0][2] is True

    def test_get_style_hints_from_dict_and_model(self, sample_db, llm):
        profiler = StyleProfiler(llm, sample_db)
        profile = StyleProfile(
            style_hints={
                "preferred_templates": ["爆点A"],
                "preferred_perspectives": ["市场传播视角"],
                "preferred_tone": "executive",
                "preferred_length": "short",
                "common_revise_directions": ["增强传播性"],
                "avoid_patterns": ["代码片段"],
            },
        )

        prompt = profiler.get_style_hints(profile)
        empty_prompt = profiler.get_style_hints({})

        assert "偏好模板：爆点A" in prompt
        assert "偏好语气：executive" in prompt
        assert "不要牺牲事实准确性" in prompt
        assert "暂无明确偏好" in empty_prompt
