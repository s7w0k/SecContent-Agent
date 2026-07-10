"""local-user 数据迁移脚本测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from scripts.migrate_local_user import migrate


def _database(target_exists: bool = True):
    db = MagicMock()
    collections = {}
    for name in (
        "users",
        "feedbacks",
        "user_activities",
        "user_profiles",
        "chat_sessions",
    ):
        collection = MagicMock()
        collection.find_one = AsyncMock(
            return_value={"user_id": "target-user"} if target_exists else None
        )
        collection.update_many = AsyncMock(return_value=SimpleNamespace(modified_count=1))
        collections[name] = collection
    db.__getitem__.side_effect = collections.__getitem__
    db.collections = collections
    return db


@pytest.mark.asyncio
async def test_migrate_updates_all_stage_six_user_data():
    db = _database()

    result = await migrate("target-user", db=db)

    assert result == {
        "feedbacks": 1,
        "user_activities": 1,
        "user_profiles": 1,
        "chat_sessions": 1,
    }
    expected_update = call(
        {"user_id": "local-user"},
        {"$set": {"user_id": "target-user"}},
    )
    for name in ("feedbacks", "user_activities", "user_profiles"):
        assert db.collections[name].update_many.await_args == expected_update
    db.collections["chat_sessions"].update_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_migrate_rejects_unknown_target_user():
    db = _database(target_exists=False)

    with pytest.raises(ValueError, match="Target user does not exist"):
        await migrate("missing-user", db=db)

    db.collections["feedbacks"].update_many.assert_not_awaited()
