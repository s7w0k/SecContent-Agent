"""
PR Agent Demo - Backend service entry point.

FastAPI app:
  - MongoDB + MCP lifecycle management
  - REST API (health, articles, reports, pipeline)
  - Static file serving (React build)
  - Request logging middleware
  - In-memory log buffer accessible via /api/logs
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from config import get_settings
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Logging ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backend")

# In-memory log buffer (last 200 entries, viewable via /api/logs)
_log_buffer: deque = deque(maxlen=200)


def _log(level: str, msg: str):
    """Write to both logger and in-memory buffer."""
    tz = timezone(timedelta(hours=8))
    ts = datetime.now(tz).strftime("%H:%M:%S")
    entry = f"[{ts}] [{level}] {msg}"
    _log_buffer.append(entry)
    if level == "ERROR":
        logger.error(msg)
    elif level == "WARNING":
        logger.warning(msg)
    else:
        logger.info(msg)


# ── Lifespan ────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect MongoDB + init Agent components. Shutdown: cleanup."""
    settings = get_settings()
    _log("INFO", "=" * 50)
    _log("INFO", "Backend starting...")
    _log("INFO", f"Python {sys.version}")
    _log("INFO", f"MongoDB URI: {settings.MONGODB_URI.split('@')[-1] if '@' in settings.MONGODB_URI else settings.MONGODB_URI}")
    _log("INFO", f"MCP WeWe URL: {settings.MCP_WEWE_URL}")
    _log("INFO", f"MCP Crawl URL: {settings.MCP_CRAWL_URL}")
    _log("INFO", f"DeepSeek model: {settings.DEEPSEEK_MODEL}")
    _log("INFO", f"DeepSeek key configured: {bool(settings.DEEPSEEK_API_KEY)}")
    _log("INFO", f"Knowledge dir: {settings.KNOWLEDGE_BASE_DIR}")

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
        _log("INFO", f"MongoDB connected: {settings.MONGODB_DB}")
    except Exception as e:
        _log("ERROR", f"MongoDB connection failed: {e}")
        app.state.db = None

    # Agent components
    try:
        from agent.tools import create_mcp_toolset
        from agent.knowledge import KnowledgeLoader
        from agent.scorer import ScoringAgent
        from agent.reporter import ReportAgent
        from agent.pipeline import PipelineManager
        from langchain_openai import ChatOpenAI

        tools = create_mcp_toolset(
            wewe_url=settings.MCP_WEWE_URL,
            crawl_url=settings.MCP_CRAWL_URL,
        )
        _log("INFO", f"MCP tools initialized: {len(tools)} tools")

        knowledge_loader = KnowledgeLoader(docs_dir=settings.KNOWLEDGE_BASE_DIR)
        await knowledge_loader.load()
        app.state.knowledge_loader = knowledge_loader
        _log("INFO", f"Knowledge loaded: source={knowledge_loader.filepath}")

        llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0.1,
        )
        _log("INFO", f"LLM initialized: model={settings.DEEPSEEK_MODEL}")

        scorer = ScoringAgent(llm=llm, knowledge=knowledge_loader._cache)
        reporter = ReportAgent(llm=llm, knowledge=knowledge_loader._cache, db=app.state.db)

        # V2 6分类 Agent
        from agent.classifier_v2 import ClassifierV2
        classifier_v2 = ClassifierV2(llm=llm)
        app.state.classifier_v2 = classifier_v2
        _log("INFO", "ClassifierV2 initialized")

        pipeline_manager = PipelineManager(
            tools=tools, scorer=scorer, reporter=reporter,
            knowledge=knowledge_loader, db=app.state.db,
        )
        app.state.pipeline_manager = pipeline_manager
        _log("INFO", "Agent pipeline initialized")
    except Exception as e:
        _log("WARNING", f"Agent init skipped: {e}")
        app.state.pipeline_manager = None
        app.state.knowledge_loader = None

    yield

    # Shutdown
    _log("INFO", "Shutting down backend...")
    try:
        from db.mongo import MongoDB
        await MongoDB.disconnect()
    except Exception:
        pass
    _log("INFO", "Backend stopped")


# ── FastAPI App ─────────────────────────────────────────

app = FastAPI(
    title="PR Agent Demo - Backend",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request logging middleware ──────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every HTTP request with method, path, status, and duration."""
    start = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start) * 1000
    _log(
        "WARNING" if response.status_code >= 400 else "INFO",
        f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms:.0f}ms)",
    )
    return response


# ── Routers ─────────────────────────────────────────────

from api.pipeline import router as pipeline_router
from api.dashboard import router as dashboard_router
from api.reports import router as reports_router
from api.accounts import router as accounts_router
from api.logs import router as logs_router
from api.crawl_config import router as crawl_config_router
from api.overseas_crawl import router as overseas_router

app.include_router(pipeline_router)
app.include_router(dashboard_router)
app.include_router(reports_router)
app.include_router(accounts_router)
app.include_router(logs_router)
app.include_router(crawl_config_router)
app.include_router(overseas_router)


# ── System endpoints ────────────────────────────────────

@app.get("/api/health", tags=["System"])
async def health():
    mongo_health = {"status": "not_configured"}
    try:
        from db.mongo import MongoDB
        if MongoDB.is_connected():
            mongo_health = await MongoDB.health_check()
    except Exception as e:
        mongo_health = {"status": "error", "error": str(e)}
    return {
        "ok": True, "status": "healthy", "version": "0.1.0",
        "mongodb": mongo_health,
        "mcp_wewe": settings.MCP_WEWE_URL,
        "mcp_crawl": settings.MCP_CRAWL_URL,
    }


@app.get("/api/config/summary", tags=["System"])
async def config_summary():
    s = get_settings()
    return {
        "mongodb_db": s.MONGODB_DB,
        "mcp_wewe_url": s.MCP_WEWE_URL,
        "mcp_crawl_url": s.MCP_CRAWL_URL,
        "deepseek_model": s.DEEPSEEK_MODEL,
        "deepseek_configured": bool(s.DEEPSEEK_API_KEY),
        "score_threshold": s.PIPELINE_SCORE_THRESHOLD,
        "crawl_days_default": s.PIPELINE_CRAWL_DEFAULT_DAYS,
    }


# ── Static files (must be last to avoid overriding API routes) ──
try:
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
    _log("INFO", "Static files mounted from ./static")
except RuntimeError:
    _log("INFO", "Static directory not found - running in API-only mode")
