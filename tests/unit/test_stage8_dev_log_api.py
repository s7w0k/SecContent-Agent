"""阶段八任务 8.4：开发者日志查询 API 测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.dev_logs import get_stats, get_trace, list_dates, query_logs
from fastapi import HTTPException


class AsyncItems:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def __aiter__(self):
        async def iterate():
            for item in self.items:
                yield item

        return iterate()


def _request(collection) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db={"pipeline_logs": collection}))
    )


@pytest.mark.asyncio
async def test_query_logs_builds_cross_user_filters_and_paginates() -> None:
    created_at = datetime(2026, 7, 13, 1, tzinfo=UTC)
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(
        return_value=[{"_id": "mongo-1", "user_id": "user-b", "created_at": created_at}]
    )
    collection = MagicMock()
    collection.count_documents = AsyncMock(return_value=1)
    collection.find.return_value = cursor
    collection.distinct = AsyncMock(side_effect=[["draft", "crawl"], ["INFO", "ERROR"]])
    collection.aggregate.return_value = AsyncItems([{"_id": "user-b", "username": "bob"}])

    result = await query_logs(
        _request(collection),
        date="2026-07-13",
        user_id="user-b",
        phase="crawl,draft",
        level="INFO,ERROR",
        trace_id="trace-1",
        keyword="完成",
        page=2,
        page_size=20,
        _developer=("developer", {}),
    )

    query = collection.count_documents.await_args.args[0]
    assert query == {
        "date": "2026-07-13",
        "user_id": "user-b",
        "phase": {"$in": ["crawl", "draft"]},
        "level": {"$in": ["INFO", "ERROR"]},
        "trace_id": "trace-1",
        "message": {"$regex": "完成", "$options": "i"},
    }
    cursor.skip.assert_called_once_with(20)
    assert result["data"]["users"] == [{"user_id": "user-b", "username": "bob"}]
    assert result["data"]["logs"][0]["created_at"] == created_at.isoformat()


@pytest.mark.asyncio
async def test_list_dates_is_global_and_descending() -> None:
    collection = MagicMock()
    collection.distinct = AsyncMock(return_value=["2026-07-12", "2026-07-13"])
    result = await list_dates(_request(collection), _developer=("developer", {}))
    assert result["data"]["dates"] == ["2026-07-13", "2026-07-12"]
    collection.distinct.assert_awaited_once_with("date")


@pytest.mark.asyncio
async def test_trace_returns_summary_and_complete_ordered_events() -> None:
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(
        return_value=[
            {"user_id": "user-a", "username": "alice", "phase": "crawl", "level": "INFO"},
            {"user_id": "user-a", "phase": "draft", "level": "ERROR", "duration_ms": 80},
        ]
    )
    collection = MagicMock()
    collection.find.return_value = cursor
    result = await get_trace("trace-1", _request(collection), _developer=("developer", {}))
    assert result["data"]["total_duration_ms"] == 80
    assert result["data"]["phase_count"] == 2
    assert result["data"]["has_error"] is True
    cursor.sort.assert_called_once_with("created_at", 1)


@pytest.mark.asyncio
async def test_trace_not_found_uses_contract_error() -> None:
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[])
    collection = MagicMock()
    collection.find.return_value = cursor
    with pytest.raises(HTTPException) as caught:
        await get_trace("missing", _request(collection), _developer=("developer", {}))
    assert caught.value.status_code == 404
    assert caught.value.detail["code"] == "TRACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_stats_aggregates_levels_users_errors_and_duration() -> None:
    cursor = MagicMock()
    cursor.to_list = AsyncMock(
        return_value=[
            {
                "user_id": "user-a",
                "username": "alice",
                "phase": "crawl",
                "level": "INFO",
                "duration_ms": 100,
            },
            {
                "user_id": "user-a",
                "username": "alice",
                "phase": "crawl",
                "level": "ERROR",
                "duration_ms": 200,
            },
        ]
    )
    collection = MagicMock()
    collection.find.return_value = cursor
    result = await get_stats(_request(collection), date="2026-07-13", _developer=("developer", {}))
    data = result["data"]
    assert data["by_level"] == {"INFO": 1, "ERROR": 1}
    assert data["by_user"] == [{"user_id": "user-a", "username": "alice", "count": 2}]
    assert data["error_count"] == 1
    assert data["avg_duration_ms"] == {"crawl": 150}
