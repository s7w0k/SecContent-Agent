"""将 mcp-crawl stdio 服务安全地桥接为 HTTP API。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_PATH = os.path.join(SCRIPT_DIR, "server.py")
LOGGER = logging.getLogger("mcp-crawl.bridge")
PUBLIC_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})
MAX_URL_LENGTH = 4096

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        with suppress(OSError, AttributeError):
            stream.reconfigure(encoding="utf-8", line_buffering=True)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class BridgeSettings:
    """仅属于独立爬虫 Bridge 的安全配置。"""

    api_key: str
    max_concurrency: int
    max_batch_urls: int
    max_articles: int
    max_response_bytes: int
    max_request_bytes: int
    cors_origins: tuple[str, ...]


@lru_cache(maxsize=1)
def get_bridge_settings() -> BridgeSettings:
    """从环境变量加载配置；缓存可由测试或配置重载显式清除。"""
    response_mb = _env_int("MCP_CRAWL_MAX_RESPONSE_MB", 20, 1, 100)
    request_mb = _env_int("MCP_CRAWL_MAX_REQUEST_MB", 5, 1, 20)
    origins = tuple(
        origin.strip()
        for origin in os.getenv("MCP_CRAWL_CORS_ORIGINS", "").split(",")
        if origin.strip() and origin.strip() != "*"
    )
    return BridgeSettings(
        api_key=os.getenv("MCP_CRAWL_API_KEY", "").strip(),
        max_concurrency=_env_int("MCP_CRAWL_MAX_CONCURRENCY", 2, 1, 32),
        max_batch_urls=_env_int("MCP_CRAWL_MAX_BATCH_URLS", 100, 1, 500),
        max_articles=_env_int("MCP_CRAWL_MAX_ARTICLES", 500, 1, 5000),
        max_response_bytes=response_mb * 1024 * 1024,
        max_request_bytes=request_mb * 1024 * 1024,
        cors_origins=origins,
    )


class BridgeError(Exception):
    """可安全返回给调用方的 Bridge 错误。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = headers or {}


class ConcurrencyLimiter:
    """非等待式并发限制器，超限请求立即返回 429。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            if self._active >= self.limit:
                raise BridgeError(
                    429,
                    "CRAWLER_BUSY",
                    "爬虫服务繁忙，请稍后重试",
                    retryable=True,
                    headers={"Retry-After": "1"},
                )
            self._active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1


_mcp_session: ClientSession | None = None
_mcp_tools: dict[str, dict[str, Any]] = {}
_stdio_ctx = None
_session_ctx = None
_heavy_limiter = ConcurrencyLimiter(get_bridge_settings().max_concurrency)


async def _init_mcp() -> None:
    """启动 MCP Server 子进程并建立持久会话。"""
    global _mcp_session, _mcp_tools, _stdio_ctx, _session_ctx
    params = StdioServerParameters(
        command=sys.executable,
        args=[MCP_SERVER_PATH],
        env={**os.environ},
    )
    _stdio_ctx = stdio_client(params)
    read, write = await _stdio_ctx.__aenter__()
    _session_ctx = ClientSession(read, write)
    _mcp_session = await _session_ctx.__aenter__()
    await _mcp_session.initialize()
    result = await _mcp_session.list_tools()
    _mcp_tools = {
        tool.name: {
            "description": tool.description,
            # mcp 包新版将 Tool.inputSchema 改为 input_schema，兼容两种
            "inputSchema": getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None),
        }
        for tool in result.tools
    }


async def _shutdown_mcp() -> None:
    """关闭 MCP 会话和子进程。"""
    global _mcp_session, _stdio_ctx, _session_ctx
    if _session_ctx:
        await _session_ctx.__aexit__(None, None, None)
    if _stdio_ctx:
        await _stdio_ctx.__aexit__(None, None, None)
    _mcp_session = None
    _session_ctx = None
    _stdio_ctx = None


async def _call_tool(name: str, arguments: dict[str, Any] | None = None) -> str:
    """调用 MCP 工具，仅向客户端暴露稳定错误码。"""
    if _mcp_session is None:
        raise BridgeError(503, "MCP_UNAVAILABLE", "MCP 服务尚未就绪", retryable=True)
    try:
        result = await _mcp_session.call_tool(name, arguments or {})
    except Exception as exc:
        LOGGER.warning("MCP tool call failed: %s", type(exc).__name__)
        raise BridgeError(502, "MCP_CALL_FAILED", "MCP 工具调用失败", retryable=True) from exc
    return "\n".join(item.text for item in result.content if hasattr(item, "text"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _init_mcp()
    if not get_bridge_settings().api_key:
        LOGGER.warning(
            "MCP_CRAWL_API_KEY is empty; bridge is running in embedded compatibility mode"
        )
    yield
    await _shutdown_mcp()


app = FastAPI(
    title="MCP-Crawl HTTP Bridge",
    version="1.1.0",
    description="海外安全新闻爬虫 MCP HTTP Bridge",
    lifespan=lifespan,
)

_cors_origins = get_bridge_settings().cors_origins
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Trace-ID",
            "X-Initiator-User-ID",
        ],
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "retryable": retryable,
            },
        },
    )


@app.exception_handler(BridgeError)
async def bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
    return _error_response(
        request,
        exc.status_code,
        exc.code,
        exc.message,
        retryable=exc.retryable,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, _exc: RequestValidationError) -> JSONResponse:
    return _error_response(request, 422, "INVALID_REQUEST", "请求参数不合法")


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
    message = "请求的资源不存在" if exc.status_code == 404 else "请求处理失败"
    return _error_response(request, exc.status_code, code, message)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    LOGGER.error("Unhandled bridge error: %s", type(exc).__name__)
    return _error_response(request, 500, "INTERNAL_ERROR", "服务内部错误", retryable=True)


def _authenticated(request: Request) -> bool:
    expected = get_bridge_settings().api_key
    if not expected:
        return True
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return scheme.lower() == "bearer" and bool(token) and secrets.compare_digest(token, expected)


@app.middleware("http")
async def security_and_audit_middleware(request: Request, call_next):
    """注入链路上下文、执行机器认证并记录不含敏感信息的调用日志。"""
    started = time.perf_counter()
    request.state.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.trace_id = request.headers.get("X-Trace-ID") or request.state.request_id
    request.state.user_id = request.headers.get("X-Initiator-User-ID") or "system"
    request.state.result_count = None

    content_length = request.headers.get("content-length")
    too_large = (
        bool(content_length and content_length.isdigit())
        and int(content_length) > get_bridge_settings().max_request_bytes
    )
    if request.url.path not in PUBLIC_PATHS and not _authenticated(request):
        response = _error_response(request, 401, "UNAUTHORIZED", "机器 Token 无效")
        response.headers["WWW-Authenticate"] = "Bearer"
    elif too_large:
        response = _error_response(request, 413, "REQUEST_TOO_LARGE", "请求体超过大小限制")
    else:
        response = await call_next(request)

    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Trace-ID"] = request.state.trace_id
    event = {
        "event": "mcp_crawl_http_call",
        "service": "mcp-crawl",
        "method": request.method,
        "path": request.url.path,
        "status_code": response.status_code,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "request_id": request.state.request_id,
        "trace_id": request.state.trace_id,
        "user_id": request.state.user_id,
        "client_ip": request.client.host if request.client else "unknown",
    }
    if request.state.result_count is not None:
        event["result_count"] = request.state.result_count
    LOGGER.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    return response


def _validate_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if (
        not value
        or len(value) > MAX_URL_LENGTH
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise BridgeError(422, "INVALID_URL", "URL 必须是有效的 HTTP(S) 地址")
    return value


def _validate_urls(urls: list[str]) -> list[str]:
    settings = get_bridge_settings()
    if not urls or len(urls) > settings.max_batch_urls:
        raise BridgeError(422, "INVALID_BATCH_SIZE", f"URL 数量必须为 1-{settings.max_batch_urls}")
    return [_validate_url(url) for url in urls]


def _ensure_response_size(payload: Any) -> Any:
    if isinstance(payload, dict):
        data = payload.get("data")
        articles = data.get("articles") if isinstance(data, dict) else None
        if isinstance(articles, list) and len(articles) > get_bridge_settings().max_articles:
            raise BridgeError(502, "TOO_MANY_ARTICLES", "爬虫返回文章数超过限制")
    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) > get_bridge_settings().max_response_bytes:
        raise BridgeError(502, "RESPONSE_TOO_LARGE", "爬虫响应超过大小限制")
    return payload


def _result_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("count"), int):
        return payload["count"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("count"), int):
        return data["count"]
    return None


def _parse_tool_json(result_text: str) -> Any:
    try:
        payload = json.loads(result_text)
    except json.JSONDecodeError as exc:
        raise BridgeError(502, "INVALID_MCP_RESPONSE", "MCP 返回了无效响应") from exc
    return _ensure_response_size(payload)


def _validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> None:
    """防止通用工具入口绕过快捷端点的核心参数限制。"""
    if tool_name == "crawl_news":
        days = arguments.get("days", 1)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 30:
            raise BridgeError(422, "INVALID_REQUEST", "days 必须为 1-30 的整数")
    elif tool_name == "classify_articles":
        articles_json = arguments.get("articles_json")
        batch_size = arguments.get("batch_size", 25)
        if (
            not isinstance(articles_json, str)
            or len(articles_json.encode("utf-8")) > get_bridge_settings().max_request_bytes
        ):
            raise BridgeError(422, "INVALID_REQUEST", "articles_json 不合法")
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 100
        ):
            raise BridgeError(422, "INVALID_REQUEST", "batch_size 必须为 1-100 的整数")
    elif tool_name == "query_database":
        days = arguments.get("days", 7)
        category = arguments.get("category", "")
        keyword = arguments.get("keyword", "")
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise BridgeError(422, "INVALID_REQUEST", "days 必须为 1-365 的整数")
        if not isinstance(category, str) or len(category) > 100:
            raise BridgeError(422, "INVALID_REQUEST", "category 不合法")
        if not isinstance(keyword, str) or len(keyword) > 200:
            raise BridgeError(422, "INVALID_REQUEST", "keyword 不合法")


@app.get("/health")
async def health() -> dict[str, Any]:
    """免认证健康检查，供 Docker/Kubernetes 探针使用。"""
    return {
        "ok": True,
        "status": "healthy",
        "mcp_connected": _mcp_session is not None,
        "tools_count": len(_mcp_tools),
    }


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    return {"tools": _mcp_tools}


@app.post("/call/{tool_name}")
async def call_tool_endpoint(
    request: Request,
    tool_name: str,
    arguments: dict[str, Any] = Body(default={}),
) -> Any:
    if tool_name not in _mcp_tools:
        raise BridgeError(404, "TOOL_NOT_FOUND", "请求的工具不存在")
    if (
        len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
        > get_bridge_settings().max_request_bytes
    ):
        raise BridgeError(413, "REQUEST_TOO_LARGE", "工具参数超过大小限制")
    _validate_tool_arguments(tool_name, arguments)
    async with _heavy_limiter.slot():
        payload = _parse_tool_json(await _call_tool(tool_name, arguments))
    request.state.result_count = _result_count(payload)
    return payload


@app.post("/crawl-news")
async def crawl_news(
    request: Request,
    days: int = Body(1, embed=True, ge=1, le=30),
) -> Any:
    async with _heavy_limiter.slot():
        payload = _parse_tool_json(await _call_tool("crawl_news", {"days": days}))
    request.state.result_count = _result_count(payload)
    return payload


@app.post("/fetch-fulltext")
async def fetch_fulltext(
    request: Request,
    url: str = Body(..., embed=True, min_length=1, max_length=MAX_URL_LENGTH),
) -> dict[str, Any]:
    from crawler import NewsCrawler

    safe_url = _validate_url(url)
    async with _heavy_limiter.slot():
        content = await NewsCrawler().fetch_fulltext(safe_url)
    if not content:
        raise BridgeError(502, "EMPTY_CONTENT", "抓取结果为空", retryable=True)
    payload = _ensure_response_size({"ok": True, "content_md": content, "length": len(content)})
    request.state.result_count = 1
    return payload


@app.post("/fetch-fulltext-batch")
async def fetch_fulltext_batch(request: Request, urls: list[str] = Body(...)) -> dict[str, Any]:
    from crawler import NewsArticle, NewsCrawler

    safe_urls = _validate_urls(urls)
    articles = [NewsArticle(title="", url=url, source="") for url in safe_urls]
    async with _heavy_limiter.slot():
        results = await NewsCrawler().fetch_fulltext_batch(articles)
    url_map = {
        article.url: results[article.url_hash]
        for article in articles
        if article.url_hash in results
    }
    payload = _ensure_response_size(
        {"ok": True, "data": url_map, "success": len(url_map), "total": len(safe_urls)}
    )
    request.state.result_count = len(url_map)
    return payload


@app.post("/classify")
async def classify(
    request: Request,
    articles_json: str = Body(..., min_length=2),
    batch_size: int = Body(25, embed=True, ge=1, le=100),
) -> Any:
    if len(articles_json.encode("utf-8")) > get_bridge_settings().max_request_bytes:
        raise BridgeError(413, "REQUEST_TOO_LARGE", "文章数据超过大小限制")
    async with _heavy_limiter.slot():
        payload = _parse_tool_json(
            await _call_tool(
                "classify_articles",
                {"articles_json": articles_json, "batch_size": batch_size},
            )
        )
    request.state.result_count = _result_count(payload)
    return payload


@app.post("/query")
async def query(
    request: Request,
    category: str = Body("", embed=True, max_length=100),
    days: int = Body(7, embed=True, ge=1, le=365),
    keyword: str = Body("", embed=True, max_length=200),
) -> Any:
    payload = _parse_tool_json(
        await _call_tool(
            "query_database",
            {"category": category, "days": days, "keyword": keyword},
        )
    )
    request.state.result_count = _result_count(payload)
    return payload


@app.get("/stats")
async def stats() -> Any:
    return _parse_tool_json(await _call_tool("get_stats", {}))


@app.post("/export-csv")
async def export_csv(category: str = Body("", embed=True, max_length=100)) -> Any:
    return _parse_tool_json(await _call_tool("export_csv", {"category": category}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP-Crawl HTTP Bridge")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(
        app, host=args.host, port=args.port, log_level=os.getenv("LOG_LEVEL", "INFO").lower()
    )
