"""Unit tests for web search article enrichment task.

Tests the `enrich_web_search_articles` ARQ task with mocked httpx and MongoDB.

Run:
    pytest tests/unit/test_web_search_enrich.py -v
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# Fake MongoDB helpers
# ═══════════════════════════════════════════════════════════════


def _make_article(
    url_hash: str = "hash-1",
    url: str = "https://example.com/article",
    content_md: str = "",
    source_type: str = "web_search",
) -> dict:
    """Create a minimal article document for testing."""
    return {
        "url_hash": url_hash,
        "url": url,
        "content_md": content_md,
        "source_type": source_type,
        "content_fetch_status": "queued",
        "pipeline_status": "pending_enrichment",
    }


class FakeArticlesCollection:
    """Fake articles collection that tracks update_one calls."""

    def __init__(self, document: dict | None = None):
        self.document = deepcopy(document)
        self.updates: list[tuple[dict, dict]] = []

    async def find_one(self, query: dict, projection: dict | None = None):
        if self.document and self.document.get("url_hash") == query.get("url_hash"):
            return deepcopy(self.document)
        return None

    async def update_one(self, query: dict, update: dict):
        self.updates.append((deepcopy(query), deepcopy(update)))
        if self.document and self.document.get("url_hash") == query.get("url_hash"):
            self.document.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(matched_count=1, modified_count=1)


class FakeDatabase:
    """Fake database exposing an articles collection."""

    def __init__(self, document: dict | None = None):
        self.articles = FakeArticlesCollection(document)

    def __getitem__(self, name: str):
        assert name == "articles"
        return self.articles


def _mock_httpx_response(
    text: str = "<html><body><p>Hello World</p></body></html>",
    content_type: str = "text/html; charset=utf-8",
) -> MagicMock:
    """Create a mock httpx response."""
    response = MagicMock()
    response.text = text
    response.headers = {"content-type": content_type}
    return response


def _setup_httpx_client_mock(
    mock_client_class: MagicMock,
    response: MagicMock | None = None,
    side_effect: Exception | None = None,
) -> AsyncMock:
    """Configure httpx.AsyncClient mock to support async context manager.

    The enrich_web_search_articles task uses::

        async with httpx.AsyncClient(...) as client:
            resp = await client.get(url)

    So we need ``AsyncClient(...)`` to return an object whose
    ``__aenter__`` returns the mock client itself.
    """
    mock_client = AsyncMock()
    if side_effect is not None:
        mock_client.get = AsyncMock(side_effect=side_effect)
    else:
        mock_client.get = AsyncMock(return_value=response)

    # async with httpx.AsyncClient(...) as client:
    mock_client_class.return_value = mock_client
    mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
    return mock_client


def _last_update_set(collection: FakeArticlesCollection) -> dict:
    """Return the $set dict from the most recent update_one call."""
    assert collection.updates, "Expected at least one update_one call"
    _query, update = collection.updates[-1]
    return update.get("$set", {})


# ═══════════════════════════════════════════════════════════════
# Tests for enrich_web_search_articles
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_enrich_successfully_fetches_and_updates_content():
    """Article with empty content_md is fetched and marked completed."""
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(url="https://example.com/article", content_md="")
    db = FakeDatabase(article)
    ctx = {"db": db}

    response = _mock_httpx_response(
        text="<html><body><article>Test content here.</article></body></html>",
        content_type="text/html; charset=utf-8",
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class, response=response)
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 1, "failed": 0, "skipped": 0}
    # Verify final update set content and status
    final_set = _last_update_set(db.articles)
    assert final_set["content_fetch_status"] == "completed"
    assert final_set["content_fetch_error"] is None
    assert final_set["pipeline_status"] == "ready"
    assert "Test content here." in final_set["content_md"]


@pytest.mark.asyncio
async def test_enrich_skips_articles_with_existing_content():
    """Articles that already have content_md are skipped."""
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(content_md="existing content")
    db = FakeDatabase(article)
    ctx = {"db": db}

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class)
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 0, "failed": 0, "skipped": 1}
    # No httpx calls should have been made
    mock_client_class.assert_not_called()
    # No update_one calls (other than none) should have happened
    assert db.articles.updates == []


@pytest.mark.asyncio
async def test_enrich_marks_blocked_for_unsafe_url():
    """Articles with unsafe URLs (e.g. loopback) are marked blocked."""
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(url="http://127.0.0.1/path")
    db = FakeDatabase(article)
    ctx = {"db": db}

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class)
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 0, "failed": 1, "skipped": 0}
    mock_client_class.assert_not_called()
    blocked_set = _last_update_set(db.articles)
    assert blocked_set["content_fetch_status"] == "blocked"
    assert "URL不安全" in blocked_set["content_fetch_error"]


@pytest.mark.asyncio
async def test_enrich_marks_blocked_for_non_html_content_type():
    """Non-HTML content types are marked blocked."""
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(url="https://example.com/file.pdf")
    db = FakeDatabase(article)
    ctx = {"db": db}

    response = _mock_httpx_response(
        text="%PDF-1.4 binary",
        content_type="application/pdf",
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class, response=response)
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 0, "failed": 1, "skipped": 0}
    blocked_set = _last_update_set(db.articles)
    assert blocked_set["content_fetch_status"] == "blocked"
    assert "application/pdf" in blocked_set["content_fetch_error"]


@pytest.mark.asyncio
async def test_enrich_marks_failed_for_empty_content():
    """HTML with no extractable text is marked failed."""
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(url="https://example.com/empty")
    db = FakeDatabase(article)
    ctx = {"db": db}

    # HTML where all text is inside removed tags (script/style/nav)
    response = _mock_httpx_response(
        text="<html><head><script>alert(1)</script></head><body></body></html>",
        content_type="text/html",
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class, response=response)
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 0, "failed": 1, "skipped": 0}
    failed_set = _last_update_set(db.articles)
    assert failed_set["content_fetch_status"] == "failed"
    assert "内容为空" in failed_set["content_fetch_error"]


@pytest.mark.asyncio
async def test_enrich_marks_failed_for_timeout():
    """httpx.TimeoutException marks the article as failed."""
    import httpx
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(url="https://example.com/slow")
    db = FakeDatabase(article)
    ctx = {"db": db}

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(
            mock_client_class, side_effect=httpx.TimeoutException("timed out")
        )
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 0, "failed": 1, "skipped": 0}
    failed_set = _last_update_set(db.articles)
    assert failed_set["content_fetch_status"] == "failed"
    assert "抓取超时" in failed_set["content_fetch_error"]


@pytest.mark.asyncio
async def test_enrich_marks_failed_for_general_exception():
    """General exceptions mark the article as failed."""
    from agent.task_queue import enrich_web_search_articles

    article = _make_article(url="https://example.com/error")
    db = FakeDatabase(article)
    ctx = {"db": db}

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(
            mock_client_class, side_effect=ConnectionError("network down")
        )
        result = await enrich_web_search_articles(ctx, ["hash-1"])

    assert result == {"success": 0, "failed": 1, "skipped": 0}
    failed_set = _last_update_set(db.articles)
    assert failed_set["content_fetch_status"] == "failed"
    assert "network down" in failed_set["content_fetch_error"]


@pytest.mark.asyncio
async def test_enrich_skips_nonexistent_articles():
    """Articles not found in the database are skipped."""
    from agent.task_queue import enrich_web_search_articles

    db = FakeDatabase(document=None)
    ctx = {"db": db}

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class)
        result = await enrich_web_search_articles(ctx, ["missing-hash"])

    assert result == {"success": 0, "failed": 0, "skipped": 1}
    mock_client_class.assert_not_called()
    assert db.articles.updates == []


@pytest.mark.asyncio
async def test_enrich_processes_multiple_articles_with_mixed_results():
    """Multiple articles produce mixed success/failed/skipped counts."""
    from agent.task_queue import enrich_web_search_articles

    # We need a database that returns different articles for different url_hashes.
    articles_by_hash = {
        "hash-success": _make_article(url_hash="hash-success", url="https://example.com/ok"),
        "hash-unsafe": _make_article(url_hash="hash-unsafe", url="http://127.0.0.1/x"),
        "hash-missing": None,
    }

    class MultiFakeArticlesCollection:
        def __init__(self):
            self.updates: list[tuple[dict, dict]] = []

        async def find_one(self, query: dict, projection: dict | None = None):
            return deepcopy(articles_by_hash.get(query.get("url_hash")))

        async def update_one(self, query: dict, update: dict):
            self.updates.append((deepcopy(query), deepcopy(update)))
            return SimpleNamespace(matched_count=1, modified_count=1)

    class MultiFakeDatabase:
        def __init__(self):
            self.articles = MultiFakeArticlesCollection()

        def __getitem__(self, name: str):
            assert name == "articles"
            return self.articles

    db = MultiFakeDatabase()
    ctx = {"db": db}

    response = _mock_httpx_response(
        text="<html><body><p>OK</p></body></html>",
        content_type="text/html",
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        _setup_httpx_client_mock(mock_client_class, response=response)
        result = await enrich_web_search_articles(
            ctx, ["hash-success", "hash-unsafe", "hash-missing"]
        )

    assert result == {"success": 1, "failed": 1, "skipped": 1}
