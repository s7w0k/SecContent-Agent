"""用户操作记录 REST API 单元测试。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _nested_value(document: dict, path: str):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = _nested_value(document, key)
        if isinstance(expected, dict) and "$gte" in expected:
            if actual is None or actual < expected["$gte"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, documents: list[dict]):
        self.documents = documents

    async def to_list(self, length=None):
        result = deepcopy(self.documents)
        return result if length is None else result[:length]


class FakeActivityCollection:
    def __init__(self):
        self.documents: list[dict] = []
        self.fail_inserts = False

    async def insert_one(self, document: dict):
        if self.fail_inserts:
            raise RuntimeError("insert failed")
        stored = deepcopy(document)
        stored["_id"] = f"id-{len(self.documents) + 1}"
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    def find(self, query: dict):
        return FakeCursor([item for item in self.documents if _matches(item, query)])


class FakeDatabase:
    def __init__(self):
        self.activities = FakeActivityCollection()

    def __getitem__(self, name: str):
        assert name == "user_activities"
        return self.activities


@pytest.fixture
def db():
    return FakeDatabase()


@pytest.fixture
def app(db):
    from api.activity import router
    from auth.deps import get_current_user

    async def override_current_user():
        return "local-user"

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_current_user] = override_current_user
    test_app.state.db = db
    return test_app


def _activity_payload(**overrides):
    payload = {
        "action": "draft_download",
        "target": {
            "article_url_hash": ARTICLE_HASH,
            "draft_index": 0,
            "template": "爆点A",
            "template_id": "tpl-user-breaking-a",
            "template_key": "breaking_a",
            "template_version": 3,
            "template_name": "爆点A",
            "perspective": "产品能力视角",
        },
        "context": {"article_title": "测试文章"},
        "metadata": {"file_format": "md"},
    }
    payload.update(overrides)
    return payload


async def _request(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


class TestActivityCreate:
    @pytest.mark.asyncio
    async def test_create_activity(self, app, db):
        response = await _request(
            app,
            "POST",
            "/api/activities/log",
            json=_activity_payload(),
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["activity_id"]
        assert len(db.activities.documents) == 1
        assert db.activities.documents[0]["action"] == "draft_download"
        assert db.activities.documents[0]["target"]["template_id"] == "tpl-user-breaking-a"

    @pytest.mark.asyncio
    async def test_create_pipeline_activity_without_article(self, app, db):
        response = await _request(
            app,
            "POST",
            "/api/activities/log",
            json=_activity_payload(
                action="pipeline_run",
                target={"pipeline_id": "pipeline-1"},
            ),
        )

        assert response.status_code == 200
        assert db.activities.documents[0]["target"]["pipeline_id"] == "pipeline-1"

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_action(self, app):
        response = await _request(
            app,
            "POST",
            "/api/activities/log",
            json=_activity_payload(action="unknown"),
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_rejects_missing_article(self, app):
        response = await _request(
            app,
            "POST",
            "/api/activities/log",
            json=_activity_payload(target={}),
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_returns_500_when_insert_fails(self, app, db):
        db.activities.fail_inserts = True

        response = await _request(
            app,
            "POST",
            "/api/activities/log",
            json=_activity_payload(),
        )

        assert response.status_code == 500


class TestActivityBatch:
    @pytest.mark.asyncio
    async def test_batch_create(self, app, db):
        response = await _request(
            app,
            "POST",
            "/api/activities/batch-log",
            json={
                "activities": [
                    _activity_payload(),
                    _activity_payload(
                        action="revision_apply",
                        target={
                            "article_url_hash": ARTICLE_HASH,
                            "draft_index": 0,
                            "revision_id": "revision-1",
                        },
                    ),
                ]
            },
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["recorded"] == 2
        assert data["failed"] == 0
        assert len(data["activity_ids"]) == 2
        assert len(db.activities.documents) == 2

    @pytest.mark.asyncio
    async def test_batch_rejects_empty_list(self, app):
        response = await _request(
            app,
            "POST",
            "/api/activities/batch-log",
            json={"activities": []},
        )

        assert response.status_code == 422


class TestActivityQuery:
    @pytest.fixture(autouse=True)
    def seed(self, db):
        now = datetime.now(UTC)
        db.activities.documents.extend(
            [
                {
                    "activity_id": "one",
                    "user_id": "local-user",
                    "action": "draft_download",
                    "target": {
                        "article_url_hash": ARTICLE_HASH,
                        "template": "爆点A",
                    },
                    "context": {},
                    "metadata": {},
                    "created_at": now,
                },
                {
                    "activity_id": "two",
                    "user_id": "local-user",
                    "action": "draft_revise",
                    "target": {
                        "article_url_hash": ARTICLE_HASH,
                        "template": "爆点B",
                    },
                    "context": {},
                    "metadata": {},
                    "created_at": now - timedelta(days=1),
                },
                {
                    "activity_id": "old",
                    "user_id": "local-user",
                    "action": "draft_download",
                    "target": {
                        "article_url_hash": ARTICLE_HASH,
                        "template": "爆点A",
                    },
                    "context": {},
                    "metadata": {},
                    "created_at": now - timedelta(days=90),
                },
            ]
        )

    @pytest.mark.asyncio
    async def test_list_filters_and_paginates(self, app):
        response = await _request(
            app,
            "GET",
            f"/api/activities?action=draft_download&article_url_hash={ARTICLE_HASH}"
            "&page=1&page_size=1",
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert len(data["items"]) == 1
        assert data["items"][0]["activity_id"] == "one"

    @pytest.mark.asyncio
    async def test_list_returns_second_page(self, app):
        response = await _request(
            app,
            "GET",
            "/api/activities?page=2&page_size=1",
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 3
        assert data["page"] == 2
        assert data["items"][0]["activity_id"] == "two"

    @pytest.mark.asyncio
    async def test_stats(self, app):
        response = await _request(
            app,
            "GET",
            "/api/activities/stats?days=30",
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["by_action"] == {"draft_download": 1, "draft_revise": 1}
        assert data["by_template"] == {"爆点A": 1, "爆点B": 1}
        assert sum(item["count"] for item in data["daily_trend"]) == 2


@pytest.mark.asyncio
async def test_log_activity_is_best_effort(db):
    from api.activity import log_activity

    db.activities.fail_inserts = True
    result = await log_activity(
        db,
        "local-user",
        "draft_view",
        {"article_url_hash": ARTICLE_HASH},
    )

    assert result is None


@pytest.mark.asyncio
async def test_database_unavailable():
    from api.activity import router
    from auth.deps import get_current_user

    async def override_current_user():
        return "local-user"

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = override_current_user
    app.state.db = None

    response = await _request(app, "GET", "/api/activities")

    assert response.status_code == 503
