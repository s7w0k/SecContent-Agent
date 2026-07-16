"""Resolve and persist tenant-scoped PR template overrides."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.pr_templates import PR_TEMPLATES, PRTemplate, get_system_template, match_templates
from models.pr_template import (
    TEMPLATE_IDENTITY,
    EffectivePRTemplate,
    TemplateChangeType,
    TemplateKey,
    TemplateSection,
    TemplateSnapshot,
    TemplateSource,
    UserPRTemplate,
    UserPRTemplateUpdate,
    UserPRTemplateVersion,
)
from pymongo import DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError


class TemplateNotFoundError(LookupError):
    """Raised when a stable system template key does not exist."""


class TemplateVersionConflictError(RuntimeError):
    """Raised when an override was changed after the caller read it."""


class TemplateRepository:
    """MongoDB repository shared by API and worker template resolution."""

    def __init__(self, db: Any) -> None:
        if db is None:
            raise ValueError("db is required")
        self._templates = db["user_pr_templates"]
        self._versions = db["user_pr_template_versions"]

    async def list_effective_templates(
        self,
        user_id: str,
        category_v2: str | None = None,
    ) -> list[EffectivePRTemplate]:
        """Return system templates merged with this user's enabled overrides."""
        user_id = self._require_user_id(user_id)
        systems = (
            match_templates(category_v2)
            if category_v2 is not None
            else [template for group in PR_TEMPLATES.values() for template in group]
        )
        if not systems:
            return []

        keys = [template.template_key for template in systems]
        cursor = self._templates.find(
            {"user_id": user_id, "template_key": {"$in": keys}, "enabled": True}
        )
        documents = await cursor.to_list(length=len(keys))
        overrides = {
            str(document["template_key"]): self._document_to_user_template(document)
            for document in documents
        }
        return [
            self._to_effective(overrides.get(system.template_key), system) for system in systems
        ]

    async def resolve(self, user_id: str, category_v2: str) -> list[EffectivePRTemplate]:
        """Resolve the A/B template pair for a V2 category."""
        return await self.list_effective_templates(user_id, category_v2)

    async def get_override(
        self,
        user_id: str,
        template_key: str | TemplateKey,
    ) -> UserPRTemplate | None:
        """Read one current user override without falling back to the system template."""
        user_id = self._require_user_id(user_id)
        key = self._require_system_template(template_key).template_key
        document = await self._templates.find_one({"user_id": user_id, "template_key": key})
        return self._document_to_user_template(document) if document else None

    async def save(
        self,
        user_id: str,
        template_key: str | TemplateKey,
        update: UserPRTemplateUpdate,
    ) -> EffectivePRTemplate:
        """Create or atomically update a tenant override using optimistic locking."""
        user_id = self._require_user_id(user_id)
        system = self._require_system_template(template_key)
        current_document = await self._templates.find_one(
            {"user_id": user_id, "template_key": system.template_key}
        )
        now = datetime.now(UTC)

        if current_document is None:
            if update.expected_version not in (None, system.system_version):
                raise TemplateVersionConflictError(
                    f"template {system.template_key} version changed; reload before saving"
                )
            category, slot = TEMPLATE_IDENTITY[TemplateKey(system.template_key)]
            override = UserPRTemplate(
                user_id=user_id,
                template_key=system.template_key,
                base_system_version=system.system_version,
                category_v2=category,
                slot=slot,
                version=1,
                created_at=now,
                updated_at=now,
                **self._content_values(update),
            )
            try:
                await self._templates.insert_one(self._mongo_document(override))
            except DuplicateKeyError as exc:
                raise TemplateVersionConflictError(
                    f"template {system.template_key} was created concurrently"
                ) from exc
            await self._record_version(override, TemplateChangeType.CREATE)
            return self._to_effective(override, system)

        current = self._document_to_user_template(current_document)
        expected_version = update.expected_version
        if expected_version is not None and expected_version != current.version:
            raise TemplateVersionConflictError(
                f"expected version {expected_version}, current version is {current.version}"
            )

        new_version = current.version + 1
        updated_document = await self._templates.find_one_and_update(
            {
                "user_id": user_id,
                "template_key": system.template_key,
                "version": current.version,
            },
            {
                "$set": {
                    **self._content_values(update),
                    "base_system_version": system.system_version,
                    "enabled": True,
                    "updated_at": now,
                },
                "$inc": {"version": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated_document is None:
            raise TemplateVersionConflictError(
                f"template {system.template_key} version changed; reload before saving"
            )
        updated = self._document_to_user_template(updated_document)
        if updated.version != new_version:
            raise TemplateVersionConflictError("template version did not advance atomically")
        await self._record_version(updated, TemplateChangeType.UPDATE)
        return self._to_effective(updated, system)

    async def reset(
        self,
        user_id: str,
        template_key: str | TemplateKey,
    ) -> EffectivePRTemplate:
        """Delete only this tenant's override and return the current system default."""
        user_id = self._require_user_id(user_id)
        system = self._require_system_template(template_key)
        await self._templates.find_one_and_delete(
            {"user_id": user_id, "template_key": system.template_key}
        )
        return self._to_effective(None, system)

    async def list_versions(
        self,
        user_id: str,
        template_key: str | TemplateKey,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[UserPRTemplateVersion]:
        """Read one tenant's immutable history, newest version first."""
        user_id = self._require_user_id(user_id)
        key = self._require_system_template(template_key).template_key
        if offset < 0:
            raise ValueError("offset must not be negative")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        cursor = (
            self._versions.find({"user_id": user_id, "template_key": key})
            .sort("version", DESCENDING)
            .skip(offset)
            .limit(limit)
        )
        documents = await cursor.to_list(length=limit)
        return [
            UserPRTemplateVersion.model_validate(self._normalize_document(document))
            for document in documents
        ]

    @staticmethod
    def to_snapshot(template: EffectivePRTemplate | UserPRTemplate) -> TemplateSnapshot:
        """Freeze editable template content and stable identity for a task or history row."""
        return TemplateSnapshot.model_validate(
            template.model_dump(
                include={
                    "template_key",
                    "category_v2",
                    "slot",
                    "name",
                    "title_template",
                    "sections",
                    "perspectives",
                    "extra_instructions",
                }
            )
        )

    async def _record_version(
        self,
        template: UserPRTemplate,
        change_type: TemplateChangeType,
    ) -> None:
        version = UserPRTemplateVersion(
            template_id=template.template_id,
            user_id=template.user_id,
            template_key=template.template_key,
            version=template.version,
            snapshot=self.to_snapshot(template),
            change_type=change_type,
        )
        await self._versions.insert_one(self._mongo_document(version))

    @staticmethod
    def _to_effective(
        override: UserPRTemplate | None,
        system: PRTemplate,
    ) -> EffectivePRTemplate:
        category, slot = TEMPLATE_IDENTITY[TemplateKey(system.template_key)]
        if override is not None and override.enabled:
            return EffectivePRTemplate(
                template_id=override.template_id,
                template_key=override.template_key,
                category_v2=override.category_v2,
                slot=override.slot,
                source=TemplateSource.USER,
                version=override.version,
                system_version=system.system_version,
                updated_at=override.updated_at,
                **TemplateRepository._content_values(override),
            )
        return EffectivePRTemplate(
            template_id=f"system:{system.template_key}",
            template_key=system.template_key,
            category_v2=category,
            slot=slot,
            source=TemplateSource.SYSTEM,
            version=system.system_version,
            system_version=system.system_version,
            name=system.name,
            title_template=system.title_template,
            sections=[
                TemplateSection(order=index, **section)
                for index, section in enumerate(system.sections, start=1)
            ],
            perspectives=list(system.perspectives),
            extra_instructions=system.extra_instructions,
        )

    @staticmethod
    def _content_values(template: Any) -> dict[str, Any]:
        return template.model_dump(
            include={
                "name",
                "title_template",
                "sections",
                "perspectives",
                "extra_instructions",
            },
            mode="python",
        )

    @staticmethod
    def _mongo_document(model: Any) -> dict[str, Any]:
        return model.model_dump(by_alias=True, exclude_none=True, mode="python")

    @staticmethod
    def _normalize_document(document: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(document)
        if "_id" in normalized:
            normalized["_id"] = str(normalized["_id"])
        return normalized

    @classmethod
    def _document_to_user_template(cls, document: dict[str, Any]) -> UserPRTemplate:
        return UserPRTemplate.model_validate(cls._normalize_document(document))

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        normalized = str(user_id or "").strip()
        if not normalized:
            raise ValueError("user_id is required")
        return normalized

    @staticmethod
    def _require_system_template(template_key: str | TemplateKey) -> PRTemplate:
        system = get_system_template(str(template_key))
        if system is None:
            raise TemplateNotFoundError(f"unknown template key: {template_key}")
        return system
