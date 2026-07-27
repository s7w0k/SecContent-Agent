"""草稿与历史版本 MongoDB 仓库。"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from models.knowledge_management import (
    KnowledgeDraftInDB,
    KnowledgeDraftStatus,
    KnowledgeValidationResult,
    KnowledgeValidationStatus,
)
from pymongo import ReturnDocument

logger = logging.getLogger("backend.knowledge_admin.repository")


class KnowledgeDraftRepository:
    """草稿的 MongoDB 持久化仓库。"""

    def __init__(self, db: Any):
        self._collection = db["knowledge_drafts"]

    @staticmethod
    def _generate_draft_id() -> str:
        date_part = datetime.now(UTC).strftime("%Y%m%d")
        return f"kbd-{date_part}-{secrets.token_hex(3)}"

    @staticmethod
    def _compute_document_id(relative_path: str) -> str:
        """根据规范化相对路径生成文档 ID。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    async def create_draft(
        self,
        relative_path: str,
        base_content_hash: str,
        content_md: str,
        user_id: str,
    ) -> KnowledgeDraftInDB:
        """创建新草稿。同一路径已有 editing 草稿时返回已有草稿。"""
        # Check for existing editing draft for the same path
        existing = await self._collection.find_one({
            "relative_path": relative_path,
            "status": KnowledgeDraftStatus.EDITING,
        })
        if existing:
            return KnowledgeDraftInDB(**existing)

        now = datetime.now(UTC)
        draft = KnowledgeDraftInDB(
            draft_id=self._generate_draft_id(),
            document_id=self._compute_document_id(relative_path),
            relative_path=relative_path,
            base_content_hash=base_content_hash,
            content_md=content_md,
            status=KnowledgeDraftStatus.EDITING,
            validation=KnowledgeValidationResult(
                status=KnowledgeValidationStatus.PENDING,
                errors=[],
                warnings=[],
            ),
            created_by=user_id,
            updated_by=user_id,
            created_at=now,
            updated_at=now,
        )
        await self._collection.insert_one(draft.model_dump(by_alias=True))
        logger.info("Draft created: %s for %s", draft.draft_id, relative_path)
        return draft

    async def get_draft(self, draft_id: str) -> KnowledgeDraftInDB | None:
        """获取草稿。"""
        doc = await self._collection.find_one({"draft_id": draft_id})
        if doc:
            return KnowledgeDraftInDB(**doc)
        return None

    async def update_draft(
        self,
        draft_id: str,
        content_md: str,
        user_id: str,
        change_summary: str = "",
    ) -> KnowledgeDraftInDB | None:
        """更新草稿内容和变更摘要。"""
        now = datetime.now(UTC)
        result = await self._collection.find_one_and_update(
            {"draft_id": draft_id, "status": KnowledgeDraftStatus.EDITING},
            {
                "$set": {
                    "content_md": content_md,
                    "change_summary": change_summary,
                    "updated_by": user_id,
                    "updated_at": now,
                    "validation.status": KnowledgeValidationStatus.PENDING,
                    "validation.errors": [],
                    "validation.warnings": [],
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if result:
            return KnowledgeDraftInDB(**result)
        return None

    async def delete_draft(self, draft_id: str, user_id: str) -> bool:
        """放弃草稿（标记为 abandoned）。"""
        result = await self._collection.update_one(
            {"draft_id": draft_id, "status": KnowledgeDraftStatus.EDITING},
            {
                "$set": {
                    "status": KnowledgeDraftStatus.ABANDONED,
                    "updated_by": user_id,
                    "updated_at": datetime.now(UTC),
                }
            },
        )
        return result.modified_count > 0

    async def list_drafts(
        self,
        relative_path: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[KnowledgeDraftInDB]:
        """列出草稿。"""
        query: dict[str, Any] = {}
        if relative_path:
            query["relative_path"] = relative_path
        if status:
            query["status"] = status
        cursor = self._collection.find(query).sort("updated_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [KnowledgeDraftInDB(**doc) for doc in docs]

    async def update_validation(
        self,
        draft_id: str,
        validation: KnowledgeValidationResult,
    ) -> None:
        """更新草稿的校验结果。"""
        await self._collection.update_one(
            {"draft_id": draft_id},
            {"$set": {"validation": validation.model_dump()}},
        )
