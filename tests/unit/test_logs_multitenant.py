"""流水线日志多租户字段测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.logs import _log_to_db, get_logs_by_date, list_dates


@pytest.mark.asyncio
async def test_log_to_db_persists_user_id_and_datetime():
    db = MagicMock()
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    db.__getitem__.return_value = collection

    await _log_to_db(
        db,
        "INFO",
        "crawl",
        "crawl started",
        "user-a",
        {"days": 1},
    )

    document = collection.insert_one.await_args.args[0]
    assert document["user_id"] == "user-a"
    assert document["phase"] == "crawl"
    assert document["detail"] == {"days": 1}
    assert document["created_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_log_dates_are_filtered_by_current_user():
    db = MagicMock()
    collection = MagicMock()
    collection.distinct = AsyncMock(return_value=["2026-07-10"])
    db.__getitem__.return_value = collection
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))

    result = await list_dates(request, user_id="user-a")

    assert result == {"dates": ["2026-07-10"]}
    collection.distinct.assert_awaited_once_with("date", {"user_id": "user-a"})


@pytest.mark.asyncio
async def test_logs_and_phases_are_filtered_by_current_user():
    class FakeCursor:
        def sort(self, _field, _direction):
            return self

        def limit(self, _limit):
            return self

        def __aiter__(self):
            async def iterate():
                yield {"_id": "log-1", "user_id": "user-a", "phase": "crawl"}

            return iterate()

    db = MagicMock()
    collection = MagicMock()
    collection.find = MagicMock(return_value=FakeCursor())
    collection.distinct = AsyncMock(return_value=["crawl"])
    db.__getitem__.return_value = collection
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))

    result = await get_logs_by_date(
        "2026-07-10",
        request,
        phase="crawl",
        limit=200,
        user_id="user-a",
    )

    expected_query = {"user_id": "user-a", "date": "2026-07-10", "phase": "crawl"}
    collection.find.assert_called_once_with(expected_query)
    collection.distinct.assert_awaited_once_with(
        "phase",
        {"user_id": "user-a", "date": "2026-07-10"},
    )
    assert {item["user_id"] for item in result["logs"]} == {"user-a"}
