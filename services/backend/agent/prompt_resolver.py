"""PromptResolver - 按用户解析生效提示词版本。

职责：
- get_effective: 获取用户当前生效版本（用户覆盖 > 系统默认）
- freeze_many: 冻结多个提示词的 PromptRef（任务创建时调用）
- resolve_ref: 按任务快照中的 PromptRef 解析精确版本

所有数据库查询包含 user_id，确保多租户隔离。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from agent.prompt_registry import PromptDefinition, get_registry, resolve_prompt_key
from models.user_prompt import (
    EffectivePrompt,
    PromptRef,
    UserPromptVersion,
    compute_content_hash,
)

logger = logging.getLogger("backend.prompt_resolver")

# 每个提示词保留的最大历史版本数
MAX_VERSIONS = 30


class PromptResolver:
    """按用户解析提示词的生效版本。"""

    def __init__(self, db):
        self._db = db
        self._registry = get_registry()

    async def get_effective(
        self,
        user_id: str,
        prompt_key: str,
        *,
        version: int | None = None,
    ) -> EffectivePrompt:
        """获取用户当前生效的提示词。

        继承规则：任务指定版本 > 用户当前生效版本 > 系统默认

        Args:
            user_id: 用户 ID
            prompt_key: 提示词键（支持旧 draft_system 兼容映射）
            version: 指定版本号（任务快照恢复时使用）

        Returns:
            EffectivePrompt，含内容、来源、版本等信息
        """
        resolved_key = resolve_prompt_key(prompt_key)
        definition = self._registry.require(resolved_key)

        # 任务指定版本：从历史版本集合中查找
        if version is not None:
            return await self._resolve_by_version(
                user_id, resolved_key, version, definition
            )

        # 查找用户当前覆盖
        record_doc = await self._db["user_prompts"].find_one(
            {"user_id": user_id, "prompt_key": resolved_key}
        )
        if record_doc is not None:
            return EffectivePrompt(
                prompt_key=resolved_key,
                content=record_doc["content"],
                is_custom=True,
                source="user",
                version=record_doc.get("version", 1),
                default_version=definition.default_version,
                required_placeholders=list(definition.required_placeholders),
                allowed_placeholders=list(definition.allowed_placeholders),
                updated_at=record_doc.get("updated_at"),
            )

        # 系统默认
        return EffectivePrompt(
            prompt_key=resolved_key,
            content=definition.default_content,
            is_custom=False,
            source="system",
            version=definition.default_version,
            default_version=definition.default_version,
            required_placeholders=list(definition.required_placeholders),
            allowed_placeholders=list(definition.allowed_placeholders),
            updated_at=None,
        )

    async def freeze_many(
        self,
        user_id: str,
        prompt_keys: list[str],
    ) -> list[PromptRef]:
        """冻结多个提示词的版本引用（任务创建时调用）。

        返回每个提示词的 PromptRef，包含 source/version/content_hash。
        Worker 后续通过 resolve_ref 加载精确版本。
        """
        refs: list[PromptRef] = []
        for raw_key in prompt_keys:
            resolved = resolve_prompt_key(raw_key)
            effective = await self.get_effective(user_id, resolved)
            ref = PromptRef(
                prompt_key=resolved,
                source=effective.source,
                version=effective.version or effective.default_version,
                content_hash=compute_content_hash(effective.content),
            )
            refs.append(ref)
        return refs

    async def resolve_ref(
        self,
        user_id: str,
        ref: PromptRef,
    ) -> EffectivePrompt:
        """按任务快照中的 PromptRef 解析精确版本。

        找不到任务指定的用户版本时失败，不静默使用其他版本。
        """
        definition = self._registry.require(ref.prompt_key)

        if ref.source == "system":
            return EffectivePrompt(
                prompt_key=ref.prompt_key,
                content=definition.default_content,
                is_custom=False,
                source="system",
                version=ref.version,
                default_version=definition.default_version,
                required_placeholders=list(definition.required_placeholders),
                allowed_placeholders=list(definition.allowed_placeholders),
            )

        # 从历史版本集合查找
        return await self._resolve_by_version(
            user_id, ref.prompt_key, ref.version, definition
        )

    async def save(
        self,
        user_id: str,
        prompt_key: str,
        content: str,
        *,
        expected_version: int | None = None,
    ) -> EffectivePrompt:
        """保存用户提示词覆盖，支持乐观锁。

        - 首次保存：创建用户版本 1
        - 再次保存：匹配 expected_version 后版本加 1
        - 不匹配返回 409 冲突
        - 自动写历史版本
        """
        import hashlib

        resolved = resolve_prompt_key(prompt_key)
        definition = self._registry.require(resolved)

        # 校验必需占位符
        missing = [
            name
            for name in definition.required_placeholders
            if f"{{{name}}}" not in content
        ]
        if missing:
            rendered = ", ".join(f"{{{name}}}" for name in missing)
            raise ValueError(f"MISSING_PLACEHOLDER: 提示词缺少必需占位符: {rendered}")

        # 校验长度
        if len(content) < definition.min_length:
            raise ValueError(
                f"CONTENT_TOO_SHORT: 内容长度 {len(content)} 低于最小要求 {definition.min_length}"
            )
        if len(content) > definition.max_length:
            raise ValueError(
                f"CONTENT_TOO_LONG: 内容长度 {len(content)} 超过最大限制 {definition.max_length}"
            )

        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)

        # 查找现有记录
        existing = await self._db["user_prompts"].find_one(
            {"user_id": user_id, "prompt_key": resolved}
        )

        if existing is None:
            # 首次创建
            if expected_version is not None and expected_version != 0:
                raise ValueError("VERSION_CONFLICT: 首次保存时 expected_version 应为 0 或不传")
            new_version = 1
            change_type = "create"
        else:
            current_version = existing.get("version", 1)
            if expected_version is not None and expected_version != current_version:
                raise ValueError(
                    f"VERSION_CONFLICT: 期望版本 {expected_version}，实际版本 {current_version}"
                )
            new_version = current_version + 1
            change_type = "update"

        # 写入 user_prompts
        await self._db["user_prompts"].update_one(
            {"user_id": user_id, "prompt_key": resolved},
            {
                "$set": {
                    "content": content,
                    "version": new_version,
                    "base_default_version": definition.default_version,
                    "content_hash": content_hash,
                    "enabled": True,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

        # 写入历史版本
        await self._db["user_prompt_versions"].insert_one(
            UserPromptVersion(
                version_id=f"promptv-{uuid4()}",
                user_id=user_id,
                prompt_key=resolved,
                version=new_version,
                content=content,
                content_hash=content_hash,
                base_default_version=definition.default_version,
                change_type=change_type,
                created_at=now,
            ).model_dump()
        )

        # 清理旧版本（保留最近 MAX_VERSIONS 个）
        await self._cleanup_old_versions(user_id, resolved)

        logger.info(
            "Prompt saved: user=%s key=%s version=%d change_type=%s hash=%s",
            user_id,
            resolved,
            new_version,
            change_type,
            content_hash[:16],
        )

        return EffectivePrompt(
            prompt_key=resolved,
            content=content,
            is_custom=True,
            source="user",
            version=new_version,
            default_version=definition.default_version,
            required_placeholders=list(definition.required_placeholders),
            allowed_placeholders=list(definition.allowed_placeholders),
            updated_at=now,
        )

    async def reset(
        self,
        user_id: str,
        prompt_key: str,
    ) -> EffectivePrompt:
        """恢复系统默认提示词。

        删除用户覆盖，保留历史版本。
        """
        resolved = resolve_prompt_key(prompt_key)
        definition = self._registry.require(resolved)

        existing = await self._db["user_prompts"].find_one(
            {"user_id": user_id, "prompt_key": resolved}
        )
        if existing is not None:
            # 记录 reset 操作到历史
            now = datetime.now(UTC)
            await self._db["user_prompt_versions"].insert_one(
                UserPromptVersion(
                    version_id=f"promptv-{uuid4()}",
                    user_id=user_id,
                    prompt_key=resolved,
                    version=existing.get("version", 1),
                    content=existing["content"],
                    content_hash=existing.get("content_hash", ""),
                    base_default_version=definition.default_version,
                    change_type="reset",
                    created_at=now,
                ).model_dump()
            )

            await self._db["user_prompts"].delete_one(
                {"user_id": user_id, "prompt_key": resolved}
            )

            logger.info(
                "Prompt reset: user=%s key=%s",
                user_id,
                resolved,
            )

        return await self.get_effective(user_id, resolved)

    async def list_versions(
        self,
        user_id: str,
        prompt_key: str,
        *,
        page: int = 1,
        page_size: int = 30,
    ) -> dict:
        """分页查询提示词历史版本。"""
        resolved = resolve_prompt_key(prompt_key)
        skip = (page - 1) * page_size

        cursor = (
            self._db["user_prompt_versions"]
            .find({"user_id": user_id, "prompt_key": resolved})
            .sort("version", -1)
            .skip(skip)
            .limit(page_size)
        )
        docs = await cursor.to_list(length=page_size)
        total = await self._db["user_prompt_versions"].count_documents(
            {"user_id": user_id, "prompt_key": resolved}
        )

        versions = []
        for doc in docs:
            doc.pop("_id", None)
            versions.append(UserPromptVersion(**doc).model_dump())

        return {
            "items": versions,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def restore_version(
        self,
        user_id: str,
        prompt_key: str,
        version: int,
    ) -> EffectivePrompt:
        """回滚到历史版本（创建新版本，不倒退版本号）。"""
        resolved = resolve_prompt_key(prompt_key)

        # 查找历史版本
        hist_doc = await self._db["user_prompt_versions"].find_one(
            {"user_id": user_id, "prompt_key": resolved, "version": version}
        )
        if hist_doc is None:
            raise ValueError(f"VERSION_NOT_FOUND: 版本 {version} 不存在")

        content = hist_doc["content"]

        # 使用 save 创建新版本
        # 先删除现有记录以便跳过乐观锁检查
        existing = await self._db["user_prompts"].find_one(
            {"user_id": user_id, "prompt_key": resolved}
        )
        expected = existing.get("version", 0) if existing else None

        result = await self.save(
            user_id, resolved, content, expected_version=expected
        )

        # 标记为 restore
        await self._db["user_prompt_versions"].update_one(
            {"user_id": user_id, "prompt_key": resolved, "version": result.version},
            {"$set": {"change_type": "restore"}},
        )

        logger.info(
            "Prompt restored: user=%s key=%s from_version=%d to_version=%d",
            user_id,
            resolved,
            version,
            result.version,
        )

        return result

    async def _resolve_by_version(
        self,
        user_id: str,
        resolved_key: str,
        version: int,
        definition: PromptDefinition,
    ) -> EffectivePrompt:
        """从历史版本集合中按版本号解析。"""
        hist_doc = await self._db["user_prompt_versions"].find_one(
            {"user_id": user_id, "prompt_key": resolved_key, "version": version}
        )
        if hist_doc is None:
            raise ValueError(
                f"VERSION_NOT_FOUND: 用户 {user_id} 的 {resolved_key} 版本 {version} 不存在"
            )
        return EffectivePrompt(
            prompt_key=resolved_key,
            content=hist_doc["content"],
            is_custom=True,
            source="user",
            version=version,
            default_version=definition.default_version,
            required_placeholders=list(definition.required_placeholders),
            allowed_placeholders=list(definition.allowed_placeholders),
            updated_at=hist_doc.get("created_at"),
        )

    async def _cleanup_old_versions(self, user_id: str, prompt_key: str) -> None:
        """清理旧版本，保留最近 MAX_VERSIONS 个。"""
        total = await self._db["user_prompt_versions"].count_documents(
            {"user_id": user_id, "prompt_key": prompt_key}
        )
        if total <= MAX_VERSIONS:
            return

        # 查找需要删除的旧版本
        cursor = (
            self._db["user_prompt_versions"]
            .find({"user_id": user_id, "prompt_key": prompt_key})
            .sort("version", -1)
            .skip(MAX_VERSIONS)
            .project({"version": 1})
        )
        old_docs = await cursor.to_list(length=total - MAX_VERSIONS)
        old_versions = [doc["version"] for doc in old_docs]

        if old_versions:
            await self._db["user_prompt_versions"].delete_many(
                {
                    "user_id": user_id,
                    "prompt_key": prompt_key,
                    "version": {"$in": old_versions},
                }
            )
            logger.debug(
                "Cleaned up %d old prompt versions for user=%s key=%s",
                len(old_versions),
                user_id,
                prompt_key,
            )
