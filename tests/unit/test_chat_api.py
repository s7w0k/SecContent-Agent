"""任务 11.3：应用修订后重新审核测试。"""

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


@pytest.mark.asyncio
async def test_apply_revision_rechecks_and_overwrites_review():
    app = FastAPI()
    app.include_router(router)

    async def current_user():
        return "user-a"

    app.dependency_overrides[get_current_user] = current_user
    article = {"url_hash": URL_HASH, "title": "原文", "content_md": "原文内容"}
    draft = {
        "title": "稿件",
        "content_md": "旧内容",
        "index": 1,
        "review": {"status": "completed", "content_hash": compute_content_hash("旧内容")},
        "revisions": [
            {"revision_id": "rev-1", "content_md": "修订后内容", "applied": False}
        ],
    }
    articles = MagicMock()
    articles.find_one = AsyncMock(return_value=article)
    user_drafts = MagicMock()
    user_drafts.find_one = AsyncMock(return_value={"drafts": [draft]})
    user_drafts.update_one = AsyncMock()
    activities = MagicMock()
    activities.insert_one = AsyncMock()
    pipeline_logs = MagicMock()
    pipeline_logs.insert_one = AsyncMock()
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: {
        "articles": articles,
        "user_drafts": user_drafts,
        "user_activities": activities,
        "pipeline_logs": pipeline_logs,
    }.get(name, MagicMock())
    app.state.db = db

    reviewer = MagicMock()
    reviewer.review = AsyncMock(
        return_value=DraftReview(
            status="completed",
            content_hash=compute_content_hash("修订后内容"),
            summary="修订稿检查完成",
            issues=[],
            counts={"high": 0, "medium": 0, "low": 0},
            fact_check_available=True,
        )
    )
    app.state.draft_reviewer = reviewer

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/articles/{URL_HASH}/drafts/0/revisions/rev-1/apply"
        )

    assert response.status_code == 200
    assert response.json()["data"]["review"]["summary"] == "修订稿检查完成"
    reviewed_draft = reviewer.review.await_args.args[1]
    assert reviewed_draft["content_md"] == "修订后内容"
    saved_drafts = user_drafts.update_one.await_args.args[1]["$set"]["drafts"]
    assert saved_drafts[0]["review"]["content_hash"] == compute_content_hash("修订后内容")
