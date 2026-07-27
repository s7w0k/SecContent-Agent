"""知识库管理员权限与辅助数据模型单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from auth.deps import AuthError, require_admin
from models.knowledge_management import (
    KnowledgeAuditLog,
    KnowledgeDraftCreate,
    KnowledgeDraftInDB,
    KnowledgeDraftResponse,
    KnowledgeDraftStatus,
    KnowledgeDraftUpdate,
    KnowledgePublicationCreate,
    KnowledgePublicationFile,
    KnowledgePublicationInDB,
    KnowledgePublicationResponse,
    KnowledgePublicationStatus,
    KnowledgeRevision,
    KnowledgeRollbackRequest,
    KnowledgeValidationResult,
    KnowledgeValidationStatus,
)
from models.user import UserInDB, UserPublic
from pydantic import ValidationError


def _make_request(user_id: str | None, db) -> SimpleNamespace:
    """构造一个用于依赖测试的 Request mock。"""

    state = SimpleNamespace(user_id=user_id)
    app_state = SimpleNamespace(db=db)
    app = SimpleNamespace(state=app_state)
    return SimpleNamespace(state=state, app=app)


def _make_db(user_doc: dict | None) -> MagicMock:
    """构造一个返回指定用户文档的 mock 数据库。"""

    users = MagicMock()
    users.find_one = AsyncMock(return_value=user_doc)
    db = MagicMock()
    db.__getitem__.return_value = users
    return db


class TestUserAdminField:
    """用户模型 is_admin 字段。"""

    def test_user_in_db_is_admin_defaults_false(self):
        user = UserInDB(
            username="alice",
            display_name="Alice",
            hashed_password="$2b$12$hash",
        )
        assert user.is_admin is False

    def test_user_public_is_admin_defaults_false(self):
        public = UserPublic(
            user_id="u-1",
            username="alice",
            display_name="Alice",
            created_at=datetime.now(UTC),
        )
        assert public.is_admin is False

    def test_user_in_db_accepts_is_admin_true(self):
        user = UserInDB(
            username="admin",
            display_name="Admin",
            hashed_password="$2b$12$hash",
            is_admin=True,
        )
        assert user.is_admin is True

    def test_user_public_accepts_is_admin_true(self):
        public = UserPublic(
            user_id="u-1",
            username="admin",
            display_name="Admin",
            is_admin=True,
            created_at=datetime.now(UTC),
        )
        assert public.is_admin is True


class TestRequireAdmin:
    """require_admin 依赖测试。"""

    @pytest.mark.asyncio
    async def test_raises_403_for_non_admin_user(self):
        user_doc = {"user_id": "u-1", "is_admin": False}
        db = _make_db(user_doc)
        request = _make_request("u-1", db)

        with pytest.raises(AuthError) as exc_info:
            await require_admin(request)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_user_id_and_doc_for_admin(self):
        user_doc = {"user_id": "u-1", "is_admin": True, "username": "admin"}
        db = _make_db(user_doc)
        request = _make_request("u-1", db)

        user_id, returned_doc = await require_admin(request)

        assert user_id == "u-1"
        assert returned_doc["is_admin"] is True
        assert returned_doc["username"] == "admin"

    @pytest.mark.asyncio
    async def test_developer_without_admin_still_raises_403(self):
        user_doc = {"user_id": "u-1", "is_developer": True, "is_admin": False}
        db = _make_db(user_doc)
        request = _make_request("u-1", db)

        with pytest.raises(AuthError) as exc_info:
            await require_admin(request)

        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_user_doc_raises_403(self):
        db = _make_db(None)
        request = _make_request("u-missing", db)

        with pytest.raises(AuthError) as exc_info:
            await require_admin(request)

        assert exc_info.value.status_code == 403


class TestKnowledgeManagementModels:
    """知识库管理模型校验。"""

    def test_draft_create_valid(self):
        draft = KnowledgeDraftCreate(
            document_id="doc-1",
            base_content_hash="abc123",
        )
        assert draft.document_id == "doc-1"
        assert draft.base_content_hash == "abc123"

    def test_draft_update_valid(self):
        update = KnowledgeDraftUpdate(
            content_md="# 新内容",
            change_summary="更新章节",
        )
        assert update.content_md == "# 新内容"
        assert update.change_summary == "更新章节"

    def test_draft_update_defaults_change_summary(self):
        update = KnowledgeDraftUpdate(content_md="# 新内容")
        assert update.change_summary == ""

    def test_draft_in_db_defaults(self):
        draft = KnowledgeDraftInDB(
            draft_id="d-1",
            document_id="doc-1",
            relative_path="docs/intro.md",
            base_content_hash="abc",
            content_md="# 内容",
            created_by="u-1",
            updated_by="u-1",
        )
        assert draft.status == KnowledgeDraftStatus.EDITING
        assert draft.validation.status == KnowledgeValidationStatus.PENDING
        assert draft.created_at.tzinfo is not None
        assert draft.updated_at.tzinfo is not None

    def test_draft_response_validates_from_in_db(self):
        draft = KnowledgeDraftInDB(
            draft_id="d-1",
            document_id="doc-1",
            relative_path="docs/intro.md",
            base_content_hash="abc",
            content_md="# 内容",
            created_by="u-1",
            updated_by="u-1",
        )
        response = KnowledgeDraftResponse.model_validate(draft.model_dump(by_alias=False))
        assert response.draft_id == "d-1"
        assert response.status == KnowledgeDraftStatus.EDITING

    def test_publication_create_requires_drafts(self):
        with pytest.raises(ValidationError):
            KnowledgePublicationCreate(draft_ids=[], version_name="v1")

    def test_publication_create_valid(self):
        pub = KnowledgePublicationCreate(
            draft_ids=["d-1", "d-2"],
            version_name="v1.0.0",
            release_notes="首次发布",
        )
        assert pub.draft_ids == ["d-1", "d-2"]
        assert pub.version_name == "v1.0.0"

    def test_publication_in_db_defaults(self):
        pub = KnowledgePublicationInDB(
            publication_id="p-1",
            version_name="v1",
            published_by="u-1",
        )
        assert pub.status == KnowledgePublicationStatus.VALIDATING
        assert pub.files == []
        assert pub.published_at is None
        assert pub.validation.status == KnowledgeValidationStatus.PENDING

    def test_publication_response_validates_from_in_db(self):
        pub = KnowledgePublicationInDB(
            publication_id="p-1",
            version_name="v1",
            published_by="u-1",
        )
        response = KnowledgePublicationResponse.model_validate(pub.model_dump(by_alias=False))
        assert response.publication_id == "p-1"
        assert response.status == KnowledgePublicationStatus.VALIDATING

    def test_publication_file_valid(self):
        f = KnowledgePublicationFile(
            relative_path="docs/intro.md",
            content_hash="abc",
            revision_id="r-1",
        )
        assert f.relative_path == "docs/intro.md"
        assert f.revision_id == "r-1"

    def test_validation_result_defaults(self):
        result = KnowledgeValidationResult()
        assert result.status == KnowledgeValidationStatus.PENDING
        assert result.errors == []
        assert result.warnings == []

    def test_rollback_request_valid(self):
        req = KnowledgeRollbackRequest(reason="发现内容错误")
        assert req.reason == "发现内容错误"

    def test_rollback_request_rejects_empty_reason(self):
        with pytest.raises(ValidationError):
            KnowledgeRollbackRequest(reason="")

    def test_audit_log_defaults(self):
        log = KnowledgeAuditLog(
            audit_id="a-1",
            user_id="u-1",
            action="draft.create",
            target_type="draft",
            target_id="d-1",
        )
        assert log.detail == {}
        assert log.created_at.tzinfo is not None

    def test_revision_defaults(self):
        revision = KnowledgeRevision(
            revision_id="r-1",
            publication_id="p-1",
            draft_id="d-1",
            relative_path="docs/intro.md",
            new_content_hash="def",
        )
        assert revision.previous_content_hash is None
        assert revision.diff_summary == ""
        assert revision.created_at.tzinfo is not None
