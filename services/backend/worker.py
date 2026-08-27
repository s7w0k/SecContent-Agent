"""ARQ worker entry point for durable pipeline execution."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from agent.task_queue import WorkerSettings
from arq import run_worker
from config import get_settings
from langchain_openai import ChatOpenAI
from logging_config import setup_logging

settings = get_settings()
setup_logging(
    log_dir=settings.LOG_DIR,
    log_level=settings.LOG_LEVEL,
    app_retention_days=settings.LOG_APP_RETENTION_DAYS,
    error_retention_days=settings.LOG_ERROR_RETENTION_DAYS,
    access_retention_days=settings.LOG_ACCESS_RETENTION_DAYS,
    audit_retention_days=settings.LOG_AUDIT_RETENTION_DAYS,
)
logger = logging.getLogger("backend.worker")


async def startup(ctx: dict[str, Any]) -> None:
    """Initialize MongoDB and all pipeline dependencies in the worker process."""
    from agent.classifier_v2 import ClassifierV2
    from agent.draft_generator import DraftGenerator
    from agent.draft_reviewer import DraftReviewer
    from agent.knowledge import KnowledgeLoader
    from agent.multi_agent import build_multi_agent_runtime
    from agent.pipeline import PipelineManager
    from agent.pipeline_v2 import PipelineManagerV2
    from agent.reporter import ReportAgent
    from agent.scorer import ScoringAgent
    from agent.scorer_v2 import ScoringAgentV2
    from agent.template_repository import TemplateRepository
    from agent.tools import create_mcp_toolset
    from clients.mcp_crawl import McpCrawlClient
    from db.mongo import MongoDB

    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()
    await MongoDB.ensure_indexes()
    template_repository = TemplateRepository(db)
    mcp_crawl_client = McpCrawlClient.from_settings(settings)

    tools = create_mcp_toolset(
        wewe_url=settings.MCP_WEWE_URL,
        crawl_client=mcp_crawl_client,
    )
    knowledge = KnowledgeLoader(docs_dir=settings.KNOWLEDGE_BASE_DIR)
    await knowledge.load()
    llm = ChatOpenAI(
        model=settings.DEEPSEEK_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        temperature=0.1,
        timeout=settings.DEEPSEEK_TIMEOUT,
        max_tokens=settings.DEEPSEEK_MAX_TOKENS,
    )
    classifier_v2 = ClassifierV2(llm=llm, db=db)
    scorer_v2 = ScoringAgentV2(llm=llm, knowledge=knowledge, db=db)

    # Wiki Knowledge Runtime 统一装配（Hard Gate：GOAL B）
    # 一律通过统一 factory 装配，不再按 backend 分支隐式回退 legacy；
    # KNOWLEDGE_BACKEND 必须显式配置（default=wiki），缺失 Active Wiki 会 fail-fast。
    from agent.wiki.runtime_factory import (
        KnowledgeRuntimeError,
        build_knowledge_runtime,
    )

    try:
        knowledge_runtime = build_knowledge_runtime(settings, llm=llm, db=db)
    except KnowledgeRuntimeError as exc:
        logger.critical(
            "Worker bootstrap aborted: %s — strict wiki mode refuses legacy fallback",
            exc,
        )
        raise
    scorer_v2.knowledge_provider = knowledge_runtime.provider
    logger.info(
        "Knowledge Runtime assembled: mode=%s active=%s",
        knowledge_runtime.mode,
        knowledge_runtime.active_version,
    )

    draft_gen = DraftGenerator(
        llm=llm,
        knowledge=knowledge._cache,
        max_output_tokens=settings.DRAFT_MAX_OUTPUT_TOKENS,
    )
    draft_reviewer = DraftReviewer(llm=llm)
    pipeline_v2 = PipelineManagerV2(
        tools=tools,
        classifier_v2=classifier_v2,
        scorer_v2=scorer_v2,
        draft_gen=draft_gen,
        knowledge=knowledge,
        db=db,
        crawl_client=mcp_crawl_client,
        template_repository=template_repository,
        reviewer=draft_reviewer,
    )
    pipeline_manager = PipelineManager(
        tools=tools,
        scorer=ScoringAgent(llm=llm, knowledge=knowledge._cache),
        reporter=ReportAgent(llm=llm, knowledge=knowledge._cache, db=db),
        knowledge=knowledge,
        db=db,
        crawl_client=mcp_crawl_client,
    )
    from agent.llm_wrapper import LLMWrapper

    multi_agent = build_multi_agent_runtime(
        db=db,
        manager=pipeline_v2,
        llm_wrapper=LLMWrapper(llm, db),
        settings=settings,
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            db=db,
            knowledge_loader=knowledge,
            knowledge_runtime=knowledge_runtime,
            classifier_v2=classifier_v2,
            scorer_v2=scorer_v2,
            draft_gen=draft_gen,
            draft_reviewer=draft_reviewer,
            pipeline_v2=pipeline_v2,
            pipeline_manager=pipeline_manager,
            mcp_crawl_client=mcp_crawl_client,
            template_repository=template_repository,
            multi_agent=multi_agent,
        )
    )
    ctx.update(
        {
            "app": app,
            "db": db,
            "llm": llm,
            "pipeline_v2": pipeline_v2,
            "mcp_crawl_client": mcp_crawl_client,
            "template_repository": template_repository,
            "multi_agent": multi_agent,
        }
    )
    logger.info("ARQ worker runtime initialized")


async def shutdown(ctx: dict[str, Any]) -> None:
    """Close the worker's HTTP and MongoDB connection pools."""
    from db.mongo import MongoDB

    client = ctx.get("mcp_crawl_client")
    if client is not None:
        await client.aclose()
    await MongoDB.disconnect()
    logger.info("ARQ worker stopped")


WorkerSettings.on_startup = startup
WorkerSettings.on_shutdown = shutdown


if __name__ == "__main__":
    run_worker(WorkerSettings)
