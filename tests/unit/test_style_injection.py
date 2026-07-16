"""任务 7.4 用户风格注入链路测试。"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from api.pipeline import _run_v2_single_workflow

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _build_request(profile: dict | None):
    articles = MagicMock()
    articles.find_one = AsyncMock(
        return_value={
            "url_hash": ARTICLE_HASH,
            "title": "MCP security event",
            "content_md": "article content",
        },
    )
    articles.update_one = AsyncMock()

    user_profiles = MagicMock()
    user_profiles.find_one = AsyncMock(return_value=profile)

    user_drafts = MagicMock()
    user_drafts.update_one = AsyncMock()

    collections = {
        "articles": articles,
        "user_profiles": user_profiles,
        "user_drafts": user_drafts,
    }
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: collections[name]

    classifier = MagicMock()
    classifier.classify_single = AsyncMock(
        return_value=SimpleNamespace(
            category="热点事件",
            confidence=0.95,
            reason="high relevance",
            is_fallback=False,
            is_pr_eligible=True,
        ),
    )
    scorer = MagicMock()
    scorer.score_single = AsyncMock(
        return_value={
            "product_relevance": 90,
            "event_impact": 80,
            "pr_total_score": 170,
            "score_reason": "candidate",
            "is_pr_candidate": True,
        },
    )
    draft_gen = MagicMock()
    draft_gen.generate = AsyncMock(
        return_value={
            "ok": True,
            "drafts": [
                {
                    "template": "爆点A",
                    "perspective": "市场传播视角",
                    "content_md": "# draft",
                },
            ],
        },
    )
    state = SimpleNamespace(
        db=db,
        classifier_v2=classifier,
        scorer_v2=scorer,
        draft_gen=draft_gen,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return request, collections, draft_gen


@pytest.mark.asyncio
async def test_run_v2_single_injects_current_user_style_and_saves_drafts(caplog):
    request, collections, draft_gen = _build_request(
        {
            "user_id": "user-a",
            "version": 2,
            "style_hints": {
                "preferred_templates": ["爆点A"],
                "preferred_tone": "executive",
            },
        },
    )

    with caplog.at_level("INFO"):
        result = await _run_v2_single_workflow(request.app, ARTICLE_HASH, user_id="user-a")

    assert result["ok"] is True
    collections["user_profiles"].find_one.assert_awaited_once_with({"user_id": "user-a"})
    style_hints = draft_gen.generate.await_args.kwargs["style_hints"]
    assert "爆点A" in style_hints
    assert "executive" in style_hints
    collections["user_drafts"].update_one.assert_awaited_once()
    query = collections["user_drafts"].update_one.await_args.args[0]
    assert query == {"user_id": "user-a", "article_url_hash": ARTICLE_HASH}
    assert collections["user_drafts"].update_one.await_args.kwargs["upsert"] is True
    assert "style_hints injected=True user_id=user-a" in caplog.text


@pytest.mark.asyncio
async def test_run_v2_single_uses_default_prompt_without_profile():
    request, collections, draft_gen = _build_request(None)

    result = await _run_v2_single_workflow(
        request.app,
        ARTICLE_HASH,
        user_id="first-time-user",
    )

    assert result["ok"] is True
    assert draft_gen.generate.await_args.kwargs["style_hints"] is None
    query = collections["user_drafts"].update_one.await_args.args[0]
    assert query["user_id"] == "first-time-user"


@pytest.mark.asyncio
async def test_run_v2_single_resolves_templates_for_current_user():
    request, _, draft_gen = _build_request(None)
    template_a = SimpleNamespace(
        template_key="breaking_a",
        template_id="tpl-user-a",
        version=4,
        source="user",
    )
    template_b = SimpleNamespace(
        template_key="breaking_b",
        template_id="system:breaking_b",
        version=1,
        source="system",
    )
    repository = MagicMock()
    repository.resolve = AsyncMock(return_value=[template_a, template_b])
    request.app.state.template_repository = repository

    await _run_v2_single_workflow(request.app, ARTICLE_HASH, user_id="user-a")

    repository.resolve.assert_awaited_once_with("user-a", "热点事件")
    assert draft_gen.generate.await_args.kwargs["templates"] == [template_a, template_b]
