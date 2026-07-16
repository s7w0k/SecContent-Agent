"""PR template integration tests for the asynchronous V2 pipeline."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.pipeline_v2 import (
    PipelineManagerV2,
    _template_log_metadata,
    _templates_for_category,
    create_state_v2,
    draft_node,
)
from agent.template_compat import normalize_legacy_drafts
from models.pr_template import EffectivePRTemplate


def _templates(version: int = 3) -> list[EffectivePRTemplate]:
    return [
        EffectivePRTemplate(
            template_id=f"tpl-user-breaking-{slot.lower()}",
            template_key=f"breaking_{slot.lower()}",
            category_v2="爆点事件",
            slot=slot,
            source="user",
            version=version,
            system_version=1,
            name=f"用户模板 {slot}",
            title_template="# [事件名称] 用户分析",
            sections=[{"heading": "安全影响", "guide": "分析身份风险", "order": 1}],
            perspectives=["技术视角", "市场视角"],
            extra_instructions="突出身份安全",
        )
        for slot in ("A", "B")
    ]


class Cursor:
    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    async def to_list(self, length: int | None = None) -> list[dict]:
        documents = self.documents if length is None else self.documents[:length]
        return deepcopy(documents)


class ArticleCollection:
    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    def find(self, _query: dict) -> Cursor:
        return Cursor(self.documents)


class DraftCollection:
    def __init__(self) -> None:
        self.updates: list[tuple] = []

    async def update_one(self, *args, **kwargs) -> None:
        self.updates.append((args, kwargs))


@pytest.mark.asyncio
async def test_manager_freezes_templates_before_graph_execution() -> None:
    repository = MagicMock()
    repository.list_effective_templates = AsyncMock(return_value=_templates(version=5))
    dependency = MagicMock()
    manager = PipelineManagerV2(
        tools={},
        classifier_v2=dependency,
        scorer_v2=dependency,
        draft_gen=dependency,
        knowledge=dependency,
        db=None,
        template_repository=repository,
    )

    async def return_state(state: dict, **_kwargs) -> dict:
        return state

    manager._graph = SimpleNamespace(ainvoke=AsyncMock(side_effect=return_state))

    result = await manager.run_full(user_id="user-a", task_id="task-template-freeze")

    repository.list_effective_templates.assert_awaited_once_with("user-a")
    frozen = result["state"]["frozen_templates"]["爆点事件"]
    assert [template["version"] for template in frozen] == [5, 5]
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_draft_node_resolves_once_and_reuses_frozen_pair_for_all_articles() -> None:
    articles = [
        {
            "url_hash": "a" * 32,
            "title": "事件一",
            "category_v2": "爆点事件",
            "pr_total_score": 160,
        },
        {
            "url_hash": "b" * 32,
            "title": "事件二",
            "category_v2": "爆点事件",
            "pr_total_score": 150,
        },
    ]
    drafts = DraftCollection()
    profile_collection = MagicMock()
    profile_collection.find_one = AsyncMock(return_value=None)
    db = {
        "articles": ArticleCollection(articles),
        "user_profiles": profile_collection,
        "user_drafts": drafts,
    }
    repository = MagicMock()
    repository.resolve = AsyncMock(return_value=_templates(version=3))
    draft_gen = MagicMock()
    draft_gen.generate = AsyncMock(
        return_value={"ok": True, "drafts": [{"title": "草稿", "content_md": "# 草稿"}]}
    )
    knowledge = MagicMock()
    knowledge.load = AsyncMock()
    state = create_state_v2(user_id="user-a")

    result = await draft_node(state, draft_gen, knowledge, db, repository)

    assert result["draft_count"] == 2
    repository.resolve.assert_awaited_once_with("user-a", "爆点事件")
    first_templates = draft_gen.generate.await_args_list[0].kwargs["templates"]
    second_templates = draft_gen.generate.await_args_list[1].kwargs["templates"]
    assert [template.version for template in first_templates] == [3, 3]
    assert [template.version for template in second_templates] == [3, 3]
    assert len(drafts.updates) == 2


@pytest.mark.asyncio
async def test_prefrozen_context_ignores_template_changes_during_task() -> None:
    frozen = _templates(version=3)
    state = create_state_v2(user_id="user-a")
    state["frozen_templates"] = {
        "爆点事件": [template.model_dump(mode="json") for template in frozen]
    }
    repository = MagicMock()
    repository.resolve = AsyncMock(return_value=_templates(version=9))

    selected = await _templates_for_category(state, repository, "爆点事件")

    assert [template.version for template in selected or []] == [3, 3]
    repository.resolve.assert_not_awaited()


def test_template_execution_log_metadata_excludes_template_content() -> None:
    metadata = _template_log_metadata(_templates())

    assert set(metadata[0]) == {"template_key", "template_id", "version", "source"}
    assert "sections" not in metadata[0]
    assert "extra_instructions" not in metadata[0]


def test_legacy_drafts_receive_read_only_fallback_metadata() -> None:
    source = [
        {
            "template": "爆点A",
            "perspective": "技术视角",
            "title": "历史草稿",
            "content_md": "# 历史草稿",
        }
    ]

    normalized = normalize_legacy_drafts(source)

    assert normalized[0]["template_id"] == "legacy:爆点A"
    assert normalized[0]["template_version"] == 0
    assert normalized[0]["template_source"] == "legacy"
    assert normalized[0]["template_snapshot"]["legacy"] is True
    assert "template_id" not in source[0]


def test_new_draft_metadata_is_preserved_by_compatibility_layer() -> None:
    source = [
        {
            "template": "用户模板 A",
            "template_id": "tpl-user-a",
            "template_key": "breaking_a",
            "template_version": 7,
            "template_source": "user",
            "template_snapshot": {"name": "用户模板 A"},
        }
    ]

    normalized = normalize_legacy_drafts(source)

    assert normalized[0]["template_id"] == "tpl-user-a"
    assert normalized[0]["template_version"] == 7
    assert normalized[0]["template_snapshot"] == {"name": "用户模板 A"}
