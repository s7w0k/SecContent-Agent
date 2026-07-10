"""chat_sessions 用户隔离测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.chat import _get_user_id, _save_chat_message

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def test_get_user_id_prefers_authenticated_user():
    request = SimpleNamespace(state=SimpleNamespace(user_id="user-a"))

    assert _get_user_id(request) == "user-a"


@pytest.mark.asyncio
async def test_save_chat_message_uses_user_compound_key():
    db = MagicMock()
    collection = MagicMock()
    collection.update_one = AsyncMock()
    db.__getitem__.return_value = collection

    await _save_chat_message(
        db,
        "user-a",
        ARTICLE_HASH,
        0,
        "user",
        "请优化标题",
    )

    query, update = collection.update_one.await_args.args
    assert query == {
        "user_id": "user-a",
        "article_url_hash": ARTICLE_HASH,
        "draft_index": 0,
    }
    assert update["$setOnInsert"]["user_id"] == "user-a"
    assert collection.update_one.await_args.kwargs == {"upsert": True}
