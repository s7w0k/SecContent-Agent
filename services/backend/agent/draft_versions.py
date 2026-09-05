"""Immutable draft version DAG, comparison, primary pointer and rollback."""

from __future__ import annotations

import difflib
import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(UTC)


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


class DraftVersionError(ValueError):
    pass


class DraftReviewStatus(StrEnum):
    REVIEW_PASSED = "review_passed"
    NEEDS_USER_REVIEW = "needs_user_review"
    REVIEW_FAILED = "review_failed"


class DraftVersionNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str
    root_artifact_id: str
    parent_artifact_id: str = ""
    tenant_id: str
    user_id: str
    article_id: str
    product_ids: tuple[str, ...] = ()
    version: int = Field(ge=1)
    content: str
    content_hash: str
    created_by: str
    instruction: str = ""
    review_status: DraftReviewStatus = DraftReviewStatus.NEEDS_USER_REVIEW
    review: dict[str, Any] = Field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=_now)


class DraftPrimaryPointer(BaseModel):
    artifact_id: str
    root_artifact_id: str
    tenant_id: str
    user_id: str
    generation: int = Field(default=1, ge=1)
    idempotency_key: str
    updated_at: datetime = Field(default_factory=_now)


class DraftComparison(BaseModel):
    left_artifact_id: str
    right_artifact_id: str
    added_lines: int
    removed_lines: int
    diff_summary: list[str] = Field(default_factory=list, max_length=100)


class DraftVersionRepository:
    """Repository with a deterministic memory backend and optional Mongo persistence."""

    VERSION_COLLECTION = "agent_draft_artifacts"
    POINTER_COLLECTION = "agent_draft_primary"

    def __init__(self, db: Any = None):
        self.db = db
        self._nodes: dict[str, DraftVersionNode] = {}
        self._pointers: dict[tuple[str, str, str], DraftPrimaryPointer] = {}

    async def create(
        self,
        *,
        tenant_id: str,
        user_id: str,
        article_id: str,
        content: str,
        created_by: str,
        product_ids: list[str] | tuple[str, ...] = (),
        parent_artifact_id: str = "",
        instruction: str = "",
        review_status: DraftReviewStatus = DraftReviewStatus.NEEDS_USER_REVIEW,
        review: dict[str, Any] | None = None,
        source_ids: list[str] | tuple[str, ...] = (),
    ) -> DraftVersionNode:
        if not tenant_id or not user_id or not content.strip():
            raise DraftVersionError("tenant, user and non-empty content are required")
        parent = None
        if parent_artifact_id:
            parent = await self.get(parent_artifact_id, tenant_id=tenant_id, user_id=user_id)
            if parent is None:
                raise DraftVersionError("parent draft version not found")
            if parent.article_id != article_id:
                raise DraftVersionError("parent belongs to a different source article")
        digest = content_hash(content)
        siblings = (
            await self.list_versions(
                (parent.root_artifact_id if parent else ""), tenant_id=tenant_id, user_id=user_id
            )
            if parent
            else []
        )
        for node in siblings:
            if node.parent_artifact_id == parent_artifact_id and node.content_hash == digest:
                return node
        artifact_id = "draft-" + uuid.uuid4().hex[:24]
        root_id = parent.root_artifact_id if parent else artifact_id
        node = DraftVersionNode(
            artifact_id=artifact_id,
            root_artifact_id=root_id,
            parent_artifact_id=parent_artifact_id,
            tenant_id=tenant_id,
            user_id=user_id,
            article_id=article_id,
            product_ids=tuple(product_ids),
            version=(max((item.version for item in siblings), default=0) + 1) if parent else 1,
            content=content,
            content_hash=digest,
            created_by=created_by,
            instruction=instruction,
            review_status=review_status,
            review=dict(review or {}),
            source_ids=tuple(source_ids),
        )
        self._nodes[node.artifact_id] = node
        if self.db is not None:
            doc = node.model_dump(mode="python")
            doc["content_md"] = doc.pop("content")
            await self.db[self.VERSION_COLLECTION].insert_one(doc)
        return node

    async def get(
        self, artifact_id: str, *, tenant_id: str, user_id: str
    ) -> DraftVersionNode | None:
        cached = self._nodes.get(artifact_id)
        if cached:
            return cached if cached.tenant_id == tenant_id and cached.user_id == user_id else None
        if self.db is None:
            return None
        doc = await self.db[self.VERSION_COLLECTION].find_one(
            {"artifact_id": artifact_id, "tenant_id": tenant_id, "user_id": user_id}
        )
        if not doc:
            return None
        doc = dict(doc)
        doc.pop("_id", None)
        doc["content"] = doc.pop("content_md", doc.get("content", ""))
        doc.setdefault("root_artifact_id", doc["artifact_id"])
        doc.setdefault("parent_artifact_id", "")
        doc.setdefault("created_by", "legacy")
        doc.setdefault("review_status", "needs_user_review")
        allowed = set(DraftVersionNode.model_fields)
        node = DraftVersionNode.model_validate({k: v for k, v in doc.items() if k in allowed})
        self._nodes[artifact_id] = node
        return node

    async def list_versions(
        self, root_artifact_id: str, *, tenant_id: str, user_id: str
    ) -> list[DraftVersionNode]:
        nodes = [
            node
            for node in self._nodes.values()
            if node.tenant_id == tenant_id
            and node.user_id == user_id
            and (not root_artifact_id or node.root_artifact_id == root_artifact_id)
        ]
        if self.db is not None and root_artifact_id:
            cursor = self.db[self.VERSION_COLLECTION].find(
                {"root_artifact_id": root_artifact_id, "tenant_id": tenant_id, "user_id": user_id}
            )
            docs = await cursor.to_list(length=500)
            for doc in docs:
                await self.get(str(doc["artifact_id"]), tenant_id=tenant_id, user_id=user_id)
            nodes = [
                node
                for node in self._nodes.values()
                if node.root_artifact_id == root_artifact_id
                and node.tenant_id == tenant_id
                and node.user_id == user_id
            ]
        return sorted(nodes, key=lambda item: (item.created_at, item.artifact_id))

    async def lineage(
        self, artifact_id: str, *, tenant_id: str, user_id: str
    ) -> list[DraftVersionNode]:
        result: list[DraftVersionNode] = []
        seen: set[str] = set()
        current = await self.get(artifact_id, tenant_id=tenant_id, user_id=user_id)
        while current:
            if current.artifact_id in seen:
                raise DraftVersionError("draft version cycle detected")
            seen.add(current.artifact_id)
            result.append(current)
            current = (
                await self.get(current.parent_artifact_id, tenant_id=tenant_id, user_id=user_id)
                if current.parent_artifact_id
                else None
            )
        return result

    async def compare(
        self, left_id: str, right_id: str, *, tenant_id: str, user_id: str
    ) -> DraftComparison:
        left = await self.get(left_id, tenant_id=tenant_id, user_id=user_id)
        right = await self.get(right_id, tenant_id=tenant_id, user_id=user_id)
        if left is None or right is None:
            raise DraftVersionError("draft version not found")
        diff = list(
            difflib.unified_diff(
                left.content.splitlines(), right.content.splitlines(), lineterm="", n=1
            )
        )
        body = [line for line in diff if not line.startswith(("+++", "---", "@@"))]
        return DraftComparison(
            left_artifact_id=left_id,
            right_artifact_id=right_id,
            added_lines=sum(line.startswith("+") for line in body),
            removed_lines=sum(line.startswith("-") for line in body),
            diff_summary=body[:100],
        )

    async def set_primary(
        self,
        artifact_id: str,
        *,
        tenant_id: str,
        user_id: str,
        confirmed: bool,
        idempotency_key: str,
        expected_generation: int | None = None,
    ) -> DraftPrimaryPointer:
        if not confirmed:
            raise DraftVersionError("changing the primary draft requires confirmation")
        node = await self.get(artifact_id, tenant_id=tenant_id, user_id=user_id)
        if node is None:
            raise DraftVersionError("draft version not found")
        key = (tenant_id, user_id, node.root_artifact_id)
        current = self._pointers.get(key)
        if current and current.idempotency_key == idempotency_key:
            return current
        if (
            expected_generation is not None
            and (current.generation if current else 0) != expected_generation
        ):
            raise DraftVersionError("primary pointer generation conflict")
        pointer = DraftPrimaryPointer(
            artifact_id=artifact_id,
            root_artifact_id=node.root_artifact_id,
            tenant_id=tenant_id,
            user_id=user_id,
            generation=(current.generation + 1) if current else 1,
            idempotency_key=idempotency_key,
        )
        self._pointers[key] = pointer
        if self.db is not None:
            await self.db[self.POINTER_COLLECTION].replace_one(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "root_artifact_id": node.root_artifact_id,
                },
                pointer.model_dump(mode="python"),
                upsert=True,
            )
        return pointer

    async def rollback_primary(
        self,
        artifact_id: str,
        *,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        expected_generation: int | None = None,
    ) -> DraftPrimaryPointer:
        return await self.set_primary(
            artifact_id,
            tenant_id=tenant_id,
            user_id=user_id,
            confirmed=True,
            idempotency_key=idempotency_key,
            expected_generation=expected_generation,
        )
