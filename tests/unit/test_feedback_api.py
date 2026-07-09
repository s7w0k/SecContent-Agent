"""用户反馈 REST API 单元测试。"""

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
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    """最小异步 MongoDB cursor。"""

    def __init__(self, documents: list[dict]):
        self.documents = documents

    async def to_list(self, length=None):
        documents = deepcopy(self.documents)
        return documents if length is None else documents[:length]


class FakeCollection:
    """覆盖反馈 API 所需 MongoDB 操作的内存集合。"""

    def __init__(self, documents: list[dict] | None = None):
        self.documents = deepcopy(documents or [])

    async def find_one(self, query: dict):
        return next(
            (deepcopy(item) for item in self.documents if _matches(item, query)),
            None,
        )

    def find(self, query: dict):
        return FakeCursor([item for item in self.documents if _matches(item, query)])

    async def insert_one(self, document: dict):
        stored = deepcopy(document)
        stored.setdefault("_id", f"id-{len(self.documents) + 1}")
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def update_one(self, query: dict, update: dict):
        for item in self.documents:
            if _matches(item, query):
                item.update(deepcopy(update.get("$set", {})))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def delete_one(self, query: dict):
        for index, item in enumerate(self.documents):
            if _matches(item, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class FakeDatabase:
    """按集合名访问内存集合。"""

    def __init__(self, article: dict):
        self.collections = {
            "articles": FakeCollection([article]),
            "feedbacks": FakeCollection(),
            "user_activities": FakeCollection(),
        }

    def __getitem__(self, name: str):
        return self.collections[name]


@pytest.fixture
def article():
    return {
        "_id": "article-id",
        "url_hash": ARTICLE_HASH,
        "title": "测试文章",
        "category_v2": "爆点事件",
        "pr_total_score": 150,
        "pr_drafts": [
            {
                "template": "爆点A",
                "perspective": "产品能力视角",
                "title": "草稿一",
                "content_md": "# 草稿一",
                "index": 1,
                "revisions": [
                    {
                        "revision_id": "revision-1",
                        "content_md": "# 修订稿",
                        "applied": False,
                    }
                ],
            },
            {
                "template": "爆点B",
                "perspective": "市场传播视角",
                "title": "草稿二",
                "content_md": "# 草稿二",
                "index": 1,
            },
        ],
    }


@pytest.fixture
def db(article):
    return FakeDatabase(article)


@pytest.fixture
def app(db):
    from api.feedback import router

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.db = db
    return test_app


def _feedback_payload(**overrides):
    payload = {
        "target_type": "draft",
        "target_ref": {
            "article_url_hash": ARTICLE_HASH,
            "draft_index": 0,
        },
        "rating": 5,
        "comment": "角度很好",
        "tags": ["角度好"],
    }
    payload.update(overrides)
    return payload


async def _request(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


class TestCreateFeedback:
    """POST /api/feedback。"""

    @pytest.mark.asyncio
    async def test_create_feedback_and_link_activity(self, app, db):
        response = await _request(
            app,
            "POST",
            "/api/feedback",
            json=_feedback_payload(),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["data"]["feedback_id"]
        assert len(db["feedbacks"].documents) == 1
        assert len(db["user_activities"].documents) == 1
        assert db["user_activities"].documents[0]["action"] == "feedback_submit"

        article = db["articles"].documents[0]
        assert article["pr_drafts"][0]["feedback_summary"] == {
            "avg_rating": 5.0,
            "count": 1,
            "last_rating": 5,
        }

    @pytest.mark.asyncio
    async def test_create_revision_feedback(self, app, db):
        response = await _request(
            app,
            "POST",
            "/api/feedback",
            json=_feedback_payload(
                target_type="revision",
                target_ref={
                    "article_url_hash": ARTICLE_HASH,
                    "draft_index": 0,
                    "revision_id": "revision-1",
                },
                rating=4,
            ),
        )

        assert response.status_code == 200
        assert db["feedbacks"].documents[0]["target_type"] == "revision"

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_rating(self, app):
        response = await _request(
            app,
            "POST",
            "/api/feedback",
            json=_feedback_payload(rating=6),
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_article_not_found(self, app, db):
        db["articles"].documents.clear()

        response = await _request(
            app,
            "POST",
            "/api/feedback",
            json=_feedback_payload(),
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Article not found"

    @pytest.mark.asyncio
    async def test_create_draft_not_found(self, app):
        response = await _request(
            app,
            "POST",
            "/api/feedback",
            json=_feedback_payload(
                target_ref={"article_url_hash": ARTICLE_HASH, "draft_index": 99}
            ),
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Draft not found"

    @pytest.mark.asyncio
    async def test_create_revision_not_found(self, app):
        response = await _request(
            app,
            "POST",
            "/api/feedback",
            json=_feedback_payload(
                target_type="revision",
                target_ref={
                    "article_url_hash": ARTICLE_HASH,
                    "draft_index": 0,
                    "revision_id": "missing",
                },
            ),
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Revision not found"


class TestListAndStats:
    """GET /api/feedback 与 /stats。"""

    @pytest.mark.asyncio
    async def test_list_filters_and_calculates_average(self, app, db):
        now = datetime.now(UTC)
        db["feedbacks"].documents.extend(
            [
                {
                    "feedback_id": "one",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 0,
                    },
                    "rating": 5,
                    "comment": "好",
                    "tags": [],
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "feedback_id": "two",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 0,
                    },
                    "rating": 3,
                    "comment": "一般",
                    "tags": [],
                    "status": "active",
                    "created_at": now - timedelta(minutes=1),
                    "updated_at": now,
                },
                {
                    "feedback_id": "archived",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 0,
                    },
                    "rating": 1,
                    "status": "archived",
                    "created_at": now,
                },
            ]
        )

        response = await _request(
            app,
            "GET",
            f"/api/feedback?target_type=draft&article_url_hash={ARTICLE_HASH}&draft_index=0",
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["avg_rating"] == 4.0
        assert [item["feedback_id"] for item in data["items"]] == ["one", "two"]

    @pytest.mark.asyncio
    async def test_stats_groups_by_template_and_perspective(self, app, db):
        db["feedbacks"].documents.extend(
            [
                {
                    "feedback_id": "one",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 0,
                    },
                    "rating": 5,
                    "status": "active",
                },
                {
                    "feedback_id": "two",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 1,
                    },
                    "rating": 3,
                    "status": "active",
                },
            ]
        )

        template_response = await _request(
            app,
            "GET",
            "/api/feedback/stats?group_by=template",
        )
        perspective_response = await _request(
            app,
            "GET",
            "/api/feedback/stats?group_by=perspective",
        )

        assert template_response.status_code == 200
        template_data = template_response.json()["data"]
        assert template_data["total"] == 2
        assert template_data["overall_avg"] == 4.0
        assert {item["key"] for item in template_data["groups"]} == {
            "爆点A",
            "爆点B",
        }

        perspective_data = perspective_response.json()["data"]
        assert {item["key"] for item in perspective_data["groups"]} == {
            "产品能力视角",
            "市场传播视角",
        }


class TestUpdateAndDelete:
    """PUT/DELETE /api/feedback/{feedback_id}。"""

    @pytest.fixture(autouse=True)
    def seed_feedback(self, db):
        now = datetime.now(UTC)
        db["feedbacks"].documents.extend(
            [
                {
                    "feedback_id": "feedback-1",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 0,
                    },
                    "rating": 5,
                    "comment": "原评论",
                    "tags": [],
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "feedback_id": "feedback-2",
                    "user_id": "local-user",
                    "target_type": "draft",
                    "target_ref": {
                        "article_url_hash": ARTICLE_HASH,
                        "draft_index": 0,
                    },
                    "rating": 3,
                    "comment": "",
                    "tags": [],
                    "status": "active",
                    "created_at": now - timedelta(minutes=1),
                    "updated_at": now,
                },
            ]
        )

    @pytest.mark.asyncio
    async def test_update_feedback_and_summary(self, app, db):
        response = await _request(
            app,
            "PUT",
            "/api/feedback/feedback-1",
            json={"rating": 1, "comment": "更新后"},
        )

        assert response.status_code == 200
        assert response.json()["data"]["updated"] is True
        updated = await db["feedbacks"].find_one({"feedback_id": "feedback-1"})
        assert updated["rating"] == 1
        assert updated["comment"] == "更新后"
        assert db["articles"].documents[0]["pr_drafts"][0]["feedback_summary"] == {
            "avg_rating": 2.0,
            "count": 2,
            "last_rating": 1,
        }

    @pytest.mark.asyncio
    async def test_delete_feedback_and_summary(self, app, db):
        response = await _request(
            app,
            "DELETE",
            "/api/feedback/feedback-1",
        )

        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True
        assert len(db["feedbacks"].documents) == 1
        assert db["articles"].documents[0]["pr_drafts"][0]["feedback_summary"] == {
            "avg_rating": 3.0,
            "count": 1,
            "last_rating": 3,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    async def test_missing_feedback_returns_404(self, app, method):
        kwargs = {"json": {"rating": 4}} if method == "PUT" else {}
        response = await _request(
            app,
            method,
            "/api/feedback/missing",
            **kwargs,
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Feedback not found"


@pytest.mark.asyncio
async def test_database_unavailable():
    from api.feedback import router

    app = FastAPI()
    app.include_router(router)
    app.state.db = None

    response = await _request(app, "GET", "/api/feedback")

    assert response.status_code == 503
