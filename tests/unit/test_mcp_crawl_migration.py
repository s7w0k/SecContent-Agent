"""C2 call-site convergence and attribution tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from clients.mcp_crawl import RequestContext


@pytest.mark.asyncio
async def test_overseas_single_fulltext_uses_shared_client():
    from api.overseas_crawl import _fetch_fulltext

    client = MagicMock()
    client.fetch_fulltext = AsyncMock(return_value="# article")
    context = RequestContext.create(
        request_id="request-a",
        trace_id="trace-a",
        initiator_user_id="user-a",
    )

    result = await _fetch_fulltext("https://example.com/a", client, context)

    assert result == "# article"
    client.fetch_fulltext.assert_awaited_once_with("https://example.com/a", context)


@pytest.mark.asyncio
async def test_v1_background_fulltext_propagates_attribution_and_updates_db():
    from agent.pipeline import _fetch_fulltext_background

    client = MagicMock()
    client.fetch_fulltext_batch = AsyncMock(return_value={"https://example.com/a": "content"})
    articles_collection = MagicMock()
    articles_collection.update_one = AsyncMock()
    db = {"articles": articles_collection}
    articles = [{"url_hash": "hash-a", "url": "https://example.com/a"}]

    await _fetch_fulltext_background(
        db,
        articles,
        "trace-a",
        client=client,
        user_id="user-a",
        request_id="request-a",
    )

    call = client.fetch_fulltext_batch.await_args
    assert call.args[0] == ["https://example.com/a"]
    context = call.args[1]
    assert context.request_id == "request-a"
    assert context.trace_id == "trace-a"
    assert context.initiator_user_id == "user-a"
    articles_collection.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_v2_fulltext_uses_injected_client():
    from agent.pipeline_v2 import _fetch_fulltext_batch

    client = MagicMock()
    client.fetch_fulltext_batch = AsyncMock(return_value={"https://example.com/a": "content"})
    context = RequestContext.create(
        request_id="request-a",
        trace_id="trace-a",
        initiator_user_id="user-a",
    )

    result = await _fetch_fulltext_batch(
        [{"url": "https://example.com/a"}],
        client,
        context,
    )

    assert result == {"https://example.com/a": "content"}
    client.fetch_fulltext_batch.assert_awaited_once_with(
        ["https://example.com/a"],
        context,
    )


def test_business_call_sites_have_no_embedded_crawler_url():
    root = Path(__file__).resolve().parents[2]
    business_files = [
        "services/backend/api/overseas_crawl.py",
        "services/backend/api/dashboard.py",
        "services/backend/agent/pipeline.py",
        "services/backend/agent/pipeline_v2.py",
    ]

    for relative_path in business_files:
        content = (root / relative_path).read_text(encoding="utf-8")
        assert "http://mcp-crawl:8101" not in content, relative_path
