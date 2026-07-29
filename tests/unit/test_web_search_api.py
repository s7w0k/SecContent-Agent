"""Web search API - unit tests for /api/search endpoints.

Run:
    pytest tests/unit/test_web_search_api.py -v
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from auth.deps import get_current_user
from clients.searxng import (
    SearXNGConnectionError,
    SearXNGRateLimitError,
    SearXNGTimeoutError,
)

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _mock_settings(
    web_search_enabled: bool = True,
    result_limit: int = 20,
    allowed_categories: str = "general,news",
    allowed_languages: str = "all,zh-CN,en",
    import_batch_limit: int = 20,
    session_ttl_minutes: int = 30,
) -> MagicMock:
    """Create a mock Settings object with web search config."""
    mock = MagicMock()
    mock.WEB_SEARCH_ENABLED = web_search_enabled
    mock.SEARXNG_URL = "http://searxng:8080"
    mock.WEB_SEARCH_RESULT_LIMIT = result_limit
    mock.WEB_SEARCH_SESSION_TTL_MINUTES = session_ttl_minutes
    mock.WEB_SEARCH_ALLOWED_CATEGORIES = allowed_categories
    mock.WEB_SEARCH_ALLOWED_LANGUAGES = allowed_languages
    mock.WEB_SEARCH_IMPORT_BATCH_LIMIT = import_batch_limit
    return mock


def _mock_db() -> MagicMock:
    """Create a mock Motor MongoDB database with articles and search_sessions collections."""
    db = MagicMock()

    # search_sessions collection
    sessions_coll = MagicMock()
    sessions_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="test_id"))
    sessions_coll.find_one = AsyncMock(return_value=None)
    sessions_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=0))

    # articles collection (used by mark_imported_results)
    articles_coll = MagicMock()
    articles_cursor = MagicMock()
    articles_cursor.to_list = AsyncMock(return_value=[])
    articles_coll.find = MagicMock(return_value=articles_cursor)

    def _getitem(key):
        if key == "search_sessions":
            return sessions_coll
        if key == "articles":
            return articles_coll
        return MagicMock()

    db.__getitem__ = MagicMock(side_effect=_getitem)
    db._sessions = sessions_coll
    db._articles = articles_coll
    return db


def _mock_client(search_return=None, health=True) -> AsyncMock:
    """Create a mock SearXNG client."""
    client = AsyncMock()
    client.search = AsyncMock(
        return_value=search_return or {"results": [], "unresponsive_engines": []}
    )
    client.health_check = AsyncMock(return_value=health)
    return client


def _normalize_test_response() -> dict:
    """Search response used by normalize tests (HTML strip, non-HTTP filter, dedup)."""
    return {
        "results": [
            {
                "title": "<b>Test</b> Title",
                "url": "https://example.com/article",
                "content": "<p>Summary</p>",
                "engines": ["bing"],
                "score": 1.5,
            },
            {
                "title": "Non-HTTP",
                "url": "file:///etc/passwd",
                "content": "bad",
                "engines": ["google"],
            },
            {
                "title": "Duplicate",
                "url": "https://example.com/article?utm_source=test",
                "content": "dup",
                "engines": ["duckduckgo"],
            },
        ],
        "unresponsive_engines": [["google", "timeout"]],
    }


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def app_with_state():
    """Set up app.state with mock db and searxng_client, override auth."""
    from main import app

    db = _mock_db()
    client = _mock_client()

    app.state.searxng_client = client
    app.state.db = db

    async def override_user():
        return "test-user-id"

    app.dependency_overrides[get_current_user] = override_user

    yield app

    # Cleanup
    app.state.searxng_client = None
    app.state.db = None
    app.dependency_overrides.clear()


@pytest.fixture
def disabled_client_app():
    """Set up app.state with searxng_client=None (runtime unavailable)."""
    from main import app

    db = _mock_db()
    app.state.searxng_client = None
    app.state.db = db

    async def override_user():
        return "test-user-id"

    app.dependency_overrides[get_current_user] = override_user

    yield app

    app.state.searxng_client = None
    app.state.db = None
    app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════
# 1. GET /api/search/status
# ═══════════════════════════════════════════════════════════════


class TestSearchStatus:
    """GET /api/search/status endpoint tests."""

    @pytest.mark.asyncio
    async def test_status_disabled(self, app_with_state):
        """Returns enabled=False when WEB_SEARCH_ENABLED is false."""
        settings = _mock_settings(web_search_enabled=False)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.get("/api/search/status")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is False
        assert data["available"] is False

    @pytest.mark.asyncio
    async def test_status_enabled_and_available(self, app_with_state):
        """Returns enabled=True and available when client is healthy."""
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.get("/api/search/status")

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is True
        assert data["available"] is True
        app_with_state.state.searxng_client.health_check.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════
# 2. POST /api/search/query
# ═══════════════════════════════════════════════════════════════


class TestSearchQuery:
    """POST /api/search/query endpoint tests."""

    @pytest.mark.asyncio
    async def test_query_search_disabled(self, app_with_state):
        """Returns 503 SEARCH_DISABLED when WEB_SEARCH_ENABLED is false."""
        settings = _mock_settings(web_search_enabled=False)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "SEARCH_DISABLED"

    @pytest.mark.asyncio
    async def test_query_client_none(self, disabled_client_app):
        """Returns 503 when searxng_client is None."""
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=disabled_client_app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "SEARCH_PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_query_returns_results(self, app_with_state):
        """Returns results when enabled and client works."""
        search_response = {
            "results": [
                {
                    "title": "Test Result",
                    "url": "https://example.com/test",
                    "content": "Test content",
                    "engines": ["google"],
                    "score": 1.0,
                },
            ],
            "unresponsive_engines": [],
        }
        app_with_state.state.searxng_client.search = AsyncMock(return_value=search_response)
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["search_id"].startswith("srch_")
        assert len(data["results"]) == 1
        assert data["results"][0]["title"] == "Test Result"
        assert data["results"][0]["url"] == "https://example.com/test"

    @pytest.mark.asyncio
    async def test_query_connection_error_maps_503(self, app_with_state):
        """SearXNGConnectionError maps to 503."""
        app_with_state.state.searxng_client.search = AsyncMock(
            side_effect=SearXNGConnectionError("connection failed")
        )
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "SEARCH_PROVIDER_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_query_timeout_error_maps_504(self, app_with_state):
        """SearXNGTimeoutError maps to 504."""
        app_with_state.state.searxng_client.search = AsyncMock(
            side_effect=SearXNGTimeoutError("timeout")
        )
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 504
        assert resp.json()["detail"]["code"] == "SEARCH_PROVIDER_TIMEOUT"

    @pytest.mark.asyncio
    async def test_query_rate_limit_error_maps_429(self, app_with_state):
        """SearXNGRateLimitError maps to 429."""
        app_with_state.state.searxng_client.search = AsyncMock(
            side_effect=SearXNGRateLimitError("rate limited")
        )
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 429
        assert resp.json()["detail"]["code"] == "SEARCH_RATE_LIMITED"

    @pytest.mark.asyncio
    async def test_query_filters_non_http_results(self, app_with_state):
        """Results with file:// URLs are filtered out."""
        app_with_state.state.searxng_client.search = AsyncMock(
            return_value=_normalize_test_response()
        )
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        # Non-HTTP filtered + duplicate removed = 1 result
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com/article"

    @pytest.mark.asyncio
    async def test_query_strips_html_from_title_and_snippet(self, app_with_state):
        """HTML tags are stripped from title and snippet."""
        app_with_state.state.searxng_client.search = AsyncMock(
            return_value=_normalize_test_response()
        )
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 200
        result = resp.json()["data"]["results"][0]
        assert result["title"] == "Test Title"
        assert result["snippet"] == "Summary"

    @pytest.mark.asyncio
    async def test_query_deduplicates_by_canonical_url(self, app_with_state):
        """Duplicate results (by canonical URL) are merged, engines combined."""
        app_with_state.state.searxng_client.search = AsyncMock(
            return_value=_normalize_test_response()
        )
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.post("/api/search/query", json={"q": "test query"})

        assert resp.status_code == 200
        results = resp.json()["data"]["results"]
        assert len(results) == 1
        assert results[0]["engines"] == ["bing", "duckduckgo"]


# ═══════════════════════════════════════════════════════════════
# 3. GET /api/search/sessions/{search_id}
# ═══════════════════════════════════════════════════════════════


class TestSearchSession:
    """GET /api/search/sessions/{search_id} endpoint tests."""

    @pytest.mark.asyncio
    async def test_get_session_not_found(self, app_with_state):
        """Returns 404 for non-existent session."""
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.get("/api/search/sessions/srch_nonexistent")

        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "SEARCH_SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_get_session_other_user_returns_404(self, app_with_state):
        """Returns 404 for a session belonging to another user (user isolation)."""
        # find_one returns None because the user_id filter doesn't match -
        # the session exists for another user but is invisible to test-user-id.
        app_with_state.state.db._sessions.find_one = AsyncMock(return_value=None)
        settings = _mock_settings(web_search_enabled=True)
        with patch("api.web_search.get_settings", return_value=settings):
            async with AsyncClient(
                transport=ASGITransport(app=app_with_state), base_url="http://test"
            ) as client:
                resp = await client.get("/api/search/sessions/srch_other_user")

        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "SEARCH_SESSION_NOT_FOUND"
