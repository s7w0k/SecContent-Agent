"""认证 API 与认证中间件测试。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from api.auth import PRIVATE_USER_COLLECTIONS, hash_password, router
from auth.deps import AuthError, auth_error_handler
from auth.jwt import decode_access_token
from config import get_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _matches(document: dict, query: dict) -> bool:
    return all(document.get(key) == value for key, value in query.items())


class FakeCollection:
    def __init__(self, documents: list[dict] | None = None):
        self.documents = deepcopy(documents or [])

    async def find_one(self, query: dict):
        return next(
            (deepcopy(document) for document in self.documents if _matches(document, query)),
            None,
        )

    async def insert_one(self, document: dict):
        stored = deepcopy(document)
        stored["_id"] = f"id-{len(self.documents) + 1}"
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def delete_one(self, query: dict):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query: dict):
        retained = [document for document in self.documents if not _matches(document, query)]
        deleted_count = len(self.documents) - len(retained)
        self.documents = retained
        return SimpleNamespace(deleted_count=deleted_count)


class FakeDatabase:
    def __init__(self):
        self.collections = {"users": FakeCollection()}
        for name in PRIVATE_USER_COLLECTIONS:
            self.collections[name] = FakeCollection()

    def __getitem__(self, name: str):
        return self.collections[name]


@pytest.fixture
def jwt_config(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "auth-api-test-secret-at-least-32-characters")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_EXPIRE_HOURS", "24")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db():
    return FakeDatabase()


@pytest.fixture
def app(db, jwt_config):
    from main import auth_middleware

    test_app = FastAPI()
    test_app.add_exception_handler(AuthError, auth_error_handler)
    test_app.middleware("http")(auth_middleware)
    test_app.include_router(router)
    test_app.state.db = db
    return test_app


async def _request(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


async def _register(app: FastAPI, **overrides):
    payload = {
        "username": "alice",
        "password": "SecurePass123!",
        "display_name": "Alice",
        "email": "alice@example.com",
    }
    payload.update(overrides)
    return await _request(app, "POST", "/api/auth/register", json=payload)


async def _login(app: FastAPI, **overrides):
    payload = {"username": "alice", "password": "SecurePass123!"}
    payload.update(overrides)
    return await _request(app, "POST", "/api/auth/login", json=payload)


@pytest.mark.asyncio
async def test_register_hashes_password_and_returns_public_user(app, db):
    response = await _register(app)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["username"] == "alice"
    assert "hashed_password" not in data
    stored = db["users"].documents[0]
    assert stored["hashed_password"] != "SecurePass123!"
    assert stored["hashed_password"].startswith("$2b$12$")


@pytest.mark.asyncio
async def test_register_duplicate_username_returns_409(app):
    assert (await _register(app)).status_code == 200
    response = await _register(app)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "USERNAME_EXISTS"


@pytest.mark.asyncio
async def test_login_returns_decodable_jwt(app):
    await _register(app)
    response = await _login(app)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 86400
    payload = decode_access_token(data["access_token"])
    assert payload is not None
    assert payload["sub"] == data["user"]["user_id"]


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(app):
    await _register(app)
    response = await _login(app, password="WrongPassword!")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_get_me_requires_token(app):
    response = await _request(app, "GET", "/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


@pytest.mark.asyncio
async def test_get_me_returns_authenticated_user(app):
    await _register(app)
    login = await _login(app)
    token = login.json()["data"]["access_token"]

    response = await _request(
        app,
        "GET",
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["username"] == "alice"


@pytest.mark.asyncio
async def test_disabled_account_cannot_login(app, db):
    await _register(app)
    db["users"].documents[0]["is_active"] = False

    response = await _login(app)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


@pytest.mark.asyncio
async def test_delete_account_cascades_private_data(app, db):
    await _register(app)
    login = await _login(app)
    token = login.json()["data"]["access_token"]
    user_id = login.json()["data"]["user"]["user_id"]
    for collection_name in PRIVATE_USER_COLLECTIONS:
        db[collection_name].documents.extend(
            [
                {"user_id": user_id, "value": "delete"},
                {"user_id": "another-user", "value": "keep"},
            ]
        )

    response = await _request(
        app,
        "DELETE",
        "/api/auth/account",
        json={"password": "SecurePass123!"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert db["users"].documents == []
    for collection_name in PRIVATE_USER_COLLECTIONS:
        assert db[collection_name].documents == [{"user_id": "another-user", "value": "keep"}]


@pytest.mark.asyncio
async def test_delete_account_rejects_wrong_password(app, db):
    await _register(app)
    login = await _login(app)
    token = login.json()["data"]["access_token"]

    response = await _request(
        app,
        "DELETE",
        "/api/auth/account",
        json={"password": "WrongPassword!"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert len(db["users"].documents) == 1


def test_hash_password_is_bcrypt_cost_12():
    hashed = hash_password("SecurePass123!")
    assert hashed.startswith("$2b$12$")
