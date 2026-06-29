"""
PR Agent Demo — Backend 服务入口

FastAPI 应用:
  - 管理 MongoDB + MCP 服务生命周期
  - 提供 REST API（健康检查 + 文章 + 报道 + 流水线）
  - 生产模式下托管 React 构建产物

启动:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from config import get_settings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ═══════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backend")

# ═══════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时连接 MongoDB + 初始化 Agent 组件，关闭时清理。"""
    settings = get_settings()

    # ── 启动 ─────────────────────────────────────────
    logger.info("Starting backend service...")

    # MongoDB
    try:
        from db.mongo import MongoDB

        await MongoDB.connect(
            uri=settings.MONGODB_URI,
            db_name=settings.MONGODB_DB,
            max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
            min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
        )
        app.state.db = MongoDB.get_db()
        logger.info("MongoDB connected: %s", settings.MONGODB_DB)
    except Exception as e:
        logger.error("MongoDB connection failed: %s", e)
        logger.warning("Backend will start without MongoDB — database features unavailable")
        app.state.db = None

    # ── Agent 组件初始化（阶段二）─────────────────────
    try:
        from agent.tools import create_mcp_toolset
        from agent.knowledge import KnowledgeLoader
        from agent.scorer import ScoringAgent
        from agent.reporter import ReportAgent
        from agent.pipeline import PipelineManager
        from langchain_openai import ChatOpenAI

        # 1. MCP 工具集
        tools = create_mcp_toolset(
            wewe_url=settings.MCP_WEWE_URL,
            crawl_url=settings.MCP_CRAWL_URL,
        )
        logger.info("MCP tools initialized: %d tools", len(tools))

        # 2. 知识库
        knowledge_loader = KnowledgeLoader(docs_dir=settings.KNOWLEDGE_BASE_DIR)
        await knowledge_loader.load()
        app.state.knowledge_loader = knowledge_loader
        logger.info("Knowledge loaded: %d features", len(knowledge_loader._cache.core_features) if knowledge_loader._cache else 0)

        # 3. LLM
        llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=f"{settings.DEEPSEEK_BASE_URL}/v1",
            temperature=0.1,
        )

        # 4. Agent
        scorer = ScoringAgent(llm=llm, knowledge=knowledge_loader._cache)
        reporter = ReportAgent(llm=llm, knowledge=knowledge_loader._cache, db=app.state.db)

        # 5. 流水线管理器
        pipeline_manager = PipelineManager(
            tools=tools,
            scorer=scorer,
            reporter=reporter,
            knowledge=knowledge_loader,
            db=app.state.db,
        )
        app.state.pipeline_manager = pipeline_manager
        logger.info("Agent pipeline initialized")

    except Exception as e:
        logger.warning("Agent components initialization skipped: %s", e)
        app.state.pipeline_manager = None
        app.state.knowledge_loader = None

    yield  # ← 应用运行中

    # ── 关闭 ─────────────────────────────────────────
    logger.info("Shutting down backend...")
    try:
        from db.mongo import MongoDB

        await MongoDB.disconnect()
    except Exception:
        pass
    logger.info("Backend stopped")


# ═══════════════════════════════════════════════════════════
# FastAPI 应用
# ═══════════════════════════════════════════════════════════

app = FastAPI(
    title="PR Agent Demo — Backend",
    version="0.1.0",
    description="智能体安全 PR 情报 Agent 系统 — 核心后端服务",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════
# 路由注册（阶段二：Agent 流水线 + 仪表盘 API）
# ═══════════════════════════════════════════════════════════

from api.pipeline import router as pipeline_router
from api.dashboard import router as dashboard_router
from api.reports import router as reports_router

app.include_router(pipeline_router)
app.include_router(dashboard_router)
app.include_router(reports_router)


# ═══════════════════════════════════════════════════════════════
# 系统端点
# ═══════════════════════════════════════════════════════════════


@app.get("/api/health", tags=["System"])
async def health():
    """健康检查 — 返回服务及各依赖的状态。"""
    mongo_health = {"status": "not_configured"}

    try:
        from db.mongo import MongoDB

        if MongoDB.is_connected():
            mongo_health = await MongoDB.health_check()
    except Exception as e:
        mongo_health = {"status": "error", "error": str(e)}

    return {
        "ok": True,
        "status": "healthy",
        "version": "0.1.0",
        "mongodb": mongo_health,
        "mcp_wewe": settings.MCP_WEWE_URL,
        "mcp_crawl": settings.MCP_CRAWL_URL,
    }


@app.get("/api/config/summary", tags=["System"])
async def config_summary():
    """返回当前配置摘要（不含敏感信息）。"""
    s = get_settings()
    return {
        "mongodb_db": s.MONGODB_DB,
        "mongodb_pool": f"{s.MONGODB_MIN_POOL_SIZE}-{s.MONGODB_MAX_POOL_SIZE}",
        "mcp_wewe_url": s.MCP_WEWE_URL,
        "mcp_crawl_url": s.MCP_CRAWL_URL,
        "deepseek_model": s.DEEPSEEK_MODEL,
        "deepseek_configured": bool(s.DEEPSEEK_API_KEY),
        "score_threshold": s.PIPELINE_SCORE_THRESHOLD,
        "crawl_days_default": s.PIPELINE_CRAWL_DEFAULT_DAYS,
        "page_size_max": s.API_PAGE_SIZE_MAX,
    }


# ── 静态文件（生产模式，放在最后避免覆盖 API 路由）──
try:
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
    logger.info("Static files mounted from ./static")
except RuntimeError:
    logger.info("Static directory not found — running in API-only mode")
