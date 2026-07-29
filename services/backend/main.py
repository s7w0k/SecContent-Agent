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
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from auth.deps import AuthError, auth_error_handler
from auth.jwt import decode_access_token
from config import get_settings
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from logging_config import (
    log_request,
    set_request_id,
    set_user_id,
    setup_logging,
)

# ── Logging（企业级：JSON 结构化 + 按日期轮转 + 分级文件）────
_settings = get_settings()
setup_logging(
    log_dir=_settings.LOG_DIR,
    log_level=_settings.LOG_LEVEL,
    app_retention_days=_settings.LOG_APP_RETENTION_DAYS,
    error_retention_days=_settings.LOG_ERROR_RETENTION_DAYS,
    access_retention_days=_settings.LOG_ACCESS_RETENTION_DAYS,
    audit_retention_days=_settings.LOG_AUDIT_RETENTION_DAYS,
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
    app.state.arq_pool = None
    app.state.draft_reviewer = None
    settings = get_settings()
    from clients.mcp_crawl import McpCrawlClient

    app.state.mcp_crawl_client = McpCrawlClient.from_settings(settings)

    app.state.searxng_client = None
    if settings.WEB_SEARCH_ENABLED:
        try:
            from clients.searxng import SearXNGClient
            app.state.searxng_client = SearXNGClient.from_settings(settings)
            _log("INFO", f"SearXNG client initialized: {settings.SEARXNG_URL}")
        except Exception as e:
            _log("WARNING", f"SearXNG client init failed: {e}")

    _log("INFO", "=" * 50)
    _log("INFO", "Backend starting...")
    _log("INFO", f"Python {sys.version}")
    _log(
        "INFO",
        f"MongoDB URI: {settings.MONGODB_URI.split('@')[-1] if '@' in settings.MONGODB_URI else settings.MONGODB_URI}",
    )
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
        await MongoDB.ensure_indexes()
        from agent.template_repository import TemplateRepository

        app.state.template_repository = TemplateRepository(app.state.db)
        _log("INFO", f"MongoDB connected: {settings.MONGODB_DB}")
    except Exception as e:
        _log("ERROR", f"MongoDB connection failed: {e}")
        app.state.db = None
        app.state.template_repository = None

    # Redis / ARQ
    try:
        from agent.task_queue import redis_settings
        from arq import create_pool

        app.state.arq_pool = await create_pool(redis_settings())
        _log(
            "INFO",
            f"ARQ connected: {settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}",
        )
    except Exception as e:
        _log("ERROR", f"ARQ connection failed: {e}")
        app.state.arq_pool = None

    # Agent components
    try:
        from agent.knowledge import KnowledgeLoader
        from agent.pipeline import PipelineManager
        from agent.reporter import ReportAgent
        from agent.scorer import ScoringAgent
        from agent.tools import create_mcp_toolset
        from langchain_openai import ChatOpenAI

        tools = create_mcp_toolset(
            wewe_url=settings.MCP_WEWE_URL,
            crawl_client=app.state.mcp_crawl_client,
        )
        _log("INFO", f"MCP tools initialized: {len(tools)} tools")

        knowledge_loader = KnowledgeLoader(docs_dir=settings.KNOWLEDGE_BASE_DIR)
        await knowledge_loader.load()
        app.state.knowledge_loader = knowledge_loader
        _log(
            "INFO",
            f"Knowledge loaded: {len(knowledge_loader._cache.source_files) if knowledge_loader._cache else 0} files from {settings.KNOWLEDGE_BASE_DIR}",
        )

        llm = ChatOpenAI(
            model=settings.DEEPSEEK_MODEL,
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            temperature=0.1,
            timeout=settings.DEEPSEEK_TIMEOUT,
            max_tokens=settings.DEEPSEEK_MAX_TOKENS,
        )
        app.state.llm = llm
        _log("INFO", f"LLM initialized: model={settings.DEEPSEEK_MODEL}")

        from agent.style_profiler import StyleProfiler

        app.state.style_profiler = StyleProfiler(llm=llm, db=app.state.db)
        _log("INFO", "StyleProfiler initialized")

        scorer = ScoringAgent(llm=llm, knowledge=knowledge_loader._cache)
        reporter = ReportAgent(llm=llm, knowledge=knowledge_loader._cache, db=app.state.db)

        # V2 6分类 Agent
        from agent.classifier_v2 import ClassifierV2

        classifier_v2 = ClassifierV2(llm=llm, db=app.state.db)
        app.state.classifier_v2 = classifier_v2
        _log("INFO", "ClassifierV2 initialized")

        # V2 打分 + 草稿（完整流水线仅在 ARQ Worker 中构建）
        from agent.draft_generator import DraftGenerator
        from agent.draft_reviewer import DraftReviewer
        from agent.scorer_v2 import ScoringAgentV2

        scorer_v2 = ScoringAgentV2(llm=llm, knowledge=knowledge_loader, db=app.state.db)
        draft_gen = DraftGenerator(llm=llm, knowledge=knowledge_loader._cache)
        draft_reviewer = DraftReviewer(llm=llm)
        app.state.scorer_v2 = scorer_v2
        app.state.draft_gen = draft_gen
        app.state.draft_reviewer = draft_reviewer
        _log("INFO", "V2 agents initialized; pipeline execution delegated to ARQ Worker")

        pipeline_manager = PipelineManager(
            tools=tools,
            scorer=scorer,
            reporter=reporter,
            knowledge=knowledge_loader,
            db=app.state.db,
            crawl_client=app.state.mcp_crawl_client,
        )
        app.state.pipeline_manager = pipeline_manager
        _log("INFO", "Agent pipeline initialized (V1)")
    except Exception as e:
        _log("WARNING", f"Agent init skipped: {e}")
        app.state.pipeline_manager = None
        app.state.knowledge_loader = None
        app.state.llm = None
        app.state.style_profiler = None
        app.state.draft_reviewer = None

    try:
        yield
    finally:
        # Shutdown must also run when request handling or application teardown fails.
        _log("INFO", "Shutting down backend...")
        arq_pool = getattr(app.state, "arq_pool", None)
        if arq_pool is not None:
            await arq_pool.aclose()
        await app.state.mcp_crawl_client.aclose()
        searxng_client = getattr(app.state, "searxng_client", None)
        if searxng_client is not None:
            await searxng_client.aclose()
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
app.add_exception_handler(AuthError, auth_error_handler)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Authentication + request logging middleware ────────


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """解析 JWT，并将用户 ID 写入 request.state 和日志上下文。"""
    whitelist = {"/api/health", "/api/auth/register", "/api/auth/login"}
    request.state.user_id = None
    request.state.username = None

    # 生成 request_id 并设置到上下文
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    set_request_id(request_id)

    if request.url.path not in whitelist:
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif "token" in request.query_params:
            token = request.query_params["token"]

        if token:
            payload = decode_access_token(token)
            if payload:
                request.state.user_id = payload.get("sub")
                request.state.username = payload.get("username")
                set_user_id(payload.get("sub"))

    response = await call_next(request)

    # 清理上下文
    set_request_id(None)
    set_user_id(None)

    # 透传 request_id 到响应头
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录 HTTP 请求日志：method/path/status/duration/client_ip/user_id -> access.log"""
    # 健康检查不打访问日志，避免刷屏
    if request.url.path == "/api/health":
        return await call_next(request)

    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)

    # 获取客户端 IP
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"

    # 记录到 access.log（JSON 结构化）
    log_request(
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
        client_ip=client_ip,
        user_id=getattr(request.state, "user_id", None),
        request_id=getattr(request.state, "request_id", None),
    )

    # 同时写入内存缓冲区（供 /api/logs 接口）
    level = "WARNING" if response.status_code >= 400 else "INFO"
    _log(
        level,
        f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms}ms)",
    )

    return response


# ── Routers ─────────────────────────────────────────────

from api.accounts import router as accounts_router
from api.activity import router as activity_router
from api.auth import router as auth_router
from api.chat import router as chat_router
from api.crawl_config import router as crawl_config_router
from api.dashboard import router as dashboard_router
from api.dev_logs import router as dev_logs_router
from api.feedback import router as feedback_router
from api.knowledge_admin import router as knowledge_admin_router
from api.knowledge_catalog import router as knowledge_catalog_router
from api.logs import router as logs_router
from api.memory import router as memory_router
from api.overseas_crawl import router as overseas_router
from api.personalization import router as personalization_router
from api.pipeline import llm_router
from api.pipeline import router as pipeline_router
from api.pr_templates import router as pr_templates_router
from api.profile import router as profile_router
from api.profile_policy import router as profile_policy_router
from api.reports import router as reports_router
from api.upload import router as upload_router
from api.user_prompts import router as user_prompts_router
from api.web_search import router as web_search_router

app.include_router(pipeline_router)
app.include_router(llm_router)
app.include_router(dashboard_router)
app.include_router(reports_router)
app.include_router(chat_router)
app.include_router(feedback_router)
app.include_router(activity_router)
app.include_router(profile_router)
app.include_router(pr_templates_router)
app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(logs_router)
app.include_router(dev_logs_router)
app.include_router(crawl_config_router)
app.include_router(overseas_router)
app.include_router(upload_router)
app.include_router(user_prompts_router)
app.include_router(profile_policy_router)
app.include_router(personalization_router)
app.include_router(memory_router)
app.include_router(knowledge_catalog_router)
app.include_router(knowledge_admin_router)
app.include_router(web_search_router)


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

    searxng_status = {"status": "disabled"}
    if settings.WEB_SEARCH_ENABLED:
        searxng_client = getattr(app.state, "searxng_client", None)
        if searxng_client is not None:
            try:
                available = await searxng_client.health_check()
                searxng_status = {"status": "ok" if available else "unavailable"}
            except Exception:
                searxng_status = {"status": "error"}
        else:
            searxng_status = {"status": "not_initialized"}

    return {
        "ok": True,
        "status": "healthy",
        "version": "0.1.0",
        "mongodb": mongo_health,
        "mcp_wewe": settings.MCP_WEWE_URL,
        "mcp_crawl": settings.MCP_CRAWL_URL,
        "searxng": searxng_status,
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
