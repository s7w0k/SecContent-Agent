"""chat_sessions 用户隔离测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from api.chat import _load_user_drafts, _save_chat_message

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


@pytest.mark.asyncio
async def test_load_user_drafts_filters_by_user():
    db = MagicMock()
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value={"drafts": [{"title": "A"}]})
    db.__getitem__.return_value = collection

    drafts = await _load_user_drafts(db, "user-a", ARTICLE_HASH)

    assert drafts[0]["title"] == "A"
    assert drafts[0]["template_key"] == "legacy:unknown"
    assert drafts[0]["template_source"] == "legacy"
    collection.find_one.assert_awaited_once_with(
        {"user_id": "user-a", "article_url_hash": ARTICLE_HASH}
    )


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
