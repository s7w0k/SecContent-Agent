"""用户风格画像 API 单元测试。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.profile import router
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _make_app(db, profiler=...):
    from auth.deps import get_current_user

    async def override_current_user():
        return "local-user"

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = override_current_user
    app.state.db = db
    if profiler is not ...:
        app.state.style_profiler = profiler
    return app


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

    def find(self, query: dict):
        return FakeCursor([item for item in self.documents if _matches(item, query)])

    async def find_one(self, query: dict):
        return next(
            (deepcopy(item) for item in self.documents if _matches(item, query)),
            None,
        )

    async def replace_one(self, query: dict, document: dict, upsert: bool = False):
        for index, current in enumerate(self.documents):
            if _matches(current, query):
                self.documents[index] = deepcopy(document)
                break
        else:
            if upsert:
                self.documents.append(deepcopy(document))
        return SimpleNamespace(modified_count=1)

    async def count_documents(self, query: dict):
        return len([item for item in self.documents if _matches(item, query)])


class FakeProfileDatabase:
    def __init__(self):
        self.collections = {
            "feedbacks": FakeCollection(),
            "articles": FakeCollection(),
            "user_activities": FakeCollection(
                [{"user_id": "local-user", "action": "draft_download", "target": {}}],
            ),
            "chat_sessions": FakeCollection(
                [
                    {
                        "messages": [
                            {"role": "user", "content": "减少技术细节"},
                            {"role": "user", "content": "增强传播性"},
                            {"role": "user", "content": "标题更有冲击力"},
                        ],
                    }
                ],
            ),
            "user_profiles": FakeCollection(),
        }

    def __getitem__(self, name: str):
        return self.collections[name]


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


@pytest.mark.asyncio
async def test_rebuild_real_profiler_falls_back_when_llm_fails():
    db = FakeProfileDatabase()
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=RuntimeError("llm unavailable"))
    app = _make_app(db)
    app.state.llm = llm

    response = await _request(app, "POST", "/api/profile/rebuild")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rebuilt"] is True
    assert data["version"] == 1
    assert data["activity_count"] == 1
    stored = db["user_profiles"].documents[0]
    assert stored["style_hints"]["common_revise_directions"] == []
    assert stored["style_hints"]["preferred_tone"] == "market_oriented"
