"""local-user 数据迁移脚本测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from scripts.migrate_local_user import migrate


class AsyncCursor:
    def __init__(self, documents: list[dict]):
        self._documents = iter(documents)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._documents)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _database(target_exists: bool = True):
    db = MagicMock()
    collections = {}
    for name in (
        "users",
        "feedbacks",
        "user_activities",
        "user_profiles",
        "chat_sessions",
        "pipeline_logs",
        "articles",
        "user_drafts",
    ):
        collection = MagicMock()
        collection.find_one = AsyncMock(
            return_value={"user_id": "target-user"} if target_exists else None
        )
        collection.update_many = AsyncMock(return_value=SimpleNamespace(modified_count=1))
        collections[name] = collection
    collections["articles"].find.return_value = AsyncCursor(
        [
            {
                "url_hash": "a" * 32,
                "pr_drafts": [{"title": "legacy"}],
                "draft_owner_id": "local-user",
            },
            {
                "url_hash": "b" * 32,
                "pr_drafts": [{"title": "owned"}],
                "draft_owner_id": "existing-owner",
            },
        ]
    )
    collections["user_drafts"].update_one = AsyncMock(
        return_value=SimpleNamespace(modified_count=1)
    )
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
        "pipeline_logs": 1,
        "user_drafts": 2,
    }
    expected_update = call(
        {"user_id": "local-user"},
        {"$set": {"user_id": "target-user"}},
    )
    for name in ("feedbacks", "user_activities", "user_profiles"):
        assert db.collections[name].update_many.await_args == expected_update
    db.collections["chat_sessions"].update_many.assert_awaited_once()
    db.collections["pipeline_logs"].update_many.assert_awaited_once()
    assert db.collections["user_drafts"].update_one.await_count == 2
    first_query = db.collections["user_drafts"].update_one.await_args_list[0].args[0]
    second_query = db.collections["user_drafts"].update_one.await_args_list[1].args[0]
    assert first_query == {
        "user_id": "target-user",
        "article_url_hash": "a" * 32,
    }
    assert second_query == {
        "user_id": "existing-owner",
        "article_url_hash": "b" * 32,
    }
    assert all(
        call.kwargs["upsert"] is True
        for call in db.collections["user_drafts"].update_one.await_args_list
    )


@pytest.mark.asyncio
async def test_migrate_rejects_unknown_target_user():
    db = _database(target_exists=False)

    with pytest.raises(ValueError, match="Target user does not exist"):
        await migrate("missing-user", db=db)

    db.collections["feedbacks"].update_many.assert_not_awaited()
