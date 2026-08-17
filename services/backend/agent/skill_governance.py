"""Immutable Skill release workflow used by the production runtime."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SkillReleaseError(ValueError):
    """A release transition or immutable version constraint was rejected."""


class ReleaseStage(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    EVALUATED = "evaluated"
    SHADOW = "shadow"
    APPROVED = "approved"
    PUBLISHED = "published"
    ROLLED_BACK = "rolled_back"


class SkillRelease(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., pattern=r"^[a-z0-9-]+$")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    manifest_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    stage: ReleaseStage = ReleaseStage.DRAFT
    dataset_refs: tuple[str, ...] = ()
    eval_report_ref: str = ""
    eval_passed: bool = False
    shadow_report_ref: str = ""
    approved_by: str = ""
    published_by: str = ""
    rollback_target: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def release_id(self) -> str:
        return f"{self.name}@{self.version}"


def skill_manifest_hash(payload: dict[str, Any]) -> str:
    safe = {key: value for key, value in payload.items() if key not in {"created_at", "updated_at"}}
    raw = json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SkillPublicationService:
    """draft -> validate -> eval -> shadow -> approve -> publish -> rollback.

    The service keeps an in-process store for tests and small deployments. A Mongo
    collection can be supplied; writes use the immutable release_id plus hash.
    """

    COLLECTION = "skill_releases"

    def __init__(self, db: Any = None):
        self.db = db
        self._releases: dict[str, SkillRelease] = {}
        self._published: dict[str, str] = {}

    async def create_draft(
        self, *, name: str, version: str, manifest: dict[str, Any]
    ) -> SkillRelease:
        release = SkillRelease(
            name=name,
            version=version,
            manifest_hash=skill_manifest_hash(manifest),
            dataset_refs=tuple(str(item) for item in manifest.get("eval_datasets", ())),
        )
        existing = await self.get(release.release_id)
        if existing:
            if existing.manifest_hash != release.manifest_hash:
                raise SkillReleaseError(f"immutable Skill version conflict: {release.release_id}")
            return existing
        return await self._save(release)

    async def validate(self, release_id: str) -> SkillRelease:
        release = await self._require_stage(release_id, ReleaseStage.DRAFT)
        if not release.dataset_refs:
            raise SkillReleaseError("validation requires eval dataset references")
        return await self._transition(release, ReleaseStage.VALIDATED)

    async def record_offline_eval(
        self, release_id: str, *, report_ref: str, passed: bool
    ) -> SkillRelease:
        release = await self._require_stage(release_id, ReleaseStage.VALIDATED)
        if not report_ref:
            raise SkillReleaseError("offline eval report is required")
        if not passed:
            raise SkillReleaseError("offline eval did not pass")
        return await self._transition(
            release, ReleaseStage.EVALUATED, eval_report_ref=report_ref, eval_passed=True
        )

    async def record_shadow(self, release_id: str, *, report_ref: str) -> SkillRelease:
        release = await self._require_stage(release_id, ReleaseStage.EVALUATED)
        if not report_ref:
            raise SkillReleaseError("shadow report is required")
        return await self._transition(
            release, ReleaseStage.SHADOW, shadow_report_ref=report_ref
        )

    async def approve(self, release_id: str, *, approver: str) -> SkillRelease:
        release = await self._require_stage(release_id, ReleaseStage.SHADOW)
        if not approver:
            raise SkillReleaseError("approver is required")
        return await self._transition(release, ReleaseStage.APPROVED, approved_by=approver)

    async def publish(self, release_id: str, *, publisher: str) -> SkillRelease:
        release = await self._require_stage(release_id, ReleaseStage.APPROVED)
        if not release.eval_passed or not release.approved_by:
            raise SkillReleaseError("evaluated and approved release required")
        published = await self._transition(
            release, ReleaseStage.PUBLISHED, published_by=publisher or release.approved_by
        )
        self._published[published.name] = published.release_id
        return published

    async def rollback(self, name: str, *, target_version: str) -> SkillRelease:
        target = await self.get(f"{name}@{target_version}")
        if target is None or target.stage != ReleaseStage.PUBLISHED:
            raise SkillReleaseError("rollback target must be a published version")
        current_id = self._published.get(name)
        if current_id and current_id != target.release_id:
            current = await self.get(current_id)
            if current:
                await self._transition(
                    current, ReleaseStage.ROLLED_BACK, rollback_target=target.release_id
                )
        self._published[name] = target.release_id
        return target

    async def freeze_published(self, names: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name in names:
            release_id = self._published.get(name)
            release = await self.get(release_id) if release_id else None
            if release is None or release.stage != ReleaseStage.PUBLISHED:
                raise SkillReleaseError(f"published Skill unavailable: {name}")
            result[name] = release.version
        return result

    async def get(self, release_id: str | None) -> SkillRelease | None:
        if not release_id:
            return None
        if release_id in self._releases:
            return self._releases[release_id]
        if self.db is not None:
            doc = await self.db[self.COLLECTION].find_one({"release_id": release_id})
            if doc:
                doc.pop("_id", None)
                doc.pop("release_id", None)
                release = SkillRelease.model_validate(doc)
                self._releases[release_id] = release
                return release
        return None

    async def _require_stage(self, release_id: str, stage: ReleaseStage) -> SkillRelease:
        release = await self.get(release_id)
        if release is None:
            raise SkillReleaseError(f"unknown Skill release: {release_id}")
        if release.stage != stage:
            raise SkillReleaseError(
                f"invalid release transition: {release.stage.value} -> expected {stage.value}"
            )
        return release

    async def _transition(
        self, release: SkillRelease, stage: ReleaseStage, **updates: Any
    ) -> SkillRelease:
        changed = release.model_copy(
            update={"stage": stage, "updated_at": datetime.now(UTC), **updates}
        )
        return await self._save(changed, replace=True)

    async def _save(self, release: SkillRelease, *, replace: bool = False) -> SkillRelease:
        self._releases[release.release_id] = release
        if self.db is not None:
            doc = release.model_dump(mode="python")
            doc["release_id"] = release.release_id
            if replace:
                await self.db[self.COLLECTION].replace_one(
                    {"release_id": release.release_id}, doc, upsert=True
                )
            else:
                await self.db[self.COLLECTION].insert_one(doc)
        return release
