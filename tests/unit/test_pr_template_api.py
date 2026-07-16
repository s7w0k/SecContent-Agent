"""Authenticated PR template management API tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.template_repository import TemplateRepository, TemplateVersionConflictError
from api.pr_templates import router
from auth.deps import AuthError, auth_error_handler
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from models.pr_template import (
    EffectivePRTemplate,
    TemplateChangeType,
    TemplateKey,
    TemplateSnapshot,
    TemplateSource,
    UserPRTemplate,
    UserPRTemplateVersion,
)


class LogCollection:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def insert_one(self, document: dict) -> SimpleNamespace:
        self.documents.append(document)
        return SimpleNamespace(inserted_id="log-id")


class LogDatabase:
    def __init__(self) -> None:
        self.collections = {"pipeline_logs": LogCollection()}

    def __getitem__(self, name: str) -> LogCollection:
        return self.collections[name]


def _content(name: str = "用户爆点模板") -> dict:
    return {
        "name": name,
        "title_template": "# [事件名称]：影响分析",
        "sections": [
            {"heading": "事件概述", "guide": "描述事件背景", "order": 1},
            {"heading": "安全影响", "guide": "分析身份安全影响", "order": 2},
        ],
        "perspectives": ["技术视角", "市场视角"],
        "extra_instructions": "突出智能体身份风险",
    }


def _effective(
    *,
    source: TemplateSource = TemplateSource.USER,
    version: int = 2,
    name: str = "用户爆点模板",
) -> EffectivePRTemplate:
    return EffectivePRTemplate(
        template_id="tpl-user-breaking-a" if source == TemplateSource.USER else "system:breaking_a",
        template_key=TemplateKey.BREAKING_A,
        category_v2="爆点事件",
        slot="A",
        source=source,
        version=version,
        system_version=1,
        **_content(name),
    )


def _override(version: int = 1) -> UserPRTemplate:
    return UserPRTemplate(
        template_id="tpl-user-breaking-a",
        user_id="user-a",
        template_key="breaking_a",
        category_v2="爆点事件",
        slot="A",
        version=version,
        **_content(),
    )


def _history(version: int = 1) -> UserPRTemplateVersion:
    snapshot = TemplateSnapshot(
        template_key="breaking_a",
        category_v2="爆点事件",
        slot="A",
        **_content("历史模板"),
    )
    return UserPRTemplateVersion(
        template_id="tpl-user-breaking-a",
        user_id="user-a",
        template_key="breaking_a",
        version=version,
        snapshot=snapshot,
        change_type=TemplateChangeType.CREATE,
    )


@pytest.fixture
def api_runtime(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.add_exception_handler(AuthError, auth_error_handler)
    app.include_router(router)
    repository = MagicMock(spec=TemplateRepository)
    database = LogDatabase()
    app.state.db = database
    app.state.template_repository = repository

    audit_logger = MagicMock()
    monkeypatch.setattr("api.pr_templates.get_audit_logger", lambda: audit_logger)

    @app.middleware("http")
    async def identify_user(request: Request, call_next):
        request.state.user_id = request.headers.get("X-Test-User")
        request.state.username = request.headers.get("X-Test-User")
        return await call_next(request)

    return app, repository, database, audit_logger


@pytest.fixture
async def client(api_runtime):
    app, _, _, _ = api_runtime
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_all_endpoints_require_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/pr-templates")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


@pytest.mark.asyncio
async def test_list_uses_current_user_and_optional_category(client, api_runtime) -> None:
    _, repository, _, _ = api_runtime
    repository.list_effective_templates = AsyncMock(return_value=[_effective()])

    response = await client.get(
        "/api/pr-templates",
        params={"category_v2": "爆点事件"},
        headers={"X-Test-User": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["total"] == 1
    repository.list_effective_templates.assert_awaited_once_with("user-a", "爆点事件")


@pytest.mark.asyncio
async def test_single_template_reads_are_tenant_scoped(client, api_runtime) -> None:
    _, repository, _, _ = api_runtime
    repository.get_effective = AsyncMock(return_value=_effective())

    await client.get("/api/pr-templates/breaking_a", headers={"X-Test-User": "user-a"})
    await client.get("/api/pr-templates/breaking_a", headers={"X-Test-User": "user-b"})

    assert repository.get_effective.await_args_list[0].args == ("user-a", "breaking_a")
    assert repository.get_effective.await_args_list[1].args == ("user-b", "breaking_a")


@pytest.mark.asyncio
async def test_save_returns_template_and_writes_safe_logs(client, api_runtime) -> None:
    _, repository, database, audit_logger = api_runtime
    repository.get_override = AsyncMock(return_value=_override())
    repository.save_override = AsyncMock(return_value=_effective(version=2))
    payload = {**_content(), "expected_version": 1}

    response = await client.put(
        "/api/pr-templates/breaking_a",
        json=payload,
        headers={"X-Test-User": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["version"] == 2
    repository.save_override.assert_awaited_once()
    assert repository.save_override.await_args.args[:2] == ("user-a", "breaking_a")
    log = database["pipeline_logs"].documents[0]
    assert log["phase"] == "pr_template"
    assert log["action"] == "update"
    assert log["detail"] == {
        "template_key": "breaking_a",
        "template_id": "tpl-user-breaking-a",
        "old_version": 1,
        "new_version": 2,
        "source": "user",
    }
    assert "sections" not in log["detail"]
    audit_logger.log.assert_called_once()


@pytest.mark.asyncio
async def test_save_rejects_client_identity_and_maps_version_conflict(client, api_runtime) -> None:
    _, repository, _, _ = api_runtime
    invalid = await client.put(
        "/api/pr-templates/breaking_a",
        json={**_content(), "user_id": "user-b"},
        headers={"X-Test-User": "user-a"},
    )
    assert invalid.status_code == 422

    repository.get_override = AsyncMock(return_value=_override())
    repository.save_override = AsyncMock(
        side_effect=TemplateVersionConflictError("expected 1, current 2")
    )
    conflict = await client.put(
        "/api/pr-templates/breaking_a",
        json={**_content(), "expected_version": 1},
        headers={"X-Test-User": "user-a"},
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "TEMPLATE_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_reset_returns_system_template(client, api_runtime) -> None:
    _, repository, _, _ = api_runtime
    repository.get_override = AsyncMock(return_value=_override(version=3))
    repository.reset_override = AsyncMock(
        return_value=_effective(source=TemplateSource.SYSTEM, version=1, name="爆点A")
    )

    response = await client.post(
        "/api/pr-templates/breaking_a/reset",
        headers={"X-Test-User": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["source"] == "system"
    repository.reset_override.assert_awaited_once_with("user-a", "breaking_a")


@pytest.mark.asyncio
async def test_preview_returns_markdown_without_saving(client, api_runtime) -> None:
    _, repository, _, _ = api_runtime
    repository.get_effective = AsyncMock(return_value=_effective())

    response = await client.post(
        "/api/pr-templates/breaking_a/preview",
        json=_content(),
        headers={"X-Test-User": "user-a"},
    )

    assert response.status_code == 200
    markdown = response.json()["data"]["content_md"]
    assert markdown.startswith("# [事件名称]：影响分析")
    assert "## 事件概述" in markdown
    repository.save_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_versions_are_paginated_with_current_user(client, api_runtime) -> None:
    _, repository, _, _ = api_runtime
    repository.list_versions = AsyncMock(return_value=[_history(2)])
    repository.count_versions = AsyncMock(return_value=3)

    response = await client.get(
        "/api/pr-templates/breaking_a/versions?page=2&page_size=1",
        headers={"X-Test-User": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["total"] == 3
    repository.list_versions.assert_awaited_once_with("user-a", "breaking_a", offset=1, limit=1)
    repository.count_versions.assert_awaited_once_with("user-a", "breaking_a")


@pytest.mark.asyncio
async def test_restore_returns_a_new_version_and_audits(client, api_runtime) -> None:
    _, repository, database, audit_logger = api_runtime
    repository.get_override = AsyncMock(return_value=_override(version=3))
    repository.restore = AsyncMock(return_value=_effective(version=4, name="历史模板"))

    response = await client.post(
        "/api/pr-templates/breaking_a/versions/1/restore",
        headers={"X-Test-User": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["version"] == 4
    repository.restore.assert_awaited_once_with("user-a", "breaking_a", 1)
    assert database["pipeline_logs"].documents[0]["action"] == "restore"
    assert audit_logger.log.call_args.kwargs["action"] == "pr_template_restore"
