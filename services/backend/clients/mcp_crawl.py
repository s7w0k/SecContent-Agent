"""Async client for the independently deployable mcp-crawl HTTP Bridge."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger("backend.clients.mcp_crawl")

HEADER_REQUEST_ID = "X-Request-ID"
HEADER_TRACE_ID = "X-Trace-ID"
HEADER_INITIATOR_USER_ID = "X-Initiator-User-ID"

ERROR_UNAUTHORIZED = "UNAUTHORIZED"
ERROR_FORBIDDEN = "FORBIDDEN"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_CRAWLER_BUSY = "CRAWLER_BUSY"
ERROR_UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
ERROR_MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
ERROR_INVALID_UPSTREAM_RESPONSE = "INVALID_UPSTREAM_RESPONSE"


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Cross-service request attribution propagated to the crawler node."""

    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str | None = None
    initiator_user_id: str | None = None

    def as_headers(self) -> dict[str, str]:
        headers = {HEADER_REQUEST_ID: self.request_id}
        if self.trace_id:
            headers[HEADER_TRACE_ID] = self.trace_id
        if self.initiator_user_id:
            headers[HEADER_INITIATOR_USER_ID] = self.initiator_user_id
        return headers


class McpCrawlError(RuntimeError):
    """Stable, secret-safe error raised for all mcp-crawl failures."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.retryable = retryable


class McpCrawlClient:
    """Connection-pooled HTTP client with retries, validation and trace headers."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | SecretStr = "",
        connect_timeout: float = 5.0,
        read_timeout: float = 300.0,
        max_retries: int = 2,
        max_response_mb: int = 20,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._api_key = secret
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_retries = max_retries
        self._max_response_bytes = max_response_mb * 1024 * 1024
        headers = {"Accept": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self._timeout(read_timeout),
            verify=verify_tls,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> McpCrawlClient:
        """Build an API/Worker-identical client from central settings."""
        return cls(
            base_url=settings.MCP_CRAWL_URL,
            api_key=settings.MCP_CRAWL_API_KEY,
            connect_timeout=settings.MCP_CRAWL_CONNECT_TIMEOUT,
            read_timeout=settings.MCP_CRAWL_READ_TIMEOUT,
            max_retries=settings.MCP_CRAWL_MAX_RETRIES,
            max_response_mb=settings.MCP_CRAWL_MAX_RESPONSE_MB,
            verify_tls=settings.MCP_CRAWL_VERIFY_TLS,
        )

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> McpCrawlClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def health(self, context: RequestContext | None = None) -> dict[str, Any]:
        data = await self._request(
            "GET",
            "/health",
            context=context,
            read_timeout=min(self._read_timeout, 3.0),
            max_retries=0,
        )
        return self._require_dict(data, endpoint="/health")

    async def crawl_news(
        self,
        days: int,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        data = await self._request(
            "POST",
            "/crawl-news",
            json_data={"days": days},
            context=context,
        )
        return self._require_dict(data, endpoint="/crawl-news")

    async def fetch_fulltext(
        self,
        url: str,
        context: RequestContext | None = None,
    ) -> str:
        data = self._require_dict(
            await self._request(
                "POST",
                "/fetch-fulltext",
                json_data={"url": url},
                context=context,
                read_timeout=min(self._read_timeout, 60.0),
                max_retries=min(self._max_retries, 1),
            ),
            endpoint="/fetch-fulltext",
        )
        content = data.get("content_md")
        if not isinstance(content, str):
            raise self._invalid_response("/fetch-fulltext missing string content_md")
        return content

    async def fetch_fulltext_batch(
        self,
        urls: list[str],
        context: RequestContext | None = None,
    ) -> dict[str, str]:
        response = self._require_dict(
            await self._request(
                "POST",
                "/fetch-fulltext-batch",
                json_data=urls,
                context=context,
                read_timeout=min(self._read_timeout, 180.0),
                max_retries=min(self._max_retries, 1),
            ),
            endpoint="/fetch-fulltext-batch",
        )
        data = response.get("data")
        if not isinstance(data, dict) or not all(
            isinstance(url, str) and isinstance(content, str) for url, content in data.items()
        ):
            raise self._invalid_response("/fetch-fulltext-batch has invalid data mapping")
        return data

    def _timeout(self, read_timeout: float) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self._connect_timeout,
            read=read_timeout,
            write=self._connect_timeout,
            pool=self._connect_timeout,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: Any = None,
        context: RequestContext | None = None,
        read_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> Any:
        request_context = context or RequestContext()
        retries = self._max_retries if max_retries is None else max_retries
        timeout = self._timeout(read_timeout or self._read_timeout)

        for attempt in range(retries + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json_data,
                    headers=request_context.as_headers(),
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                if attempt < retries:
                    await self._backoff(attempt)
                    continue
                raise McpCrawlError(
                    ERROR_UPSTREAM_TIMEOUT,
                    "mcp-crawl request timed out",
                    request_id=request_context.request_id,
                    retryable=True,
                ) from exc
            except httpx.RequestError as exc:
                if attempt < retries:
                    await self._backoff(attempt)
                    continue
                raise McpCrawlError(
                    ERROR_MCP_UNAVAILABLE,
                    "mcp-crawl is unavailable",
                    request_id=request_context.request_id,
                    retryable=True,
                ) from exc

            if (response.status_code == 429 or response.status_code >= 500) and attempt < retries:
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if response.is_error:
                raise self._http_error(response, request_context.request_id)

            content_length = response.headers.get("Content-Length")
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > self._max_response_bytes
            ):
                raise self._invalid_response("mcp-crawl response exceeds configured limit")
            if len(response.content) > self._max_response_bytes:
                raise self._invalid_response("mcp-crawl response exceeds configured limit")
            try:
                data = response.json()
            except ValueError as exc:
                raise self._invalid_response("mcp-crawl returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise self._invalid_response("mcp-crawl response must be a JSON object")
            if data.get("ok") is False:
                raise self._contract_error(data, response.status_code, request_context.request_id)
            return data

        raise AssertionError("unreachable")

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(0.25 * (2**attempt), 5.0)
        if retry_after:
            with suppress(ValueError):
                delay = min(max(float(retry_after), 0.0), 5.0)
        logger.warning("Retrying mcp-crawl request after %.2fs (attempt=%d)", delay, attempt + 1)
        await asyncio.sleep(delay)

    def _http_error(self, response: httpx.Response, request_id: str) -> McpCrawlError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("ok") is False:
            return self._contract_error(payload, response.status_code, request_id)
        mapping = {
            401: (ERROR_UNAUTHORIZED, False),
            403: (ERROR_FORBIDDEN, False),
            422: (ERROR_INVALID_REQUEST, False),
            429: (ERROR_CRAWLER_BUSY, True),
            503: (ERROR_MCP_UNAVAILABLE, True),
            504: (ERROR_UPSTREAM_TIMEOUT, True),
        }
        code, retryable = mapping.get(
            response.status_code,
            (
                ERROR_MCP_UNAVAILABLE if response.status_code >= 500 else ERROR_INVALID_REQUEST,
                response.status_code >= 500,
            ),
        )
        return McpCrawlError(
            code,
            f"mcp-crawl returned HTTP {response.status_code}",
            status_code=response.status_code,
            request_id=response.headers.get(HEADER_REQUEST_ID, request_id),
            retryable=retryable,
        )

    def _contract_error(
        self,
        payload: dict[str, Any],
        status_code: int,
        request_id: str,
    ) -> McpCrawlError:
        error = payload.get("error")
        if not isinstance(error, dict):
            return self._invalid_response("mcp-crawl returned an invalid error object")
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            return self._invalid_response("mcp-crawl returned an invalid error contract")
        return McpCrawlError(
            code,
            self._redact(message),
            status_code=status_code,
            request_id=error.get("request_id") or request_id,
            retryable=error.get("retryable") is True,
        )

    @staticmethod
    def _require_dict(data: Any, *, endpoint: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise McpCrawlClient._invalid_response(f"{endpoint} returned invalid data")
        return data

    @staticmethod
    def _invalid_response(message: str) -> McpCrawlError:
        return McpCrawlError(ERROR_INVALID_UPSTREAM_RESPONSE, message)

    def _redact(self, message: str) -> str:
        if self._api_key:
            return message.replace(self._api_key, "[REDACTED]")
        return message
