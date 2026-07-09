"""用户风格画像 API 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.profile import router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(db, profiler=...):
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    if profiler is not ...:
        app.state.style_profiler = profiler
    return app


async def _request(app, method: str, path: str):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path)


@pytest.fixture
def db():
    profiles = MagicMock()
    profiles.find_one = AsyncMock(return_value=None)
    profiles.count_documents = AsyncMock(return_value=15)
    database = MagicMock()
    database.__getitem__.return_value = profiles
    database._profiles = profiles
    return database


@pytest.mark.asyncio
async def test_get_profile_success(db):
    now = datetime.now(UTC)
    db._profiles.find_one.return_value = {
        "_id": SimpleNamespace(),
        "user_id": "local-user",
        "style_hints": {},
        "created_at": now,
        "updated_at": now,
    }

    response = await _request(_make_app(db), "GET", "/api/profile/style")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["user_id"] == "local-user"
    assert "_id" not in data
    assert data["updated_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_get_profile_not_found(db):
    response = await _request(_make_app(db), "GET", "/api/profile/style")

    assert response.status_code == 404
    assert response.json()["detail"] == "Profile not found"


@pytest.mark.asyncio
async def test_get_profile_database_unavailable():
    response = await _request(_make_app(None), "GET", "/api/profile/style")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_rebuild_database_unavailable():
    response = await _request(_make_app(None, MagicMock()), "POST", "/api/profile/rebuild")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_rebuild_profile(db):
    profiler = MagicMock()
    profiler.build_profile = AsyncMock(
        return_value={
            "feedback_summary": {"total_feedbacks": 6},
            "activity_summary": {
                "total_downloads": 3,
                "total_applies": 2,
                "total_revises": 4,
                "total_feedbacks": 6,
            },
            "version": 2,
            "updated_at": "2026-07-09T00:00:00+00:00",
        },
    )

    response = await _request(
        _make_app(db, profiler),
        "POST",
        "/api/profile/rebuild",
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rebuilt"] is True
    assert data["feedback_count"] == 6
    assert data["activity_count"] == 15
    assert data["version"] == 2
    profiler.build_profile.assert_awaited_once_with("local-user")


@pytest.mark.asyncio
async def test_rebuild_lazily_creates_profiler(db):
    db["feedbacks"].find.return_value.to_list = AsyncMock(return_value=[])
    db["user_activities"].find.return_value.to_list = AsyncMock(return_value=[])
    db["chat_sessions"].find.return_value.to_list = AsyncMock(return_value=[])
    db["user_profiles"].find_one = AsyncMock(return_value=None)
    db["user_profiles"].replace_one = AsyncMock()
    db["user_activities"].count_documents = AsyncMock(return_value=0)
    app = _make_app(db)
    app.state.llm = None

    response = await _request(app, "POST", "/api/profile/rebuild")

    assert response.status_code == 200
    assert response.json()["data"]["version"] == 1
    assert app.state.style_profiler is not None
