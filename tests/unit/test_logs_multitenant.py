"""流水线日志多租户字段测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from api.logs import _log_to_db


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
        {"days": 1},
        "user-a",
    )

    document = collection.insert_one.await_args.args[0]
    assert document["user_id"] == "user-a"
    assert document["phase"] == "crawl"
    assert document["detail"] == {"days": 1}
    assert document["created_at"].tzinfo is not None
