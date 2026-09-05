"""安全发布服务 - 冲突检测、原子发布、失败恢复、历史和回滚。"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from agent.knowledge import KnowledgeLoader
from agent.knowledge_index import KnowledgeIndexBuilder
from knowledge_admin.file_store import KnowledgeFileStore
from knowledge_admin.repository import KnowledgeDraftRepository
from models.knowledge_management import (
    KnowledgeAuditLog,
    KnowledgeDraftStatus,
    KnowledgePublicationFile,
    KnowledgePublicationInDB,
    KnowledgePublicationStatus,
    KnowledgeRevision,
)
from pymongo import ReturnDocument

logger = logging.getLogger("backend.knowledge_admin.publication")

LOCK_KEY = "global-knowledge-publication"
LOCK_TTL_SECONDS = 300  # 5 minutes


class KnowledgePublicationService:
    """管理草稿发布、历史记录和回滚。"""

    def __init__(self, db: Any, root_dir: str):
        self.db = db
        self.file_store = KnowledgeFileStore(root_dir)
        self.draft_repo = KnowledgeDraftRepository(db)
        self._publications = db["knowledge_publications"]
        self._revisions = db["knowledge_revisions"]
        self._locks = db["knowledge_publish_locks"]
        self._audit_logs = db["knowledge_audit_logs"]

    @staticmethod
    def _generate_id(prefix: str) -> str:
        date_part = datetime.now(UTC).strftime("%Y%m%d")
        return f"{prefix}-{date_part}-{secrets.token_hex(3)}"

    # ── 知识索引（S4-5 对接）────────────────────────────────

    def _build_index_preview(self, content_overrides: dict[str, str] | None):
        """基于（可能覆盖的）内容构建并校验新索引，返回 (manifest, index_version)。

        校验失败抛 ValueError，保证不覆盖线上知识后再尝试构建索引。
        """
        builder = KnowledgeIndexBuilder(self.file_store.root_dir)
        manifest = builder.build_manifest(content_overrides=content_overrides)
        errors = builder.validate(manifest)
        if errors:
            raise ValueError("知识索引校验失败: " + "; ".join(errors))
        return manifest, manifest.index_version

    def _write_index(self, manifest) -> str:
        """原子写入知识索引，返回 index_version。"""
        builder = KnowledgeIndexBuilder(self.file_store.root_dir)
        return builder.write(manifest)

    # ── 发布锁 ───────────────────────────────────────────────

    async def acquire_lock(self, user_id: str, publication_id: str) -> bool:
        """原子获取全局发布锁。"""
        now = datetime.now(UTC)
        expires = datetime.fromtimestamp(now.timestamp() + LOCK_TTL_SECONDS, tz=UTC)
        lock_doc = {
            "lock_key": LOCK_KEY,
            "publication_id": publication_id,
            "owner_user_id": user_id,
            "status": "publishing",
            "acquired_at": now,
            "expires_at": expires,
        }
        try:
            await self._locks.insert_one(lock_doc)
            return True
        except Exception:
            # Lock already exists - check if expired
            existing = await self._locks.find_one({"lock_key": LOCK_KEY})
            if existing and existing.get("expires_at", now) < now:
                # Expired - try to take over
                result = await self._locks.find_one_and_replace(
                    {"lock_key": LOCK_KEY, "expires_at": {"$lt": now}},
                    lock_doc,
                    return_document=ReturnDocument.AFTER,
                )
                return result is not None
            return False

    async def release_lock(self) -> None:
        """释放发布锁。"""
        await self._locks.delete_one({"lock_key": LOCK_KEY})

    # ── 审计日志 ─────────────────────────────────────────────

    async def _log_audit(
        self,
        user_id: str,
        action: str,
        target: str,
        result: str,
        publication_id: str = "",
        detail: dict | None = None,
    ) -> None:
        log = KnowledgeAuditLog(
            audit_id=self._generate_id("kba"),
            user_id=user_id,
            action=action,
            target_type="publication",
            target_id=publication_id or target,
            detail={"result": result, "target": target, **(detail or {})},
            created_at=datetime.now(UTC),
        )
        await self._audit_logs.insert_one(log.model_dump(by_alias=True))

    # ── 发布 ─────────────────────────────────────────────────

    async def publish(
        self,
        draft_ids: list[str],
        version_name: str,
        release_notes: str,
        user_id: str,
    ) -> dict:
        """发布一个或多个草稿到正式文件。

        步骤：
        1. 获取全局锁
        2. 获取所有草稿
        3. 乐观哈希冲突检测
        4. 保存旧内容快照（revisions）
        5. 创建 publication 记录（status=publishing）
        6. 依次原子写入文件
        7. 失败时恢复所有已写入文件
        8. 成功时更新 publication 状态为 published
        9. 标记草稿为 published
        10. 释放锁
        """
        publication_id = self._generate_id("kbp")
        written: list[dict] = []

        # Acquire lock
        if not await self.acquire_lock(user_id, publication_id):
            raise ConflictError("发布锁已被占用，请稍后重试")

        try:
            # Fetch all drafts
            drafts = []
            for draft_id in draft_ids:
                draft = await self.draft_repo.get_draft(draft_id)
                if draft is None:
                    raise ValueError(f"草稿不存在: {draft_id}")
                if draft.status != KnowledgeDraftStatus.EDITING:
                    raise ValueError(f"草稿状态不允许发布: {draft_id} (status={draft.status})")
                drafts.append(draft)

            # Conflict detection - verify base_content_hash matches current file
            files_to_publish: list[dict] = []
            for draft in drafts:
                current_hash = self.file_store.compute_hash(draft.relative_path)
                if current_hash != draft.base_content_hash:
                    raise ConflictError(
                        f"文件已被修改: {draft.relative_path}",
                        current_hash=current_hash,
                        expected_hash=draft.base_content_hash,
                    )
                files_to_publish.append(
                    {
                        "draft": draft,
                        "relative_path": draft.relative_path,
                        "before_content": self.file_store.read_file(draft.relative_path),
                        "before_hash": draft.base_content_hash,
                        "after_content": draft.content_md,
                    }
                )

            # Compute knowledge hash before
            loader = KnowledgeLoader(docs_dir=str(self.file_store.root_dir))
            await loader.load(force=True)
            hash_before = loader._last_hash

            # 构建并校验新索引（发布前预览，不覆盖线上知识）
            content_overrides = {
                item["relative_path"]: item["after_content"] for item in files_to_publish
            }
            new_manifest, new_index_version = self._build_index_preview(content_overrides)

            # Create publication record
            now = datetime.now(UTC)
            pub_files = [
                KnowledgePublicationFile(
                    relative_path=f["relative_path"],
                    content_hash="",
                    revision_id="",
                    before_hash=f["before_hash"],
                    after_hash="",
                )
                for f in files_to_publish
            ]

            publication = KnowledgePublicationInDB(
                publication_id=publication_id,
                version_name=version_name,
                status=KnowledgePublicationStatus.PUBLISHING,
                files=pub_files,
                knowledge_hash_before=hash_before,
                knowledge_hash_after="",
                index_version=new_index_version,
                release_notes=release_notes,
                published_by=user_id,
                published_at=now,
                rollback_of=None,
            )
            await self._publications.insert_one(publication.model_dump(by_alias=True))

            # Write files atomically, saving snapshots
            revisions: list[KnowledgeRevision] = []

            for item in files_to_publish:
                draft = item["draft"]
                rel_path = item["relative_path"]
                before_content = item["before_content"]
                after_content = item["after_content"]
                before_hash = item["before_hash"]

                # Save revision snapshot BEFORE writing
                revision_id = self._generate_id("kbr")
                after_hash = self.file_store.atomic_write(rel_path, after_content)

                revision = KnowledgeRevision(
                    revision_id=revision_id,
                    publication_id=publication_id,
                    draft_id=draft.draft_id,
                    relative_path=rel_path,
                    previous_content_hash=before_hash,
                    new_content_hash=after_hash,
                    diff_summary=draft.change_summary,
                    before_content=before_content,
                    after_content=after_content,
                    change_summary=draft.change_summary,
                    published_by=user_id,
                    published_at=datetime.now(UTC),
                )
                await self._revisions.insert_one(revision.model_dump(by_alias=True))
                revisions.append(revision)

                written.append(
                    {
                        "relative_path": rel_path,
                        "revision_id": revision_id,
                        "before_hash": before_hash,
                        "after_hash": after_hash,
                        "before_content": before_content,
                    }
                )

            # Compute knowledge hash after
            await loader.load(force=True)
            hash_after = loader._last_hash

            # 原子发布知识索引（作为本次知识发布的提交点）
            self._write_index(new_manifest)

            # Update publication record
            await self._publications.update_one(
                {"publication_id": publication_id},
                {
                    "$set": {
                        "status": KnowledgePublicationStatus.PUBLISHED,
                        "knowledge_hash_after": hash_after,
                        "files": [
                            {
                                "relative_path": w["relative_path"],
                                "revision_id": w["revision_id"],
                                "content_hash": w["after_hash"],
                                "before_hash": w["before_hash"],
                                "after_hash": w["after_hash"],
                            }
                            for w in written
                        ],
                    }
                },
            )

            # Mark drafts as published
            for draft in drafts:
                await self.draft_repo._collection.update_one(
                    {"draft_id": draft.draft_id},
                    {"$set": {"status": KnowledgeDraftStatus.PUBLISHED}},
                )

            # Audit log
            await self._log_audit(
                user_id,
                "knowledge.publish",
                ", ".join(d.relative_path for d in drafts),
                "success",
                publication_id,
            )

            logger.info("Publication %s completed: %d files", publication_id, len(written))
            return {
                "publication_id": publication_id,
                "status": "published",
                "knowledge_hash_before": hash_before,
                "knowledge_hash_after": hash_after,
                "index_version": new_index_version,
                "changed_files": len(written),
            }

        except ConflictError:
            raise
        except Exception as exc:
            # Failure recovery: restore all written files
            logger.error("Publication failed, attempting recovery: %s", exc)
            for item in written:
                try:
                    self.file_store.atomic_write(item["relative_path"], item["before_content"])
                    logger.info("Restored: %s", item["relative_path"])
                except Exception as restore_exc:
                    logger.error("Failed to restore %s: %s", item["relative_path"], restore_exc)

            # Update publication status to failed
            await self._publications.update_one(
                {"publication_id": publication_id},
                {"$set": {"status": KnowledgePublicationStatus.FAILED}},
            )

            await self._log_audit(
                user_id,
                "knowledge.publish",
                "",
                "failed",
                publication_id,
                {"error": str(exc)},
            )
            raise

        finally:
            await self.release_lock()

    # ── 发布历史 ─────────────────────────────────────────────

    async def list_publications(self, limit: int = 20) -> list[dict]:
        """列出发布历史。"""
        cursor = (
            self._publications.find({"status": {"$in": ["published", "rolled_back"]}})
            .sort("published_at", -1)
            .limit(limit)
        )
        docs = await cursor.to_list(length=limit)
        return docs

    async def get_publication(self, publication_id: str) -> dict | None:
        """获取发布详情。"""
        pub = await self._publications.find_one({"publication_id": publication_id})
        if not pub:
            return None
        # Get revisions
        revisions = await self._revisions.find({"publication_id": publication_id}).to_list(
            length=100
        )
        return {"publication": pub, "revisions": revisions}

    # ── 回滚 ─────────────────────────────────────────────────

    async def rollback(
        self,
        publication_id: str,
        reason: str,
        user_id: str,
    ) -> dict:
        """回滚到指定发布前的状态。

        回滚也创建一次新的发布记录（rollback_of 指向原发布）。
        """
        original = await self._publications.find_one({"publication_id": publication_id})
        if original is None:
            raise ValueError(f"发布记录不存在: {publication_id}")
        if original["status"] != "published":
            raise ValueError(f"只能回滚已发布的记录，当前状态: {original['status']}")

        # Get revisions for this publication
        revisions = await self._revisions.find({"publication_id": publication_id}).to_list(
            length=100
        )

        if not revisions:
            raise ValueError("没有找到修订记录")

        # Create rollback publication
        rollback_pub_id = self._generate_id("kbp")

        # Acquire lock
        if not await self.acquire_lock(user_id, rollback_pub_id):
            raise ConflictError("发布锁已被占用，请稍后重试")

        try:
            # Restore each file to its before_content
            for rev in revisions:
                self.file_store.atomic_write(rev["relative_path"], rev["before_content"])

            # Compute hash after rollback
            loader = KnowledgeLoader(docs_dir=str(self.file_store.root_dir))
            await loader.load(force=True)
            hash_after = loader._last_hash

            # 重建并原子写入知识索引（内容已回滚到历史版本）
            try:
                new_manifest, index_version = self._build_index_preview(None)
                self._write_index(new_manifest)
            except Exception as rollback_exc:
                logger.error("回滚后重建索引失败: %s", rollback_exc)
                index_version = ""

            # Mark original as rolled_back
            await self._publications.update_one(
                {"publication_id": publication_id},
                {
                    "$set": {
                        "status": KnowledgePublicationStatus.ROLLED_BACK,
                        "rolled_back_at": datetime.now(UTC),
                        "rollback_reason": reason,
                    }
                },
            )

            # Create rollback publication record
            rollback_pub = KnowledgePublicationInDB(
                publication_id=rollback_pub_id,
                version_name=f"Rollback of {original.get('version_name', publication_id)}",
                status=KnowledgePublicationStatus.PUBLISHED,
                files=[
                    KnowledgePublicationFile(
                        relative_path=rev["relative_path"],
                        content_hash=rev.get("previous_content_hash", ""),
                        revision_id=rev["revision_id"],
                        before_hash=rev.get("new_content_hash", ""),
                        after_hash=rev.get("previous_content_hash", ""),
                    )
                    for rev in revisions
                ],
                knowledge_hash_before=original.get("knowledge_hash_after", ""),
                knowledge_hash_after=hash_after,
                index_version=index_version,
                release_notes=f"回滚原因: {reason}",
                published_by=user_id,
                published_at=datetime.now(UTC),
                rollback_of=publication_id,
            )
            await self._publications.insert_one(rollback_pub.model_dump(by_alias=True))

            await self._log_audit(
                user_id,
                "knowledge.rollback",
                publication_id,
                "success",
                rollback_pub_id,
                {"reason": reason, "original_publication": publication_id},
            )

            logger.info(
                "Rollback %s completed for publication %s",
                rollback_pub_id,
                publication_id,
            )
            return {
                "publication_id": rollback_pub_id,
                "status": "published",
                "rolled_back_from": publication_id,
                "restored_files": len(revisions),
            }

        except Exception as exc:
            logger.error("Rollback failed: %s", exc)
            await self._log_audit(
                user_id,
                "knowledge.rollback",
                publication_id,
                "failed",
                rollback_pub_id,
                {"error": str(exc)},
            )
            raise

        finally:
            await self.release_lock()


class ConflictError(Exception):
    """乐观并发冲突。"""

    def __init__(
        self,
        message: str,
        current_hash: str = "",
        expected_hash: str = "",
    ):
        super().__init__(message)
        self.current_hash = current_hash
        self.expected_hash = expected_hash
