"""任务 7.3 API 认证与用户隔离测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from api.activity import router as activity_router
from api.chat import router as chat_router
from api.dashboard import _attach_user_drafts
from api.dashboard import router as dashboard_router
from api.feedback import router as feedback_router
from api.pipeline import router as pipeline_router
from api.profile import router as profile_router
from auth.deps import get_current_user
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


async def _request(app: FastAPI, path: str):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/feedback",
        "/api/activities",
        "/api/profile/style",
        "/api/articles/abc/drafts/0/chat-history",
        "/api/pipeline/status",
        "/api/articles",
    ],
)
async def test_protected_apis_require_authentication(path):
    app = FastAPI()
    for router in (
        feedback_router,
        activity_router,
        profile_router,
        chat_router,
        pipeline_router,
        dashboard_router,
    ):
        app.include_router(router)

    response = await _request(app, path)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_feedback_activity_profile_and_chat_queries_use_current_user():
    app = FastAPI()
    for router in (feedback_router, activity_router, profile_router, chat_router):
        app.include_router(router)

    async def current_user_override():
        return "user-b"

    app.dependency_overrides[get_current_user] = current_user_override
    db = MagicMock()
    feedbacks = MagicMock()
    activities = MagicMock()
    profiles = MagicMock()
    sessions = MagicMock()
    empty_cursor = MagicMock()
    empty_cursor.to_list = AsyncMock(return_value=[])
    feedbacks.find.return_value = empty_cursor
    activities.find.return_value = empty_cursor
    profiles.find_one = AsyncMock(
        return_value={
            "user_id": "user-b",
            "style_hints": {},
            "created_at": "2026-07-10T00:00:00+00:00",
            "updated_at": "2026-07-10T00:00:00+00:00",
        }
    )
    sessions.find_one = AsyncMock(return_value=None)
    collections = {
        "feedbacks": feedbacks,
        "user_activities": activities,
        "user_profiles": profiles,
        "chat_sessions": sessions,
    }
    db.__getitem__.side_effect = collections.__getitem__
    app.state.db = db

    assert (await _request(app, "/api/feedback")).status_code == 200
    assert (await _request(app, "/api/activities")).status_code == 200
    assert (await _request(app, "/api/profile/style")).status_code == 200
    assert (await _request(app, "/api/articles/abc/drafts/0/chat-history")).status_code == 200

    assert feedbacks.find.call_args.args[0]["user_id"] == "user-b"
    assert activities.find.call_args.args[0]["user_id"] == "user-b"
    profiles.find_one.assert_awaited_once_with({"user_id": "user-b"})
    sessions.find_one.assert_awaited_once_with(
        {"user_id": "user-b", "article_url_hash": "abc", "draft_index": 0}
    )


@pytest.mark.asyncio
async def test_dashboard_attaches_only_current_users_drafts():
    db = MagicMock()
    user_drafts = MagicMock()
    user_drafts.find_one = AsyncMock(return_value={"drafts": [{"title": "B draft"}]})
    db.__getitem__.return_value = user_drafts
    article = {"url_hash": "a" * 32, "pr_drafts": [{"title": "legacy"}]}

    result = await _attach_user_drafts(db, article, "user-b")

    assert result["pr_drafts"] == [{"title": "B draft"}]
    assert result["can_generate"] is False
    user_drafts.find_one.assert_awaited_once_with(
        {"user_id": "user-b", "article_url_hash": "a" * 32}
    )
