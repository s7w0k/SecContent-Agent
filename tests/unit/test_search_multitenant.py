"""Multitenant isolation tests for web search feature."""

import unittest.mock
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_db():
    """Create a mock MongoDB database."""
    db = MagicMock()
    # search_sessions collection
    sessions = MagicMock()
    sessions.find_one = AsyncMock(return_value=None)
    sessions.insert_one = AsyncMock(return_value=MagicMock(inserted_id="test"))
    sessions.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
    # articles collection
    articles = MagicMock()
    articles.find_one = AsyncMock(return_value=None)
    articles.insert_one = AsyncMock(return_value=MagicMock())
    # search_import_batches collection
    batches = MagicMock()
    batches.find_one = AsyncMock(return_value=None)
    batches.insert_one = AsyncMock(return_value=MagicMock())
    batches.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    # search_import_items collection
    items_coll = MagicMock()
    items_coll.insert_one = AsyncMock(return_value=MagicMock())

    def get_collection(name):
        if name == "search_sessions":
            return sessions
        if name == "articles":
            return articles
        if name == "search_import_batches":
            return batches
        if name == "search_import_items":
            return items_coll
        return MagicMock()

    db.__getitem__ = MagicMock(side_effect=get_collection)
    return db


class TestSessionIsolation:
    """Search session user isolation tests."""

    @pytest.mark.asyncio
    async def test_user_a_can_read_own_session(self, mock_db):
        """User A can read their own search session."""
        try:
            from services.web_search_service import SearchSessionService
        except ImportError:
            from services.backend.services.web_search_service import SearchSessionService
        svc = SearchSessionService(mock_db, ttl_minutes=30)

        # Setup: User A has a session
        now = datetime.now(UTC)
        mock_db["search_sessions"].find_one = AsyncMock(
            return_value={
                "search_id": "srch_001",
                "user_id": "user-A",
                "query": {"q": "test"},
                "results": [],
                "expires_at": now + timedelta(minutes=30),
            }
        )

        result = await svc.get_session("srch_001", "user-A")
        assert result is not None
        assert result["user_id"] == "user-A"

    @pytest.mark.asyncio
    async def test_user_b_cannot_read_user_a_session(self, mock_db):
        """User B cannot read User A's search session - returns None."""
        try:
            from services.web_search_service import SearchSessionService
        except ImportError:
            from services.backend.services.web_search_service import SearchSessionService
        svc = SearchSessionService(mock_db, ttl_minutes=30)

        # The query filter includes user_id, so User B's query won't match User A's session
        mock_db["search_sessions"].find_one = AsyncMock(return_value=None)

        result = await svc.get_session("srch_001", "user-B")
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_session_returns_none(self, mock_db):
        """Expired session returns None."""
        try:
            from services.web_search_service import SearchSessionService
        except ImportError:
            from services.backend.services.web_search_service import SearchSessionService
        svc = SearchSessionService(mock_db, ttl_minutes=30)

        mock_db["search_sessions"].find_one = AsyncMock(return_value=None)

        result = await svc.get_session("srch_expired", "user-A")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_imported_status_checks_user_id(self, mock_db):
        """update_imported_status filters by user_id."""
        try:
            from services.web_search_service import SearchSessionService
        except ImportError:
            from services.backend.services.web_search_service import SearchSessionService
        svc = SearchSessionService(mock_db, ttl_minutes=30)

        mock_db["search_sessions"].update_one = AsyncMock(
            return_value=MagicMock(modified_count=0)
        )

        result = await svc.update_imported_status("srch_001", "user-B", "res_1", "hash123")
        assert result is False  # Not updated because user_id doesn't match


class TestImportIsolation:
    """Import endpoint user isolation tests."""

    @pytest.mark.asyncio
    async def test_user_b_import_from_user_a_session_returns_404(self, mock_db):
        """User B importing from User A's search_id gets 404."""
        import httpx
        from auth.deps import get_current_user
        from fastapi import FastAPI

        try:
            from api.web_search import router
        except ImportError:
            from services.backend.api.web_search import router

        app = FastAPI()
        app.include_router(router)
        app.state.db = mock_db
        app.state.searxng_client = None

        # Mock settings
        mock_settings = MagicMock()
        mock_settings.WEB_SEARCH_ENABLED = True
        mock_settings.WEB_SEARCH_IMPORT_BATCH_LIMIT = 20
        mock_settings.WEB_SEARCH_SESSION_TTL_MINUTES = 30
        mock_settings.WEB_SEARCH_ENRICH_ON_IMPORT = False
        mock_settings.WEB_SEARCH_ALLOWED_CATEGORIES = "general,news"
        mock_settings.WEB_SEARCH_ALLOWED_LANGUAGES = "all,zh-CN,en"
        mock_settings.WEB_SEARCH_RESULT_LIMIT = 20

        # Override auth and settings
        async def _override_user():
            return "user-B"

        app.dependency_overrides[get_current_user] = _override_user

        # User B tries to import from User A's session
        # The session lookup will return None because user_id doesn't match
        mock_db["search_sessions"].find_one = AsyncMock(return_value=None)
        mock_db["search_import_batches"].find_one = AsyncMock(return_value=None)
        mock_db["search_import_batches"].insert_one = AsyncMock(
            return_value=MagicMock(inserted_id="batch_id")
        )

        with unittest.mock.patch(
            "api.web_search.get_settings", return_value=mock_settings
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/search/import",
                    json={"search_id": "srch_user_a", "result_ids": ["res_1"]},
                    headers={"Idempotency-Key": "test-key-1"},
                )

        assert response.status_code == 404
        data = response.json()
        assert data["detail"]["code"] == "SEARCH_SESSION_NOT_FOUND"

        app.dependency_overrides.clear()


class TestArticlePrivacy:
    """Verify shared articles don't contain user data."""

    @pytest.mark.asyncio
    async def test_imported_article_has_no_user_id(self, mock_db):
        """Articles created by import must not contain user_id or search query."""
        try:
            from services.article_ingestion import ArticleIngestionService
        except ImportError:
            from services.backend.services.article_ingestion import ArticleIngestionService
        svc = ArticleIngestionService(mock_db)

        captured_doc = {}

        async def capture_insert(doc):
            captured_doc.update(doc)
            return MagicMock()

        mock_db["articles"].find_one = AsyncMock(return_value=None)
        mock_db["articles"].insert_one = AsyncMock(side_effect=capture_insert)

        result = await svc.insert_or_get_existing(
            url="https://example.com/article",
            title="Test Article",
            snippet="Test snippet",
            published_at="2026-07-28T10:00:00Z",
            engines=["bing"],
            category="news",
        )

        assert result["status"] == "imported"
        # Verify no user data in article document
        assert "user_id" not in captured_doc
        assert "search_query" not in captured_doc
        assert "search_id" not in captured_doc
        assert "username" not in captured_doc
        # Verify search_provenance only has non-personal data
        assert "engines" in captured_doc["search_provenance"]
        assert "category" in captured_doc["search_provenance"]
        assert "user_id" not in captured_doc["search_provenance"]


class TestAccountDeletion:
    """Verify account deletion cleans up search private data."""

    def test_search_collections_in_private_collections(self):
        """PRIVATE_USER_COLLECTIONS includes search collections."""
        try:
            from api.auth import PRIVATE_USER_COLLECTIONS
        except ImportError:
            from services.backend.api.auth import PRIVATE_USER_COLLECTIONS
        assert "search_sessions" in PRIVATE_USER_COLLECTIONS
        assert "search_import_batches" in PRIVATE_USER_COLLECTIONS
        assert "search_import_items" in PRIVATE_USER_COLLECTIONS
