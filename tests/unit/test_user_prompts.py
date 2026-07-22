"""Task 10.3 user prompt API and persistence tests."""

from __future__ import annotations

import os
import sys
from copy import deepcopy

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.draft_generator import SYSTEM_PROMPT_TEMPLATE
from api.user_prompts import router
from auth.deps import AuthError, auth_error_handler, get_current_user
from db.mongo import MongoDB

REQUIRED = ["knowledge_context", "template_spec", "style_hints"]
CUSTOM_PROMPT = """你是当前用户的安全 PR 撰稿人，请保持事实准确并按模板输出。

产品知识：
{knowledge_context}

文章模板：
{template_spec}

风格偏好：
{style_hints}
"""


class FakePromptCollection:
    def __init__(self):
        self.documents: list[dict] = []

    async def find_one(self, query: dict):
        return next(
            (
                deepcopy(doc)
                for doc in self.documents
                if all(doc.get(k) == v for k, v in query.items())
            ),
            None,
        )

    async def update_one(self, query: dict, update: dict, *, upsert: bool = False):
        document = next(
            (doc for doc in self.documents if all(doc.get(k) == v for k, v in query.items())),
            None,
        )
        if document is None and upsert:
            document = {**query, **update.get("$setOnInsert", {})}
            self.documents.append(document)
        if document is not None:
            document.update(update.get("$set", {}))

    async def delete_one(self, query: dict):
        self.documents = [
            doc for doc in self.documents if not all(doc.get(k) == v for k, v in query.items())
        ]


@pytest.fixture
def prompt_app():
    app = FastAPI()
    app.add_exception_handler(AuthError, auth_error_handler)
    app.include_router(router)
    collection = FakePromptCollection()
    app.state.db = {"user_prompts": collection}

    async def current_test_user(request: Request) -> str:
        return request.headers.get("X-Test-User", "user-a")

    app.dependency_overrides[get_current_user] = current_test_user
    return app, collection


async def _request(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_get_returns_system_default(prompt_app):
    app, _ = prompt_app
    response = await _request(app, "GET", "/api/user-prompts/draft-system")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["content"] == SYSTEM_PROMPT_TEMPLATE
    assert data["is_custom"] is False
    assert data["required_placeholders"] == REQUIRED
    assert data["updated_at"] is None


@pytest.mark.asyncio
async def test_save_then_get_returns_custom_prompt(prompt_app):
    app, collection = prompt_app
    saved = await _request(
        app,
        "PUT",
        "/api/user-prompts/draft-system",
        json={"content": CUSTOM_PROMPT},
    )
    fetched = await _request(app, "GET", "/api/user-prompts/draft-system")

    assert saved.status_code == 200
    assert saved.json()["data"]["is_custom"] is True
    assert fetched.json()["data"]["content"] == CUSTOM_PROMPT
    assert fetched.json()["data"]["is_custom"] is True
    assert collection.documents[0]["created_at"] == collection.documents[0]["updated_at"]


@pytest.mark.asyncio
async def test_missing_placeholder_returns_unified_422(prompt_app):
    app, collection = prompt_app
    invalid = CUSTOM_PROMPT.replace("{template_spec}", "模板内容放在这里")

    response = await _request(
        app,
        "PUT",
        "/api/user-prompts/draft-system",
        json={"content": invalid},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MISSING_PLACEHOLDER"
    assert "{template_spec}" in response.json()["error"]["message"]
    assert collection.documents == []


@pytest.mark.asyncio
async def test_prompt_length_is_validated(prompt_app):
    app, _ = prompt_app
    response = await _request(
        app,
        "PUT",
        "/api/user-prompts/draft-system",
        json={"content": "too short"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reset_restores_default(prompt_app):
    app, collection = prompt_app
    await _request(
        app,
        "PUT",
        "/api/user-prompts/draft-system",
        json={"content": CUSTOM_PROMPT},
    )

    response = await _request(app, "POST", "/api/user-prompts/draft-system/reset")

    data = response.json()["data"]
    assert data["content"] == SYSTEM_PROMPT_TEMPLATE
    assert data["is_custom"] is False
    assert collection.documents == []


@pytest.mark.asyncio
async def test_user_prompts_are_tenant_isolated(prompt_app):
    app, _ = prompt_app
    await _request(
        app,
        "PUT",
        "/api/user-prompts/draft-system",
        headers={"X-Test-User": "user-a"},
        json={"content": CUSTOM_PROMPT},
    )

    user_a = await _request(
        app, "GET", "/api/user-prompts/draft-system", headers={"X-Test-User": "user-a"}
    )
    user_b = await _request(
        app, "GET", "/api/user-prompts/draft-system", headers={"X-Test-User": "user-b"}
    )

    assert user_a.json()["data"]["is_custom"] is True
    assert user_b.json()["data"]["is_custom"] is False
    assert user_b.json()["data"]["content"] == SYSTEM_PROMPT_TEMPLATE


@pytest.mark.asyncio
async def test_user_prompt_index_is_unique(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, list] = {}

    class IndexCollection:
        async def create_indexes(self, indexes):
            captured["indexes"] = indexes
            return [index.document["name"] for index in indexes]

        async def drop_index(self, _name: str):
            return None

    collections: dict[str, IndexCollection] = {}

    def get_collection(cls, name: str):
        collection = collections.setdefault(name, IndexCollection())
        if name == "user_prompts":
            original = collection.create_indexes

            async def capture(indexes):
                captured["user_prompts"] = indexes
                return await original(indexes)

            collection.create_indexes = capture
        return collection

    monkeypatch.setattr(MongoDB, "get_collection", classmethod(get_collection))
    created = await MongoDB.ensure_indexes()

    assert created["user_prompts"] == ["uq_user_prompt_key"]
    index = captured["user_prompts"][0].document
    assert index["unique"] is True
    assert list(index["key"].items()) == [("user_id", 1), ("prompt_key", 1)]
