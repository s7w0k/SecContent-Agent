"""SearXNG HTTP client - async client for SearXNG JSON search API."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from config import Settings

logger = logging.getLogger("backend.searxng")


class SearXNGError(Exception):
    """Base exception for SearXNG client errors."""

    def __init__(self, message: str, code: str = "SEARCH_PROVIDER_ERROR"):
        super().__init__(message)
        self.code = code


class SearXNGConnectionError(SearXNGError):
    """Connection failed."""

    def __init__(self, message: str = "无法连接搜索服务"):
        super().__init__(message, code="SEARCH_PROVIDER_UNAVAILABLE")


class SearXNGTimeoutError(SearXNGError):
    """Request timed out."""

    def __init__(self, message: str = "搜索超时"):
        super().__init__(message, code="SEARCH_PROVIDER_TIMEOUT")


class SearXNGBadResponseError(SearXNGError):
    """Invalid response from SearXNG."""

    def __init__(self, message: str = "搜索服务响应异常"):
        super().__init__(message, code="SEARCH_PROVIDER_BAD_RESPONSE")


class SearXNGRateLimitError(SearXNGError):
    """SearXNG returned 429."""

    def __init__(self, message: str = "搜索服务限流"):
        super().__init__(message, code="SEARCH_RATE_LIMITED")


class SearXNGForbiddenError(SearXNGError):
    """SearXNG returned 403 - JSON format likely not enabled."""

    def __init__(self, message: str = "搜索服务配置错误（JSON 格式未启用）"):
        super().__init__(message, code="SEARCH_PROVIDER_BAD_RESPONSE")


class SearXNGClient:
    """Async HTTP client for SearXNG JSON search API.

    Uses a shared httpx.AsyncClient connection pool.
    All errors are mapped to specific exception types.
    """

    def __init__(
        self,
        base_url: str,
        connect_timeout: float = 3.0,
        read_timeout: float = 15.0,
        max_retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=5.0,
                pool=connect_timeout,
            ),
            headers={
                "Accept": "application/json",
                "User-Agent": "PR-Agent-Search/1.0",
            },
            follow_redirects=False,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SearXNGClient:
        if settings is None:
            from config import get_settings

            settings = get_settings()
        return cls(
            base_url=settings.SEARXNG_URL,
            connect_timeout=settings.SEARXNG_CONNECT_TIMEOUT,
            read_timeout=settings.SEARXNG_READ_TIMEOUT,
            max_retries=settings.SEARXNG_MAX_RETRIES,
        )

    async def search(
        self,
        q: str,
        categories: list[str] | None = None,
        language: str | None = None,
        time_range: str | None = None,
        safesearch: int = 1,
        pageno: int = 1,
    ) -> dict[str, Any]:
        """Execute a SearXNG search and return the raw JSON response.

        Note: ``language=None`` omits the ``language`` param on purpose —
        SearXNG returns empty results for some engines when ``language=all``.

        Returns:
            Dict with keys: results, unresponsive_engines, number_of_results, etc.

        Raises:
            SearXNGConnectionError, SearXNGTimeoutError, SearXNGBadResponseError,
            SearXNGRateLimitError, SearXNGForbiddenError
        """
        params: dict[str, Any] = {
            "q": q,
            "format": "json",
            "safesearch": safesearch,
            "pageno": pageno,
        }
        if language and language != "all":
            params["language"] = language
        if categories:
            params["categories"] = ",".join(categories)
        if time_range:
            params["time_range"] = time_range

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get("/search", params=params)

                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "")
                    if "application/json" not in content_type:
                        raise SearXNGBadResponseError(f"非 JSON 响应: {content_type}")
                    # Validate response body size (max 10MB)
                    if len(response.content) > 10 * 1024 * 1024:
                        raise SearXNGBadResponseError("响应体过大")
                    data = response.json()
                    if not isinstance(data, dict):
                        raise SearXNGBadResponseError("响应根结构非对象")
                    if "results" not in data:
                        raise SearXNGBadResponseError("响应缺少 results 字段")
                    logger.info(
                        "SearXNG search completed: q_len=%d results=%d",
                        len(q),
                        len(data.get("results", [])),
                    )
                    return data

                elif response.status_code == 403:
                    raise SearXNGForbiddenError()
                elif response.status_code == 429:
                    raise SearXNGRateLimitError()
                elif response.status_code in (400,):
                    raise SearXNGBadResponseError(f"搜索请求无效: {response.status_code}")
                elif response.status_code in (502, 503, 504):
                    last_exc = SearXNGConnectionError(f"搜索服务暂时不可用: {response.status_code}")
                    if attempt < self._max_retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise last_exc
                else:
                    raise SearXNGBadResponseError(f"未知响应码: {response.status_code}")

            except httpx.ConnectError as e:
                last_exc = SearXNGConnectionError(str(e))
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise last_exc from None
            except httpx.ReadTimeout:
                last_exc = SearXNGTimeoutError()
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise last_exc from None
            except httpx.PoolTimeout as e:
                last_exc = SearXNGConnectionError(str(e))
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise last_exc from None

        if last_exc:
            raise last_exc from None
        raise SearXNGError("搜索失败: 未知原因")

    async def health_check(self) -> bool:
        """Check if SearXNG is reachable."""
        try:
            response = await self._client.get("/healthz", timeout=3.0)
            return response.status_code == 200
        except Exception:
            return False

    async def aclose(self):
        """Close the underlying HTTP client."""
        await self._client.aclose()
