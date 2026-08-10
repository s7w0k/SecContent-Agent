"""Worker wiring tests for tenant-scoped PR templates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeCollections(dict):
    """为运行时装配提供任意 Mongo 集合（懒创建 MagicMock）。"""

    def __getitem__(self, name: str) -> MagicMock:
        if name not in dict.keys(self):
            dict.__setitem__(self, name, MagicMock(name=f"collection:{name}"))
        return dict.__getitem__(self, name)


@pytest.mark.asyncio
async def test_worker_startup_shares_template_repository_with_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import worker
    from db.mongo import MongoDB

    database: dict = _FakeCollections()
    repository = MagicMock(name="template_repository")
    pipeline_v2 = MagicMock(name="pipeline_v2")
    pipeline_v2_factory = MagicMock(return_value=pipeline_v2)
    crawl_client = MagicMock(name="crawl_client")
    knowledge = MagicMock(name="knowledge", _cache={})
    knowledge.load = AsyncMock()

    monkeypatch.setattr(MongoDB, "connect", AsyncMock())
    monkeypatch.setattr(MongoDB, "get_db", MagicMock(return_value=database))
    monkeypatch.setattr(MongoDB, "ensure_indexes", AsyncMock())
    monkeypatch.setattr(
        "agent.template_repository.TemplateRepository",
        MagicMock(return_value=repository),
    )
    monkeypatch.setattr(
        "clients.mcp_crawl.McpCrawlClient.from_settings", lambda _settings: crawl_client
    )
    monkeypatch.setattr("agent.tools.create_mcp_toolset", MagicMock(return_value={}))
    monkeypatch.setattr("agent.knowledge.KnowledgeLoader", MagicMock(return_value=knowledge))
    monkeypatch.setattr("worker.ChatOpenAI", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.classifier_v2.ClassifierV2", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.scorer_v2.ScoringAgentV2", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.draft_generator.DraftGenerator", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.pipeline_v2.PipelineManagerV2", pipeline_v2_factory)
    monkeypatch.setattr("agent.pipeline.PipelineManager", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.scorer.ScoringAgent", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("agent.reporter.ReportAgent", MagicMock(return_value=MagicMock()))

    context: dict = {}
    await worker.startup(context)

    assert pipeline_v2_factory.call_args.kwargs["template_repository"] is repository
    assert context["template_repository"] is repository
    assert context["pipeline_v2"] is pipeline_v2
    assert context["app"].state.template_repository is repository
