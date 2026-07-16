"""两用户认证数据隔离回归测试。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.activity import list_activities
from api.chat import get_chat_history
from api.dashboard import _attach_user_drafts
from api.feedback import list_feedback
from api.logs import get_logs_by_date
from api.profile import get_style_profile
from models.feedback import FeedbackStatus

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


class Cursor:
    def __init__(self, documents: list[dict]):
        self.documents = deepcopy(documents)

    async def to_list(self, length=None):
        return deepcopy(self.documents if length is None else self.documents[:length])

    def sort(self, _field, _direction):
        return self

    def limit(self, limit):
        self.documents = self.documents[:limit]
        return self

    def __aiter__(self):
        async def iterate():
            for document in self.documents:
                yield deepcopy(document)

        return iterate()


def request_with_db(db):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))


def user_scoped_collection(documents: dict[str, list[dict]]):
    collection = MagicMock()
    collection.find.side_effect = lambda query: Cursor(documents.get(query["user_id"], []))
    return collection


@pytest.mark.asyncio
async def test_feedback_is_isolated_when_switching_users():
    collection = user_scoped_collection(
        {
            "user-a": [{"_id": "fa", "user_id": "user-a", "rating": 5, "status": "active"}],
            "user-b": [{"_id": "fb", "user_id": "user-b", "rating": 2, "status": "active"}],
        }
    )
    db = {"feedbacks": collection}

    result_a = await list_feedback(
        request_with_db(db), None, None, None, FeedbackStatus.ACTIVE, 1, 20, "user-a"
    )
    result_b = await list_feedback(
        request_with_db(db), None, None, None, FeedbackStatus.ACTIVE, 1, 20, "user-b"
    )

    assert [item["user_id"] for item in result_a["data"]["items"]] == ["user-a"]
    assert [item["user_id"] for item in result_b["data"]["items"]] == ["user-b"]


@pytest.mark.asyncio
async def test_activity_is_isolated_when_switching_users():
    collection = user_scoped_collection(
        {
            "user-a": [{"_id": "aa", "user_id": "user-a", "action": "draft_download"}],
            "user-b": [{"_id": "ab", "user_id": "user-b", "action": "revision_apply"}],
        }
    )
    db = {"user_activities": collection}

    result_a = await list_activities(request_with_db(db), None, None, 1, 20, "user-a")
    result_b = await list_activities(request_with_db(db), None, None, 1, 20, "user-b")

    assert [item["user_id"] for item in result_a["data"]["items"]] == ["user-a"]
    assert [item["user_id"] for item in result_b["data"]["items"]] == ["user-b"]


@pytest.mark.asyncio
async def test_profile_is_isolated_when_switching_users():
    profiles = {
        "user-a": {"_id": "pa", "user_id": "user-a", "style_hints": {"tone": "formal"}},
        "user-b": {"_id": "pb", "user_id": "user-b", "style_hints": {"tone": "direct"}},
    }
    collection = MagicMock()
    collection.find_one = AsyncMock(side_effect=lambda query: profiles.get(query["user_id"]))
    db = {"user_profiles": collection}

    result_a = await get_style_profile(request_with_db(db), "user-a")
    result_b = await get_style_profile(request_with_db(db), "user-b")

    assert result_a["data"]["style_hints"] == {"tone": "formal"}
    assert result_b["data"]["style_hints"] == {"tone": "direct"}


@pytest.mark.asyncio
async def test_chat_history_is_isolated_when_switching_users():
    sessions = {
        "user-a": {"messages": [{"role": "user", "content": "A message"}]},
        "user-b": {"messages": [{"role": "user", "content": "B message"}]},
    }
    collection = MagicMock()
    collection.find_one = AsyncMock(side_effect=lambda query: sessions.get(query["user_id"]))
    db = {"chat_sessions": collection}

    result_a = await get_chat_history(request_with_db(db), ARTICLE_HASH, 0, "user-a")
    result_b = await get_chat_history(request_with_db(db), ARTICLE_HASH, 0, "user-b")

    assert result_a["data"]["messages"][0]["content"] == "A message"
    assert result_b["data"]["messages"][0]["content"] == "B message"


@pytest.mark.asyncio
async def test_drafts_are_isolated_when_switching_users():
    drafts = {
        "user-a": {"drafts": [{"title": "A draft"}]},
        "user-b": {"drafts": [{"title": "B draft"}]},
    }
    collection = MagicMock()
    collection.find_one = AsyncMock(side_effect=lambda query: drafts.get(query["user_id"]))
    db = {"user_drafts": collection}
    article = {"url_hash": ARTICLE_HASH, "pr_drafts": [{"title": "legacy"}]}

    result_a = await _attach_user_drafts(db, deepcopy(article), "user-a")
    result_b = await _attach_user_drafts(db, deepcopy(article), "user-b")

    assert result_a["pr_drafts"][0]["title"] == "A draft"
    assert result_b["pr_drafts"][0]["title"] == "B draft"
    assert result_a["pr_drafts"][0]["template_source"] == "legacy"
    assert result_b["pr_drafts"][0]["template_source"] == "legacy"


@pytest.mark.asyncio
async def test_logs_are_isolated_when_switching_users():
    logs = {
        "user-a": [{"_id": "la", "user_id": "user-a", "phase": "crawl"}],
        "user-b": [{"_id": "lb", "user_id": "user-b", "phase": "draft"}],
    }
    collection = MagicMock()
    collection.find.side_effect = lambda query: Cursor(logs.get(query["user_id"], []))
    collection.distinct = AsyncMock(
        side_effect=lambda _field, query: [logs[query["user_id"]][0]["phase"]]
    )
    db = {"pipeline_logs": collection}

    result_a = await get_logs_by_date("2026-07-11", request_with_db(db), None, 200, "user-a")
    result_b = await get_logs_by_date("2026-07-11", request_with_db(db), None, 200, "user-b")

    assert [item["user_id"] for item in result_a["logs"]] == ["user-a"]
    assert [item["user_id"] for item in result_b["logs"]] == ["user-b"]
