"""SearchSessionService 单元测试 - 使用 AsyncMock 模拟数据库。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

USER_ID = "u-test-user"
OTHER_USER_ID = "u-other-user"


def _make_db():
    """创建模拟 AsyncIOMotorDatabase。"""
    db = MagicMock()
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    collection.find_one = AsyncMock()
    collection.update_one = AsyncMock()
    db.__getitem__ = MagicMock(return_value=collection)
    return db, collection


@pytest.mark.asyncio
async def test_create_session_generates_search_id_and_expires_at():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    service = SearchSessionService(db, ttl_minutes=30)

    session = await service.create_session(
        user_id=USER_ID,
        query={"q": "MCP 漏洞"},
        results=[{"result_id": "res_1", "title": "标题"}],
        warnings=[{"code": "PARTIAL", "message": "部分超时"}],
    )

    assert session["search_id"].startswith("srch_")
    assert session["user_id"] == USER_ID
    assert session["query"] == {"q": "MCP 漏洞"}
    assert session["result_count"] == 1
    assert session["warnings"][0]["code"] == "PARTIAL"
    # expires_at 应在 created_at 之后约 30 分钟
    delta = session["expires_at"] - session["created_at"]
    assert timedelta(minutes=29) <= delta <= timedelta(minutes=31)
    # insert_one 被调用一次
    collection.insert_one.assert_awaited_once()
    inserted = collection.insert_one.call_args[0][0]
    assert inserted["search_id"] == session["search_id"]


@pytest.mark.asyncio
async def test_create_session_defaults_warnings_to_empty():
    from services.backend.services.web_search_service import SearchSessionService

    db, _ = _make_db()
    service = SearchSessionService(db)

    session = await service.create_session(
        user_id=USER_ID,
        query={"q": "test"},
        results=[],
    )

    assert session["warnings"] == []
    assert session["result_count"] == 0


@pytest.mark.asyncio
async def test_get_session_returns_own_session():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    expected = {"search_id": "srch_1", "user_id": USER_ID, "results": []}
    collection.find_one.return_value = expected
    service = SearchSessionService(db)

    result = await service.get_session("srch_1", USER_ID)

    assert result == expected
    # 验证查询条件包含 user_id 和未过期过滤
    query = collection.find_one.call_args[0][0]
    assert query["search_id"] == "srch_1"
    assert query["user_id"] == USER_ID
    assert "expires_at" in query
    assert "$gt" in query["expires_at"]


@pytest.mark.asyncio
async def test_get_session_returns_none_for_other_user():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    collection.find_one.return_value = None
    service = SearchSessionService(db)

    result = await service.get_session("srch_1", OTHER_USER_ID)

    assert result is None
    query = collection.find_one.call_args[0][0]
    # 确保查询中包含 user_id 隔离条件
    assert query["user_id"] == OTHER_USER_ID


@pytest.mark.asyncio
async def test_get_session_returns_none_for_expired_session():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    collection.find_one.return_value = None
    service = SearchSessionService(db)

    result = await service.get_session("srch_expired", USER_ID)

    assert result is None
    query = collection.find_one.call_args[0][0]
    assert query["expires_at"] == {"$gt": query["expires_at"]["$gt"]}


@pytest.mark.asyncio
async def test_get_session_expired_filter_uses_future_time():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    collection.find_one.return_value = None
    service = SearchSessionService(db)
    now_before = datetime.now(UTC)

    await service.get_session("srch_1", USER_ID)

    query = collection.find_one.call_args[0][0]
    cutoff = query["expires_at"]["$gt"]
    now_after = datetime.now(UTC)
    # 过滤时间应在调用期间
    assert now_before <= cutoff <= now_after


@pytest.mark.asyncio
async def test_update_imported_status_marks_result():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    update_result = MagicMock()
    update_result.modified_count = 1
    collection.update_one.return_value = update_result
    service = SearchSessionService(db)

    ok = await service.update_imported_status(
        search_id="srch_1",
        user_id=USER_ID,
        result_id="res_abc",
        article_url_hash="d41d8cd98f00b204e9800998ecf8427e",
    )

    assert ok is True
    call_args = collection.update_one.call_args
    filter_doc = call_args[0][0]
    set_doc = call_args[0][1]
    assert filter_doc["search_id"] == "srch_1"
    assert filter_doc["user_id"] == USER_ID
    assert filter_doc["results.result_id"] == "res_abc"
    assert set_doc["$set"]["results.$.is_imported"] is True
    assert set_doc["$set"]["results.$.article_url_hash"] == "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.asyncio
async def test_update_imported_status_returns_false_when_not_found():
    from services.backend.services.web_search_service import SearchSessionService

    db, collection = _make_db()
    update_result = MagicMock()
    update_result.modified_count = 0
    collection.update_one.return_value = update_result
    service = SearchSessionService(db)

    ok = await service.update_imported_status(
        search_id="srch_missing",
        user_id=USER_ID,
        result_id="res_none",
        article_url_hash="hash",
    )

    assert ok is False


def test_generate_search_id_format():
    from services.backend.services.web_search_service import SearchSessionService

    db, _ = _make_db()
    service = SearchSessionService(db)
    search_id = service._generate_search_id()

    assert search_id.startswith("srch_")
    parts = search_id.split("_")
    assert len(parts) == 3
    assert len(parts[1]) == 8  # YYYYMMDD
    assert len(parts[2]) == 12  # 6 bytes hex


def test_generate_result_id_is_deterministic():
    from services.backend.services.web_search_service import SearchSessionService

    db, _ = _make_db()
    service = SearchSessionService(db)

    first = service._generate_result_id("srch_1", "https://example.com/a")
    second = service._generate_result_id("srch_1", "https://example.com/a")
    different = service._generate_result_id("srch_1", "https://example.com/b")

    assert first == second
    assert first != different
    assert first.startswith("res_")
