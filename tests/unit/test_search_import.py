"""Unit tests for article ingestion service and search import endpoint."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pymongo.errors import DuplicateKeyError

from services.backend.services.article_ingestion import ArticleIngestionService

# ═══════════════════════════════════════════════════════════════
# In-memory fake MongoDB collections
# ═══════════════════════════════════════════════════════════════


def _matches(document: dict, query: dict) -> bool:
    """Minimal MongoDB query matcher supporting top-level and $gt."""
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict):
            for op, val in expected.items():
                if op == "$gt" and not (actual is not None and actual > val):
                    return False
                if op == "$in" and actual not in val:
                    return False
        elif actual != expected:
            return False
    return True


class FakeCollection:
    """In-memory async MongoDB collection for testing."""

    def __init__(self, documents: list[dict] | None = None):
        self.documents = deepcopy(documents or [])

    async def find_one(self, query: dict, projection: dict | None = None):
        for item in self.documents:
            if _matches(item, query):
                result = deepcopy(item)
                if projection:
                    result = {k: v for k, v in result.items() if k in projection or k == "_id"}
                return result
        return None

    async def insert_one(self, document: dict):
        stored = deepcopy(document)
        stored.setdefault("_id", f"id-{len(self.documents) + 1}")
        self.documents.append(stored)
        return SimpleNamespace(inserted_id=stored["_id"])

    async def update_one(self, query: dict, update: dict):
        for item in self.documents:
            if _matches(item, query):
                item.update(deepcopy(update.get("$set", {})))
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


class FakeDatabase:
    """In-memory database with configurable collections."""

    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._collections: dict[str, FakeCollection] = {}
        for name, docs in (collections or {}).items():
            self._collections[name] = FakeCollection(docs)

    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self._collections:
            self._collections[name] = FakeCollection()
        return self._collections[name]


# ═══════════════════════════════════════════════════════════════
# ArticleIngestionService tests
# ═══════════════════════════════════════════════════════════════


class TestInsertOrGetExisting:
    """insert_or_get_existing tests."""

    @pytest.mark.asyncio
    async def test_new_article_returns_imported(self):
        db = FakeDatabase()
        svc = ArticleIngestionService(db)
        result = await svc.insert_or_get_existing(
            url="https://example.com/article1",
            title="Test Article",
            snippet="A snippet",
        )
        assert result["status"] == "imported"
        assert len(result["article_url_hash"]) == 32
        assert result["canonical_url"] == "https://example.com/article1"
        # Verify article was inserted
        assert len(db["articles"].documents) == 1
        article = db["articles"].documents[0]
        assert article["source_type"] == "web_search"
        assert article["pipeline_status"] == "pending_enrichment"
        assert article["content_fetch_status"] == "queued"

    @pytest.mark.asyncio
    async def test_existing_url_returns_duplicate(self):
        db = FakeDatabase()
        svc = ArticleIngestionService(db)
        # First insert
        await svc.insert_or_get_existing(
            url="https://example.com/article1",
            title="Test Article",
        )
        # Second insert - same URL
        result = await svc.insert_or_get_existing(
            url="https://example.com/article1",
            title="Test Article",
        )
        assert result["status"] == "duplicate"
        assert len(result["article_url_hash"]) == 32

    @pytest.mark.asyncio
    async def test_duplicate_key_error_returns_duplicate(self):
        db = FakeDatabase()
        svc = ArticleIngestionService(db)

        # Mock insert_one to raise DuplicateKeyError
        original_insert = db["articles"].insert_one

        call_count = 0

        async def mock_insert(doc):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise DuplicateKeyError("duplicate")
            return await original_insert(doc)

        db["articles"].insert_one = mock_insert
        # Make find_one return None so we try to insert
        db["articles"].find_one = AsyncMock(return_value=None)

        result = await svc.insert_or_get_existing(
            url="https://example.com/article1",
            title="Test Article",
        )
        assert result["status"] == "duplicate"


class TestCreateImportBatch:
    """create_import_batch tests."""

    @pytest.mark.asyncio
    async def test_new_key_returns_new_batch(self):
        db = FakeDatabase()
        svc = ArticleIngestionService(db)
        result = await svc.create_import_batch(
            user_id="user-1",
            search_id="srch_001",
            idempotency_key="idem-001",
            result_ids=["res_1", "res_2"],
        )
        assert result is not None
        assert result["status"] == "new"
        assert "batch_id" in result
        assert result["batch_id"].startswith("simp_")

    @pytest.mark.asyncio
    async def test_existing_terminal_returns_batch(self):
        existing_batch = {
            "batch_id": "simp_existing",
            "user_id": "user-1",
            "idempotency_key": "idem-001",
            "status": "completed",
            "summary": {"requested": 2, "imported": 2, "duplicate": 0, "failed": 0},
            "items": [{"result_id": "res_1", "status": "imported"}],
        }
        db = FakeDatabase({"search_import_batches": [existing_batch]})
        svc = ArticleIngestionService(db)
        result = await svc.create_import_batch(
            user_id="user-1",
            search_id="srch_001",
            idempotency_key="idem-001",
            result_ids=["res_1", "res_2"],
        )
        assert result is not None
        assert result["status"] == "completed"
        assert result["batch_id"] == "simp_existing"

    @pytest.mark.asyncio
    async def test_existing_processing_returns_batch(self):
        existing_batch = {
            "batch_id": "simp_existing",
            "user_id": "user-1",
            "idempotency_key": "idem-001",
            "status": "processing",
            "summary": {"requested": 2, "imported": 0, "duplicate": 0, "failed": 0},
            "items": [],
        }
        db = FakeDatabase({"search_import_batches": [existing_batch]})
        svc = ArticleIngestionService(db)
        result = await svc.create_import_batch(
            user_id="user-1",
            search_id="srch_001",
            idempotency_key="idem-001",
            result_ids=["res_1", "res_2"],
        )
        assert result is not None
        assert result["status"] == "processing"


class TestCompleteImportBatch:
    """complete_import_batch tests."""

    @pytest.mark.asyncio
    async def test_updates_batch_correctly(self):
        batch = {
            "batch_id": "simp_001",
            "user_id": "user-1",
            "status": "processing",
            "summary": {},
            "items": [],
        }
        db = FakeDatabase({"search_import_batches": [batch]})
        svc = ArticleIngestionService(db)

        summary = {"requested": 1, "imported": 1, "duplicate": 0, "failed": 0}
        items = [{"result_id": "res_1", "status": "imported"}]
        await svc.complete_import_batch("simp_001", "user-1", summary, items, "completed")

        updated = db["search_import_batches"].documents[0]
        assert updated["status"] == "completed"
        assert updated["summary"] == summary
        assert updated["items"] == items
        assert updated["completed_at"] is not None


# ═══════════════════════════════════════════════════════════════
# Import endpoint tests
# ═══════════════════════════════════════════════════════════════


def _make_session(search_id: str = "srch_001", user_id: str = "local-user"):
    """Create a fake search session with one result."""
    now = datetime.now(UTC)
    return {
        "search_id": search_id,
        "user_id": user_id,
        "expires_at": now + timedelta(minutes=30),
        "results": [
            {
                "result_id": "res_abc123",
                "title": "Test Article",
                "url": "https://example.com/article1",
                "snippet": "A test snippet",
                "published_at": None,
                "engines": ["google"],
                "category": "general",
                "is_imported": False,
                "article_url_hash": None,
            }
        ],
    }


def _make_app(db, settings=None):
    """Create a FastAPI app with web_search router and mocked dependencies."""
    from api.web_search import router
    from auth.deps import get_current_user

    async def override_current_user():
        return "local-user"

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.dependency_overrides[get_current_user] = override_current_user
    test_app.state.db = db
    test_app.state.searxng_client = MagicMock()

    if settings is None:
        settings = MagicMock()
        settings.WEB_SEARCH_ENABLED = True
        settings.WEB_SEARCH_IMPORT_BATCH_LIMIT = 20
        settings.WEB_SEARCH_SESSION_TTL_MINUTES = 30

    return test_app, settings


async def _request(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


class TestImportEndpoint:
    """POST /api/search/import endpoint tests."""

    @pytest.mark.asyncio
    async def test_disabled_returns_503(self):
        db = FakeDatabase()
        app, settings = _make_app(db)
        settings.WEB_SEARCH_ENABLED = False

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_1"]},
            )
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_nonexistent_session_returns_404(self):
        db = FakeDatabase()
        app, settings = _make_app(db)

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_nonexistent", "result_ids": ["res_1"]},
                headers={"Idempotency-Key": "idem-001"},
            )
        assert response.status_code == 404
        data = response.json()
        assert data["detail"]["code"] == "SEARCH_SESSION_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_successful_import_of_one_result(self):
        session = _make_session()
        db = FakeDatabase({"search_sessions": [session]})
        app, settings = _make_app(db)

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_abc123"]},
                headers={"Idempotency-Key": "idem-001"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["data"]["summary"]["imported"] == 1
        assert data["data"]["summary"]["failed"] == 0
        assert data["data"]["items"][0]["status"] == "imported"
        assert data["data"]["items"][0]["article_url_hash"] is not None

    @pytest.mark.asyncio
    async def test_duplicate_url_returns_duplicate_status(self):
        session = _make_session()
        # Pre-insert the article so it's a duplicate
        from utils.url_safety import canonicalize_url, compute_url_hash

        canonical = canonicalize_url("https://example.com/article1")
        url_hash = compute_url_hash(canonical)
        existing_article = {"url_hash": url_hash, "canonical_url": canonical}
        db = FakeDatabase({
            "search_sessions": [session],
            "articles": [existing_article],
        })
        app, settings = _make_app(db)

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_abc123"]},
                headers={"Idempotency-Key": "idem-002"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["summary"]["duplicate"] == 1
        assert data["data"]["items"][0]["status"] == "duplicate"

    @pytest.mark.asyncio
    async def test_invalid_url_returns_invalid_url_status(self):
        session = _make_session()
        # Override URL with unsafe one
        session["results"][0]["url"] = "http://127.0.0.1/path"
        db = FakeDatabase({"search_sessions": [session]})
        app, settings = _make_app(db)

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_abc123"]},
                headers={"Idempotency-Key": "idem-003"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["summary"]["failed"] == 1
        assert data["data"]["items"][0]["status"] == "invalid_url"

    @pytest.mark.asyncio
    async def test_result_id_not_in_session_returns_failed(self):
        session = _make_session()
        db = FakeDatabase({"search_sessions": [session]})
        app, settings = _make_app(db)

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_nonexistent"]},
                headers={"Idempotency-Key": "idem-004"},
            )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["summary"]["failed"] == 1
        assert data["data"]["items"][0]["status"] == "failed"
        assert "不在当前搜索会话中" in data["data"]["items"][0]["message"]

    @pytest.mark.asyncio
    async def test_idempotency_same_key_returns_same_response(self):
        session = _make_session()
        db = FakeDatabase({"search_sessions": [session]})
        app, settings = _make_app(db)

        with patch("api.web_search.get_settings", return_value=settings):
            # First request
            response1 = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_abc123"]},
                headers={"Idempotency-Key": "idem-same"},
            )
            # Second request with same key
            response2 = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_abc123"]},
                headers={"Idempotency-Key": "idem-same"},
            )

        assert response1.status_code == 200
        assert response2.status_code == 200
        data1 = response1.json()
        data2 = response2.json()
        # Same summary (imported=1)
        assert data1["data"]["summary"] == data2["data"]["summary"]

    @pytest.mark.asyncio
    async def test_batch_too_large_returns_413(self):
        db = FakeDatabase()
        app, settings = _make_app(db)
        settings.WEB_SEARCH_IMPORT_BATCH_LIMIT = 2

        with patch("api.web_search.get_settings", return_value=settings):
            response = await _request(
                app,
                "POST",
                "/api/search/import",
                json={"search_id": "srch_001", "result_ids": ["res_1", "res_2", "res_3"]},
                headers={"Idempotency-Key": "idem-005"},
            )
        assert response.status_code == 413
        data = response.json()
        assert data["detail"]["code"] == "IMPORT_BATCH_TOO_LARGE"
