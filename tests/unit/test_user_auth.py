"""用户模型与 JWT 工具单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from auth.jwt import create_access_token, decode_access_token
from jose import jwt
from models.user import UserCreate, UserInDB, UserPublic
from pydantic import ValidationError

JWT_SETTINGS = SimpleNamespace(
    JWT_SECRET="unit-test-secret-at-least-32-characters",
    JWT_ALGORITHM="HS256",
    JWT_EXPIRE_HOURS=24,
)


class TestUserModels:
    def test_user_create_defaults_display_name(self):
        user = UserCreate(username="alice_1", password="secret123")
        assert user.display_name == "alice_1"

    @pytest.mark.parametrize("username", ["ab", "has-dash", "包含中文", "a" * 21])
    def test_user_create_rejects_invalid_username(self, username):
        with pytest.raises(ValidationError):
            UserCreate(username=username, password="secret123")

    @pytest.mark.parametrize("password", ["short", "密" * 25])
    def test_user_create_rejects_invalid_password(self, password):
        with pytest.raises(ValidationError):
            UserCreate(username="alice", password=password)

    def test_user_create_rejects_invalid_email(self):
        with pytest.raises(ValidationError):
            UserCreate(username="alice", password="secret123", email="not-an-email")

    def test_public_model_excludes_password_hash(self):
        user = UserInDB(
            username="alice",
            display_name="Alice",
            hashed_password="$2b$12$hash",
        )
        public = UserPublic.model_validate(user.model_dump())
        dumped = public.model_dump()
        assert dumped["username"] == "alice"
        assert "hashed_password" not in dumped
        assert user.user_id.startswith("u-")


class TestJwt:
    def test_create_and_decode_access_token(self):
        with patch("auth.jwt.get_settings", return_value=JWT_SETTINGS):
            token = create_access_token("user-1", "alice")
            payload = decode_access_token(token)

        assert payload is not None
        assert payload["sub"] == "user-1"
        assert payload["username"] == "alice"
        assert payload["exp"] > payload["iat"]

    def test_decode_rejects_tampered_token(self):
        with patch("auth.jwt.get_settings", return_value=JWT_SETTINGS):
            token = create_access_token("user-1", "alice")
            assert decode_access_token(f"{token}tampered") is None

    def test_decode_rejects_expired_token(self):
        payload = {
            "sub": "user-1",
            "username": "alice",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
        }
        token = jwt.encode(payload, JWT_SETTINGS.JWT_SECRET, algorithm=JWT_SETTINGS.JWT_ALGORITHM)
        with patch("auth.jwt.get_settings", return_value=JWT_SETTINGS):
            assert decode_access_token(token) is None

    def test_create_requires_secret(self):
        settings = SimpleNamespace(
            JWT_SECRET="",
            JWT_ALGORITHM="HS256",
            JWT_EXPIRE_HOURS=24,
        )
        with (
            patch("auth.jwt.get_settings", return_value=settings),
            pytest.raises(RuntimeError),
        ):
            create_access_token("user-1", "alice")
