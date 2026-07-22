"""任务 11.3：手动稿件重查 API 测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.draft_reviewer import compute_content_hash
from api.chat import router
from auth.deps import get_current_user
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from models.draft_review import DraftReview

URL_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _app(reviewer):
    app = FastAPI()
    app.include_router(router)

    async def current_user():
        return "user-a"

    app.dependency_overrides[get_current_user] = current_user
    article = {"url_hash": URL_HASH, "title": "原文", "content_md": "产品新增风险识别功能。"}
    draft = {"title": "稿件", "content_md": "产品新增风险识别功能。", "index": 1}
    articles = MagicMock()
    articles.find_one = AsyncMock(return_value=article)
    user_drafts = MagicMock()
    user_drafts.find_one = AsyncMock(
        return_value={"user_id": "user-a", "article_url_hash": URL_HASH, "drafts": [draft]}
    )
    user_drafts.update_one = AsyncMock()
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: {
        "articles": articles,
        "user_drafts": user_drafts,
    }.get(name, MagicMock())
    db.articles = articles
    db.user_drafts = user_drafts
    app.state.db = db
    app.state.draft_reviewer = reviewer
    return app


@pytest.mark.asyncio
async def test_manual_review_overwrites_current_review():
    reviewer = MagicMock()
    reviewer.review = AsyncMock(
        return_value=DraftReview(
            status="completed",
            content_hash=compute_content_hash("产品新增风险识别功能。"),
            summary="未发现需要修改的问题",
            issues=[],
            counts={"high": 0, "medium": 0, "low": 0},
            fact_check_available=True,
        )
    )
    app = _app(reviewer)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/articles/{URL_HASH}/drafts/0/review")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "completed"
    reviewer.review.assert_awaited_once()
    update = app.state.db.user_drafts.update_one.await_args.args[1]["$set"]
    assert update["drafts.0.review"]["status"] == "completed"


@pytest.mark.asyncio
async def test_manual_review_failure_is_saved_as_review_status():
    reviewer = MagicMock()
    reviewer.review = AsyncMock(side_effect=RuntimeError("service unavailable"))
    app = _app(reviewer)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/articles/{URL_HASH}/drafts/0/review")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "failed"
    update = app.state.db.user_drafts.update_one.await_args.args[1]["$set"]
    assert update["drafts.0.review"]["error"] == "service unavailable"


@pytest.mark.asyncio
async def test_manual_review_returns_404_for_missing_draft():
    reviewer = MagicMock()
    app = _app(reviewer)
    app.state.db.user_drafts.find_one = AsyncMock(return_value={"drafts": []})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/articles/{URL_HASH}/drafts/0/review")

    assert response.status_code == 404
