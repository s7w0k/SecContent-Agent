"""知识库发布服务单元测试 - 原子发布、历史和回滚。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from knowledge_admin.file_store import KnowledgeFileStore
from knowledge_admin.publication import ConflictError, KnowledgePublicationService
from models.knowledge_management import KnowledgeDraftStatus

# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


def _compute_hash(content: str) -> str:
    """计算内容的 SHA-256 哈希（与 file_store.compute_hash 一致）。"""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _make_draft_doc(
    draft_id: str = "kbd-20260101-abcdef",
    relative_path: str = "test.md",
    content_md: str = "# New Content",
    base_content_hash: str = "",
    change_summary: str = "更新了内容",
    status: str = "editing",
) -> dict[str, Any]:
    """构造一个草稿文档字典，模拟 MongoDB 中存储的文档。"""
    return {
        "_id": None,
        "draft_id": draft_id,
        "document_id": "doc-hash-123",
        "relative_path": relative_path,
        "base_content_hash": base_content_hash,
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


def _make_mock_db(collections: dict[str, MagicMock]) -> MagicMock:
    """构造一个 mock 数据库，按集合名返回对应的 mock collection。"""
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: collections.get(name, MagicMock())
    return db


def _make_mock_cursor(docs: list[dict[str, Any]]) -> MagicMock:
    """构造一个模拟 Motor cursor 的链式调用 mock。"""
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=docs)
    return cursor


def _make_mock_loader(hash_value: str = "fake-knowledge-hash") -> MagicMock:
    """构造一个 mock KnowledgeLoader。"""
    loader = MagicMock()
    loader.load = AsyncMock()
    loader._last_hash = hash_value
    return loader


# ═══════════════════════════════════════════════════════════════
# KnowledgeFileStore - 原子写入
# ═══════════════════════════════════════════════════════════════


class TestFileStoreAtomicWrite:
    """atomic_write 测试。"""

    def test_writes_content_correctly(self, tmp_path):
        """atomic_write 正确写入文件内容并返回哈希。"""
        store = KnowledgeFileStore(tmp_path)
        original = "# Original"
        (tmp_path / "test.md").write_text(original, encoding="utf-8")

        new_content = "# Updated Content"
        result_hash = store.atomic_write("test.md", new_content)

        # 文件内容已更新
        written = (tmp_path / "test.md").read_text(encoding="utf-8")
        assert written == new_content

        # 返回的哈希与写入内容匹配
        assert result_hash == _compute_hash(new_content)

    def test_uses_temp_file_not_md_suffix(self, tmp_path):
        """atomic_write 使用 .kbp-tmp 后缀的临时文件（不是 .md）。"""
        store = KnowledgeFileStore(tmp_path)
        (tmp_path / "test.md").write_text("# Original", encoding="utf-8")

        with patch(
            "knowledge_admin.file_store.tempfile.mkstemp",
            wraps=tempfile.mkstemp,
        ) as mock_mkstemp:
            store.atomic_write("test.md", "# New Content")

        mock_mkstemp.assert_called_once()
        _, kwargs = mock_mkstemp.call_args
        assert kwargs.get("suffix") == ".kbp-tmp"
        assert ".md" not in kwargs.get("suffix", "")

    def test_creates_parent_directories(self, tmp_path):
        """atomic_write 自动创建父目录。"""
        store = KnowledgeFileStore(tmp_path)
        content = "# Deep File"
        result_hash = store.atomic_write("sub/dir/deep.md", content)

        assert (tmp_path / "sub" / "dir" / "deep.md").exists()
        written = (tmp_path / "sub" / "dir" / "deep.md").read_text(encoding="utf-8")
        assert written == content
        assert result_hash == _compute_hash(content)

    def test_no_temp_files_left_after_write(self, tmp_path):
        """原子写入后不留临时文件。"""
        store = KnowledgeFileStore(tmp_path)
        (tmp_path / "test.md").write_text("# Original", encoding="utf-8")

        store.atomic_write("test.md", "# New Content")

        # 目录中只有 test.md，没有 .kbp-tmp 文件
        all_files = list(tmp_path.iterdir())
        assert len(all_files) == 1
        assert all_files[0].name == "test.md"


# ═══════════════════════════════════════════════════════════════
# KnowledgeFileStore - 路径校验
# ═══════════════════════════════════════════════════════════════


class TestFileStorePathValidation:
    """路径安全校验测试。"""

    def test_reject_parent_traversal(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        with pytest.raises(ValueError, match=r"\.\."):
            store.read_file("../../../etc/passwd.md")

    def test_reject_absolute_path(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        with pytest.raises(ValueError):
            store.read_file("/etc/passwd.md")

    def test_reject_non_md_file(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        with pytest.raises(ValueError, match="Markdown"):
            store.read_file("test.txt")

    def test_reject_empty_path(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        with pytest.raises(ValueError, match="空"):
            store.read_file("")

    def test_reject_symlink(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        # 创建一个真实文件和指向它的符号链接
        real_file = tmp_path / "real.md"
        real_file.write_text("# Real", encoding="utf-8")
        link = tmp_path / "link.md"
        try:
            os.symlink(real_file, link)
        except (OSError, NotImplementedError):
            pytest.skip("Symbolic links not supported on this platform")

        with pytest.raises(ValueError, match="符号链接"):
            store.read_file("link.md")

    def test_read_file_raises_file_not_found(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.read_file("nonexistent.md")

    def test_compute_hash_returns_sha256(self, tmp_path):
        store = KnowledgeFileStore(tmp_path)
        content = "# Test Content"
        (tmp_path / "test.md").write_text(content, encoding="utf-8")

        result = store.compute_hash("test.md")
        assert result == _compute_hash(content)
        assert result.startswith("sha256:")


# ═══════════════════════════════════════════════════════════════
# KnowledgePublicationService - 发布锁
# ═══════════════════════════════════════════════════════════════


class TestPublicationLock:
    """acquire_lock / release_lock 测试。"""

    @pytest.mark.asyncio
    async def test_acquire_lock_succeeds_when_no_existing_lock(self, tmp_path):
        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": MagicMock(),
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.acquire_lock("u-admin", "kbp-test-001")
        assert result is True
        locks.insert_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_acquire_lock_fails_when_lock_exists_and_not_expired(self, tmp_path):
        locks = MagicMock()
        locks.insert_one = AsyncMock(side_effect=Exception("Duplicate key"))
        # Existing lock that has NOT expired
        future = datetime.now(UTC).timestamp() + 100
        locks.find_one = AsyncMock(
            return_value={
                "lock_key": "global-knowledge-publication",
                "expires_at": datetime.fromtimestamp(future, tz=UTC),
            }
        )

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": MagicMock(),
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.acquire_lock("u-admin", "kbp-test-001")
        assert result is False

    @pytest.mark.asyncio
    async def test_acquire_lock_takes_over_when_expired(self, tmp_path):
        locks = MagicMock()
        locks.insert_one = AsyncMock(side_effect=Exception("Duplicate key"))
        # Existing lock that HAS expired
        past = datetime.now(UTC).timestamp() - 100
        locks.find_one = AsyncMock(
            return_value={
                "lock_key": "global-knowledge-publication",
                "expires_at": datetime.fromtimestamp(past, tz=UTC),
            }
        )
        locks.find_one_and_replace = AsyncMock(
            return_value={
                "lock_key": "global-knowledge-publication",
                "publication_id": "kbp-test-002",
            }
        )

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": MagicMock(),
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.acquire_lock("u-admin", "kbp-test-002")
        assert result is True
        locks.find_one_and_replace.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_lock_deletes_lock(self, tmp_path):
        locks = MagicMock()
        locks.delete_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": MagicMock(),
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        await service.release_lock()
        locks.delete_one.assert_called_once_with({"lock_key": "global-knowledge-publication"})


# ═══════════════════════════════════════════════════════════════
# KnowledgePublicationService - 发布
# ═══════════════════════════════════════════════════════════════


class TestPublish:
    """publish 测试。"""

    @pytest.mark.asyncio
    async def test_publish_single_draft(self, tmp_path):
        """发布单个草稿，文件被正确写入。"""
        original_content = "# Original Content"
        new_content = "# Updated Content"
        (tmp_path / "test.md").write_text(original_content, encoding="utf-8")
        base_hash = _compute_hash(original_content)

        # 设置 mock collections
        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()

        drafts = MagicMock()
        drafts.find_one = AsyncMock(
            return_value=_make_draft_doc(
                relative_path="test.md",
                content_md=new_content,
                base_content_hash=base_hash,
            )
        )
        drafts.update_one = AsyncMock()

        publications = MagicMock()
        publications.insert_one = AsyncMock()
        publications.update_one = AsyncMock()

        revisions = MagicMock()
        revisions.insert_one = AsyncMock()

        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": drafts,
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": audit_logs,
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with patch(
            "knowledge_admin.publication.KnowledgeLoader",
            return_value=_make_mock_loader("fake-hash"),
        ):
            result = await service.publish(
                draft_ids=["kbd-20260101-abcdef"],
                version_name="v1.0.0",
                release_notes="首次发布",
                user_id="u-admin",
            )

        # 验证返回结果
        assert result["status"] == "published"
        assert result["changed_files"] == 1
        assert result["knowledge_hash_before"] == "fake-hash"
        assert result["knowledge_hash_after"] == "fake-hash"

        # 验证文件已更新
        written = (tmp_path / "test.md").read_text(encoding="utf-8")
        assert written == new_content

        # 验证 publication 记录已创建
        publications.insert_one.assert_called_once()
        publications.update_one.assert_called_once()

        # 验证 revision 已创建
        revisions.insert_one.assert_called_once()

        # 验证锁已释放
        locks.delete_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_detects_hash_conflict(self, tmp_path):
        """当文件已被修改时，发布检测到哈希冲突并抛出 ConflictError。"""
        original_content = "# Original Content"
        modified_content = "# Modified By Someone Else"
        new_content = "# Updated Content"

        # 写入原始内容
        (tmp_path / "test.md").write_text(original_content, encoding="utf-8")
        base_hash = _compute_hash(original_content)

        # 设置 mock collections
        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()

        drafts = MagicMock()
        drafts.find_one = AsyncMock(
            return_value=_make_draft_doc(
                relative_path="test.md",
                content_md=new_content,
                base_content_hash=base_hash,
            )
        )

        publications = MagicMock()
        revisions = MagicMock()
        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": drafts,
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": audit_logs,
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        # 在 publish 之前修改文件（模拟文件已被其他人修改）
        (tmp_path / "test.md").write_text(modified_content, encoding="utf-8")

        with pytest.raises(ConflictError, match="文件已被修改"):
            await service.publish(
                draft_ids=["kbd-20260101-abcdef"],
                version_name="v1.0.0",
                release_notes="",
                user_id="u-admin",
            )

        # 验证文件未被修改（仍是 modified_content，不是 new_content）
        assert (tmp_path / "test.md").read_text(encoding="utf-8") == modified_content

        # 验证锁已释放
        locks.delete_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_marks_drafts_as_published(self, tmp_path):
        """发布后草稿状态被标记为 published。"""
        original_content = "# Original"
        new_content = "# New"
        (tmp_path / "test.md").write_text(original_content, encoding="utf-8")
        base_hash = _compute_hash(original_content)

        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()

        drafts = MagicMock()
        drafts.find_one = AsyncMock(
            return_value=_make_draft_doc(
                relative_path="test.md",
                content_md=new_content,
                base_content_hash=base_hash,
            )
        )
        drafts.update_one = AsyncMock()

        publications = MagicMock()
        publications.insert_one = AsyncMock()
        publications.update_one = AsyncMock()

        revisions = MagicMock()
        revisions.insert_one = AsyncMock()

        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": drafts,
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": audit_logs,
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with patch(
            "knowledge_admin.publication.KnowledgeLoader",
            return_value=_make_mock_loader(),
        ):
            await service.publish(
                draft_ids=["kbd-20260101-abcdef"],
                version_name="v1.0",
                release_notes="",
                user_id="u-admin",
            )

        # 验证 draft 状态被更新为 published
        drafts.update_one.assert_called_once()
        call_args = drafts.update_one.call_args
        query = call_args.args[0]
        update_doc = call_args.args[1]
        assert query["draft_id"] == "kbd-20260101-abcdef"
        assert update_doc["$set"]["status"] == KnowledgeDraftStatus.PUBLISHED

    @pytest.mark.asyncio
    async def test_publish_raises_on_nonexistent_draft(self, tmp_path):
        """发布不存在的草稿时抛出 ValueError。"""
        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()

        drafts = MagicMock()
        drafts.find_one = AsyncMock(return_value=None)

        publications = MagicMock()
        publications.update_one = AsyncMock()

        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": drafts,
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": audit_logs,
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ValueError, match="草稿不存在"):
            await service.publish(
                draft_ids=["nonexistent"],
                version_name="v1",
                release_notes="",
                user_id="u-admin",
            )

    @pytest.mark.asyncio
    async def test_publish_raises_on_non_editing_draft(self, tmp_path):
        """发布非 editing 状态的草稿时抛出 ValueError。"""
        (tmp_path / "test.md").write_text("# Content", encoding="utf-8")

        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()

        drafts = MagicMock()
        drafts.find_one = AsyncMock(
            return_value=_make_draft_doc(
                status="published",
            )
        )

        publications = MagicMock()
        publications.update_one = AsyncMock()

        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": drafts,
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": audit_logs,
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ValueError, match="草稿状态不允许发布"):
            await service.publish(
                draft_ids=["kbd-20260101-abcdef"],
                version_name="v1",
                release_notes="",
                user_id="u-admin",
            )

    @pytest.mark.asyncio
    async def test_publish_fails_when_lock_busy(self, tmp_path):
        """发布锁被占用时抛出 ConflictError。"""
        locks = MagicMock()
        locks.insert_one = AsyncMock(side_effect=Exception("Duplicate"))
        locks.find_one = AsyncMock(
            return_value={
                "lock_key": "global-knowledge-publication",
                "expires_at": datetime.fromtimestamp(datetime.now(UTC).timestamp() + 100, tz=UTC),
            }
        )

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": MagicMock(),
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ConflictError, match="发布锁已被占用"):
            await service.publish(
                draft_ids=["kbd-001"],
                version_name="v1",
                release_notes="",
                user_id="u-admin",
            )


# ═══════════════════════════════════════════════════════════════
# KnowledgePublicationService - 发布历史
# ═══════════════════════════════════════════════════════════════


class TestListPublications:
    """list_publications 测试。"""

    @pytest.mark.asyncio
    async def test_list_publications_returns_docs(self, tmp_path):
        """list_publications 返回发布历史列表。"""
        docs = [
            {"publication_id": "kbp-001", "status": "published"},
            {"publication_id": "kbp-002", "status": "rolled_back"},
        ]

        publications = MagicMock()
        publications.find = MagicMock(return_value=_make_mock_cursor(docs))

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.list_publications(limit=10)

        assert len(result) == 2
        assert result[0]["publication_id"] == "kbp-001"

    @pytest.mark.asyncio
    async def test_list_publications_empty(self, tmp_path):
        """list_publications 空列表。"""
        publications = MagicMock()
        publications.find = MagicMock(return_value=_make_mock_cursor([]))

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.list_publications()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_publication_returns_detail(self, tmp_path):
        """get_publication 返回发布详情和修订记录。"""
        pub_doc = {"publication_id": "kbp-001", "status": "published"}
        rev_docs = [
            {"revision_id": "kbr-001", "relative_path": "test.md"},
        ]

        publications = MagicMock()
        publications.find_one = AsyncMock(return_value=pub_doc)

        revisions = MagicMock()
        revisions.find = MagicMock(return_value=_make_mock_cursor(rev_docs))

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.get_publication("kbp-001")

        assert result is not None
        assert result["publication"]["publication_id"] == "kbp-001"
        assert len(result["revisions"]) == 1

    @pytest.mark.asyncio
    async def test_get_publication_returns_none_for_nonexistent(self, tmp_path):
        """get_publication 对不存在的 ID 返回 None。"""
        publications = MagicMock()
        publications.find_one = AsyncMock(return_value=None)

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))
        result = await service.get_publication("nonexistent")
        assert result is None


# ═══════════════════════════════════════════════════════════════
# KnowledgePublicationService - 回滚
# ═══════════════════════════════════════════════════════════════


class TestRollback:
    """rollback 测试。"""

    @pytest.mark.asyncio
    async def test_rollback_restores_old_content(self, tmp_path):
        """回滚后文件内容恢复到发布前。"""
        before_content = "# Before Publish"
        after_content = "# After Publish"

        # 当前文件是发布后的内容
        (tmp_path / "test.md").write_text(after_content, encoding="utf-8")

        original_pub = {
            "publication_id": "kbp-001",
            "status": "published",
            "version_name": "v1.0",
            "knowledge_hash_after": "hash-after",
        }

        revision_docs = [
            {
                "revision_id": "kbr-001",
                "publication_id": "kbp-001",
                "relative_path": "test.md",
                "before_content": before_content,
                "after_content": after_content,
                "previous_content_hash": _compute_hash(before_content),
                "new_content_hash": _compute_hash(after_content),
            },
        ]

        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()

        publications = MagicMock()
        publications.find_one = AsyncMock(return_value=original_pub)
        publications.update_one = AsyncMock()
        publications.insert_one = AsyncMock()

        revisions = MagicMock()
        revisions.find = MagicMock(return_value=_make_mock_cursor(revision_docs))

        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": audit_logs,
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with patch(
            "knowledge_admin.publication.KnowledgeLoader",
            return_value=_make_mock_loader("hash-after-rollback"),
        ):
            result = await service.rollback(
                publication_id="kbp-001",
                reason="内容有误",
                user_id="u-admin",
            )

        # 验证文件已恢复到 before_content
        written = (tmp_path / "test.md").read_text(encoding="utf-8")
        assert written == before_content

        # 验证返回结果
        assert result["status"] == "published"
        assert result["rolled_back_from"] == "kbp-001"
        assert result["restored_files"] == 1

        # 验证原发布被标记为 rolled_back
        publications.update_one.assert_called_once()
        update_call = publications.update_one.call_args
        assert update_call.args[0]["publication_id"] == "kbp-001"
        assert update_call.args[1]["$set"]["status"] == "rolled_back"

        # 验证创建了回滚发布记录
        publications.insert_one.assert_called_once()

        # 验证锁已释放
        locks.delete_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_rollback_raises_for_nonexistent_publication(self, tmp_path):
        """回滚不存在的发布记录时抛出 ValueError。"""
        publications = MagicMock()
        publications.find_one = AsyncMock(return_value=None)

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ValueError, match="发布记录不存在"):
            await service.rollback(
                publication_id="nonexistent",
                reason="测试",
                user_id="u-admin",
            )

    @pytest.mark.asyncio
    async def test_rollback_raises_for_non_published_status(self, tmp_path):
        """回滚非 published 状态的发布记录时抛出 ValueError。"""
        publications = MagicMock()
        publications.find_one = AsyncMock(
            return_value={
                "publication_id": "kbp-001",
                "status": "failed",
            }
        )

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": MagicMock(),
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ValueError, match="只能回滚已发布的记录"):
            await service.rollback(
                publication_id="kbp-001",
                reason="测试",
                user_id="u-admin",
            )

    @pytest.mark.asyncio
    async def test_rollback_raises_when_no_revisions(self, tmp_path):
        """回滚没有修订记录的发布时抛出 ValueError。"""
        publications = MagicMock()
        publications.find_one = AsyncMock(
            return_value={
                "publication_id": "kbp-001",
                "status": "published",
            }
        )

        revisions = MagicMock()
        revisions.find = MagicMock(return_value=_make_mock_cursor([]))

        db = _make_mock_db(
            {
                "knowledge_publish_locks": MagicMock(),
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ValueError, match="没有找到修订记录"):
            await service.rollback(
                publication_id="kbp-001",
                reason="测试",
                user_id="u-admin",
            )

    @pytest.mark.asyncio
    async def test_rollback_fails_when_lock_busy(self, tmp_path):
        """回滚时锁被占用则抛出 ConflictError。"""
        publications = MagicMock()
        publications.find_one = AsyncMock(
            return_value={
                "publication_id": "kbp-001",
                "status": "published",
            }
        )

        revisions = MagicMock()
        revisions.find = MagicMock(
            return_value=_make_mock_cursor(
                [
                    {
                        "revision_id": "kbr-001",
                        "relative_path": "test.md",
                        "before_content": "# Before",
                        "after_content": "# After",
                        "previous_content_hash": "h1",
                        "new_content_hash": "h2",
                    },
                ]
            )
        )

        locks = MagicMock()
        locks.insert_one = AsyncMock(side_effect=Exception("Duplicate"))
        locks.find_one = AsyncMock(
            return_value={
                "lock_key": "global-knowledge-publication",
                "expires_at": datetime.fromtimestamp(datetime.now(UTC).timestamp() + 100, tz=UTC),
            }
        )

        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": MagicMock(),
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": MagicMock(),
            }
        )

        service = KnowledgePublicationService(db, str(tmp_path))

        with pytest.raises(ConflictError, match="发布锁已被占用"):
            await service.rollback(
                publication_id="kbp-001",
                reason="测试",
                user_id="u-admin",
            )
