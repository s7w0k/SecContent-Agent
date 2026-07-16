"""Tenant-aware PR template repository tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agent.template_repository import (
    TemplateNotFoundError,
    TemplateRepository,
    TemplateVersionConflictError,
)
from db.mongo import MongoDB
from models.pr_template import TemplateSource, UserPRTemplateUpdate
from pymongo.errors import DuplicateKeyError


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, key: str, direction: int) -> FakeCursor:
        self.documents.sort(key=lambda row: row[key], reverse=direction < 0)
        return self

    def skip(self, count: int) -> FakeCursor:
        self.documents = self.documents[count:]
        return self

    def limit(self, count: int) -> FakeCursor:
        self.documents = self.documents[:count]
        return self

    async def to_list(self, length: int | None) -> list[dict[str, Any]]:
        documents = self.documents if length is None else self.documents[:length]
        return deepcopy(documents)


class FakeCollection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any]) -> FakeCursor:
        self.queries.append(deepcopy(query))
        return FakeCursor([deepcopy(row) for row in self.documents if _matches(row, query)])

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self.queries.append(deepcopy(query))
        return next(
            (deepcopy(row) for row in self.documents if _matches(row, query)),
            None,
        )

    async def insert_one(self, document: dict[str, Any]) -> None:
        self.documents.append(deepcopy(document))

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any] | None:
        self.queries.append(deepcopy(query))
        for document in self.documents:
            if not _matches(document, query):
                continue
            document.update(deepcopy(update.get("$set", {})))
            for key, increment in update.get("$inc", {}).items():
                document[key] += increment
            return deepcopy(document)
        return None

    async def find_one_and_delete(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self.queries.append(deepcopy(query))
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                return self.documents.pop(index)
        return None

    async def count_documents(self, query: dict[str, Any]) -> int:
        self.queries.append(deepcopy(query))
        return sum(1 for document in self.documents if _matches(document, query))

    async def delete_many(self, query: dict[str, Any]) -> None:
        self.queries.append(deepcopy(query))
        self.documents = [document for document in self.documents if not _matches(document, query)]


class FakeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection())


def _update(
    *, expected_version: int | None = None, name: str = "我的爆点 A"
) -> UserPRTemplateUpdate:
    return UserPRTemplateUpdate(
        name=name,
        title_template="# [事件名称]：租户分析",
        sections=[
            {"heading": "事件概述", "guide": "描述事件背景", "order": 1},
            {"heading": "安全影响", "guide": "分析智能体安全影响", "order": 2},
        ],
        perspectives=["技术视角", "市场视角"],
        extra_instructions="突出身份安全风险",
        expected_version=expected_version,
    )


@pytest.mark.asyncio
async def test_returns_six_system_templates_without_overrides() -> None:
    repository = TemplateRepository(FakeDatabase())

    templates = await repository.list_effective_templates("user-a")

    assert len(templates) == 6
    assert {template.source for template in templates} == {TemplateSource.SYSTEM}
    assert {template.template_key for template in templates} == {
        "breaking_a",
        "breaking_b",
        "law_a",
        "law_b",
        "ai_a",
        "ai_b",
    }


@pytest.mark.asyncio
async def test_resolve_merges_one_user_slot_with_system_fallback() -> None:
    repository = TemplateRepository(FakeDatabase())
    await repository.save("user-a", "breaking_a", _update())

    templates = await repository.resolve("user-a", "爆点事件")

    assert [(template.slot, template.source) for template in templates] == [
        ("A", TemplateSource.USER),
        ("B", TemplateSource.SYSTEM),
    ]
    assert templates[0].name == "我的爆点 A"
    assert templates[1].template_key == "breaking_b"


@pytest.mark.asyncio
async def test_template_overrides_are_isolated_by_user_id() -> None:
    database = FakeDatabase()
    repository = TemplateRepository(database)
    await repository.save("user-b", "breaking_a", _update(name="用户 B 模板"))

    user_a_templates = await repository.resolve("user-a", "爆点事件")
    user_b_templates = await repository.resolve("user-b", "爆点事件")

    assert user_a_templates[0].source == TemplateSource.SYSTEM
    assert user_b_templates[0].source == TemplateSource.USER
    assert user_b_templates[0].name == "用户 B 模板"
    assert all("user_id" in query for query in database["user_pr_templates"].queries)


@pytest.mark.asyncio
async def test_two_users_modify_same_slot_with_independent_versions_and_history() -> None:
    repository = TemplateRepository(FakeDatabase())

    user_a = await repository.save("user-a", "breaking_a", _update(name="用户 A 模板"))
    user_b = await repository.save("user-b", "breaking_a", _update(name="用户 B 模板"))
    user_a_v2 = await repository.save(
        "user-a",
        "breaking_a",
        _update(expected_version=user_a.version, name="用户 A 第二版"),
    )

    assert user_b.version == 1
    assert user_a_v2.version == 2
    assert [
        item.snapshot.name for item in await repository.list_versions("user-a", "breaking_a")
    ] == [
        "用户 A 第二版",
        "用户 A 模板",
    ]
    assert [
        item.snapshot.name for item in await repository.list_versions("user-b", "breaking_a")
    ] == ["用户 B 模板"]


@pytest.mark.asyncio
async def test_user_cannot_read_or_restore_another_users_history() -> None:
    repository = TemplateRepository(FakeDatabase())
    await repository.save("user-a", "breaking_a", _update(name="用户 A 私有模板"))

    with pytest.raises(TemplateNotFoundError, match="unknown template version"):
        await repository.get_version("user-b", "breaking_a", 1)
    with pytest.raises(TemplateNotFoundError, match="unknown template version"):
        await repository.restore("user-b", "breaking_a", 1)

    effective = await repository.get_effective("user-b", "breaking_a")
    assert effective.source == TemplateSource.SYSTEM
    assert await repository.count_versions("user-b", "breaking_a") == 0


@pytest.mark.asyncio
async def test_save_updates_version_and_reads_tenant_history() -> None:
    repository = TemplateRepository(FakeDatabase())
    created = await repository.save("user-a", "breaking_a", _update())
    updated = await repository.save(
        "user-a",
        "breaking_a",
        _update(expected_version=1, name="第二版模板"),
    )

    versions = await repository.list_versions("user-a", "breaking_a")

    assert created.version == 1
    assert updated.version == 2
    assert updated.name == "第二版模板"
    assert [version.version for version in versions] == [2, 1]
    assert [version.snapshot.name for version in versions] == ["第二版模板", "我的爆点 A"]
    assert await repository.list_versions("user-b", "breaking_a") == []


@pytest.mark.asyncio
async def test_stale_save_is_rejected_and_reset_restores_system_default() -> None:
    database = FakeDatabase()
    repository = TemplateRepository(database)
    await repository.save("user-a", "breaking_a", _update())

    with pytest.raises(TemplateVersionConflictError, match="current version is 1"):
        await repository.save(
            "user-a",
            "breaking_a",
            _update(expected_version=2),
        )

    restored = await repository.reset("user-a", "breaking_a")

    assert restored.source == TemplateSource.SYSTEM
    assert await repository.get_override("user-a", "breaking_a") is None


@pytest.mark.asyncio
async def test_concurrent_create_duplicate_key_maps_to_version_conflict() -> None:
    database = FakeDatabase()
    repository = TemplateRepository(database)
    database["user_pr_templates"].insert_one = AsyncMock(
        side_effect=DuplicateKeyError("duplicate tenant template")
    )

    with pytest.raises(TemplateVersionConflictError, match="created concurrently"):
        await repository.save("user-a", "breaking_a", _update())


@pytest.mark.asyncio
async def test_compare_and_swap_failure_maps_to_version_conflict() -> None:
    database = FakeDatabase()
    repository = TemplateRepository(database)
    await repository.save("user-a", "breaking_a", _update())
    database["user_pr_templates"].find_one_and_update = AsyncMock(return_value=None)

    with pytest.raises(TemplateVersionConflictError, match="reload before saving"):
        await repository.save(
            "user-a",
            "breaking_a",
            _update(expected_version=1, name="并发修改"),
        )


@pytest.mark.asyncio
async def test_reset_rejects_a_concurrent_template_change() -> None:
    database = FakeDatabase()
    repository = TemplateRepository(database)
    await repository.save("user-a", "breaking_a", _update())
    database["user_pr_templates"].find_one_and_delete = AsyncMock(return_value=None)

    with pytest.raises(TemplateVersionConflictError, match="changed during reset"):
        await repository.reset("user-a", "breaking_a")


@pytest.mark.asyncio
async def test_restore_creates_a_new_monotonic_version_after_reset() -> None:
    repository = TemplateRepository(FakeDatabase())
    await repository.save("user-a", "breaking_a", _update(name="第一版"))
    await repository.save("user-a", "breaking_a", _update(expected_version=1, name="第二版"))
    await repository.reset("user-a", "breaking_a")

    restored = await repository.restore("user-a", "breaking_a", 1)
    versions = await repository.list_versions("user-a", "breaking_a")

    assert restored.version == 4
    assert restored.name == "第一版"
    assert [item.version for item in versions] == [4, 3, 2, 1]
    assert versions[0].change_type == "restore"
    assert versions[1].change_type == "reset"


@pytest.mark.asyncio
async def test_history_is_limited_to_twenty_versions() -> None:
    repository = TemplateRepository(FakeDatabase())
    current = await repository.save("user-a", "breaking_a", _update(name="版本 1"))
    for version in range(2, 23):
        current = await repository.save(
            "user-a",
            "breaking_a",
            _update(expected_version=current.version, name=f"版本 {version}"),
        )

    versions = await repository.list_versions("user-a", "breaking_a", limit=100)

    assert len(versions) == 20
    assert versions[0].version == 22
    assert versions[-1].version == 3


@pytest.mark.asyncio
async def test_template_indexes_match_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[Any]] = {}

    class IndexCollection:
        async def create_indexes(self, indexes: list[Any]) -> list[str]:
            captured["current"] = indexes
            return [index.document["name"] for index in indexes]

        async def drop_index(self, _: str) -> None:
            return None

    collections: dict[str, IndexCollection] = {}

    def get_collection(cls: type[MongoDB], name: str) -> IndexCollection:
        collection = collections.setdefault(name, IndexCollection())
        original = collection.create_indexes

        async def capture(indexes: list[Any]) -> list[str]:
            captured[name] = indexes
            return await original(indexes)

        collection.create_indexes = capture  # type: ignore[method-assign]
        return collection

    monkeypatch.setattr(MongoDB, "get_collection", classmethod(get_collection))
    created = await MongoDB.ensure_indexes()

    assert created["user_pr_templates"] == [
        "uq_user_template_key",
        "uq_user_category_slot",
        "idx_user_template_updated",
    ]
    assert created["user_pr_template_versions"] == ["uq_user_template_version"]
    template_indexes = {
        index.document["name"]: index.document for index in captured["user_pr_templates"]
    }
    assert template_indexes["uq_user_template_key"]["unique"] is True
    assert template_indexes["uq_user_category_slot"]["unique"] is True
    version_index = captured["user_pr_template_versions"][0].document
    assert version_index["unique"] is True
