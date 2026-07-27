"""草稿仓库单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from knowledge_admin.repository import KnowledgeDraftRepository
from models.knowledge_management import (
    KnowledgeDraftStatus,
    KnowledgeValidationStatus,
)


def _make_draft_doc(
    draft_id: str = "kbd-20260101-abcdef",
    relative_path: str = "docs/intro.md",
    status: str = "editing",
    content_md: str = "# Hello",
    change_summary: str = "",
) -> dict[str, Any]:
    """构造一个草稿文档字典，模拟 MongoDB 中存储的文档。"""
    return {
        "_id": None,
        "draft_id": draft_id,
        "document_id": "doc-hash-123",
        "relative_path": relative_path,
        "base_content_hash": "sha256:abc",
        "content_md": content_md,
        "status": status,
        "validation": {
            "status": "pending",
            "errors": [],
            "warnings": [],
        },
        "change_summary": change_summary,
        "created_by": "u-admin",
        "updated_by": "u-admin",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


def _make_mock_db(collection: Any) -> MagicMock:
    """构造一个 mock 数据库，返回指定的 collection。"""
    db = MagicMock()
    db.__getitem__.return_value = collection
    return db


def _make_mock_cursor(docs: list[dict[str, Any]]) -> MagicMock:
    """构造一个模拟 Motor cursor 的链式调用 mock。"""
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=docs)
    return cursor


# ═══════════════════════════════════════════════════════════════
# create_draft
# ═══════════════════════════════════════════════════════════════


class TestCreateDraft:
    """create_draft 测试。"""

    @pytest.mark.asyncio
    async def test_creates_draft_with_correct_fields(self):
        collection = MagicMock()
        collection.find_one = AsyncMock(return_value=None)
        collection.insert_one = AsyncMock()

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        draft = await repo.create_draft(
            relative_path="docs/intro.md",
            base_content_hash="sha256:abc",
            content_md="# Hello",
            user_id="u-admin",
        )

        assert draft.draft_id.startswith("kbd-")
        assert draft.relative_path == "docs/intro.md"
        assert draft.base_content_hash == "sha256:abc"
        assert draft.content_md == "# Hello"
        assert draft.status == KnowledgeDraftStatus.EDITING
        assert draft.validation.status == KnowledgeValidationStatus.PENDING
        assert draft.validation.errors == []
        assert draft.validation.warnings == []
        assert draft.created_by == "u-admin"
        assert draft.updated_by == "u-admin"
        assert draft.change_summary == ""
        assert draft.document_id  # 非空
        # 验证 insert_one 被调用
        collection.insert_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_existing_draft_for_same_path(self):
        existing_doc = _make_draft_doc()

        collection = MagicMock()
        collection.find_one = AsyncMock(return_value=existing_doc)
        collection.insert_one = AsyncMock()

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        draft = await repo.create_draft(
            relative_path="docs/intro.md",
            base_content_hash="sha256:abc",
            content_md="# Hello",
            user_id="u-admin",
        )

        assert draft.draft_id == "kbd-20260101-abcdef"
        # 验证 insert_one 未被调用（返回了已有草稿）
        collection.insert_one.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# get_draft
# ═══════════════════════════════════════════════════════════════


class TestGetDraft:
    """get_draft 测试。"""

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent_draft(self):
        collection = MagicMock()
        collection.find_one = AsyncMock(return_value=None)

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        result = await repo.get_draft("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_draft_for_existing_id(self):
        doc = _make_draft_doc()
        collection = MagicMock()
        collection.find_one = AsyncMock(return_value=doc)

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        result = await repo.get_draft("kbd-20260101-abcdef")
        assert result is not None
        assert result.draft_id == "kbd-20260101-abcdef"


# ═══════════════════════════════════════════════════════════════
# update_draft
# ═══════════════════════════════════════════════════════════════


class TestUpdateDraft:
    """update_draft 测试。"""

    @pytest.mark.asyncio
    async def test_updates_content_and_resets_validation(self):
        updated_doc = _make_draft_doc(content_md="# Updated", change_summary="改了标题")

        collection = MagicMock()
        collection.find_one_and_update = AsyncMock(return_value=updated_doc)

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        result = await repo.update_draft(
            draft_id="kbd-20260101-abcdef",
            content_md="# Updated",
            user_id="u-admin",
            change_summary="改了标题",
        )

        assert result is not None
        assert result.content_md == "# Updated"
        assert result.change_summary == "改了标题"
        assert result.validation.status == KnowledgeValidationStatus.PENDING

        # 验证 find_one_and_update 的查询条件
        call_args = collection.find_one_and_update.call_args
        query = call_args.args[0]
        assert query["draft_id"] == "kbd-20260101-abcdef"
        assert query["status"] == KnowledgeDraftStatus.EDITING

        # 验证更新内容包含 content_md 和重置 validation
        update_doc = call_args.args[1]
        assert update_doc["$set"]["content_md"] == "# Updated"
        assert update_doc["$set"]["change_summary"] == "改了标题"
        assert update_doc["$set"]["validation.status"] == KnowledgeValidationStatus.PENDING
        assert update_doc["$set"]["validation.errors"] == []
        assert update_doc["$set"]["validation.warnings"] == []

    @pytest.mark.asyncio
    async def test_returns_none_when_draft_not_found(self):
        collection = MagicMock()
        collection.find_one_and_update = AsyncMock(return_value=None)

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        result = await repo.update_draft(
            draft_id="nonexistent",
            content_md="# Updated",
            user_id="u-admin",
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════
# delete_draft
# ═══════════════════════════════════════════════════════════════


class TestDeleteDraft:
    """delete_draft 测试。"""

    @pytest.mark.asyncio
    async def test_marks_draft_as_abandoned(self):
        result_mock = MagicMock()
        result_mock.modified_count = 1

        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=result_mock)

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        deleted = await repo.delete_draft("kbd-20260101-abcdef", "u-admin")
        assert deleted is True

        # 验证 update_one 的查询和更新内容
        call_args = collection.update_one.call_args
        query = call_args.args[0]
        assert query["draft_id"] == "kbd-20260101-abcdef"
        assert query["status"] == KnowledgeDraftStatus.EDITING

        update_doc = call_args.args[1]
        assert update_doc["$set"]["status"] == KnowledgeDraftStatus.ABANDONED
        assert update_doc["$set"]["updated_by"] == "u-admin"

    @pytest.mark.asyncio
    async def test_returns_false_when_draft_not_found(self):
        result_mock = MagicMock()
        result_mock.modified_count = 0

        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=result_mock)

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        deleted = await repo.delete_draft("nonexistent", "u-admin")
        assert deleted is False


# ═══════════════════════════════════════════════════════════════
# list_drafts
# ═══════════════════════════════════════════════════════════════


class TestListDrafts:
    """list_drafts 测试。"""

    @pytest.mark.asyncio
    async def test_filters_by_path_and_status(self):
        docs = [
            _make_draft_doc(draft_id="d-1", relative_path="docs/a.md", status="editing"),
        ]

        collection = MagicMock()
        collection.find = MagicMock(return_value=_make_mock_cursor(docs))

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        drafts = await repo.list_drafts(relative_path="docs/a.md", status="editing")

        assert len(drafts) == 1
        assert drafts[0].draft_id == "d-1"

        # 验证 find 收到正确的查询条件
        call_args = collection.find.call_args
        query = call_args.args[0]
        assert query["relative_path"] == "docs/a.md"
        assert query["status"] == "editing"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_drafts(self):
        collection = MagicMock()
        collection.find = MagicMock(return_value=_make_mock_cursor([]))

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        drafts = await repo.list_drafts()
        assert drafts == []

    @pytest.mark.asyncio
    async def test_no_filter_passes_empty_query(self):
        docs = [_make_draft_doc(draft_id="d-1")]

        collection = MagicMock()
        collection.find = MagicMock(return_value=_make_mock_cursor(docs))

        repo = KnowledgeDraftRepository(_make_mock_db(collection))

        drafts = await repo.list_drafts()

        assert len(drafts) == 1
        call_args = collection.find.call_args
        query = call_args.args[0]
        assert query == {}
