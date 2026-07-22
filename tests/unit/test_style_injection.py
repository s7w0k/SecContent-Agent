"""任务 7.4 用户风格注入链路测试。"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from api.pipeline import _run_v2_single_workflow
from models.draft_review import DraftReview

ARTICLE_HASH = "d41d8cd98f00b204e9800998ecf8427e"


def _build_request(profile: dict | None, prompt: dict | None = None):
    articles = MagicMock()
    articles.find_one = AsyncMock(
        return_value={
            "url_hash": ARTICLE_HASH,
            "title": "MCP security event",
            "content_md": "article content",
            "source_type": "user_upload",
            "pipeline_status": "pending",
        },
    )
    articles.update_one = AsyncMock()

    user_profiles = MagicMock()
    user_profiles.find_one = AsyncMock(return_value=profile)

    user_drafts = MagicMock()
    user_drafts.update_one = AsyncMock()

    user_prompts = MagicMock()
    user_prompts.find_one = AsyncMock(return_value=prompt)

    collections = {
        "articles": articles,
        "user_profiles": user_profiles,
        "user_drafts": user_drafts,
        "user_prompts": user_prompts,
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
    assert draft_gen.generate.await_args.kwargs["system_prompt_template"] is None
    query = collections["user_drafts"].update_one.await_args.args[0]
    assert query["user_id"] == "first-time-user"


@pytest.mark.asyncio
async def test_run_v2_single_injects_custom_system_prompt(caplog):
    custom_prompt = (
        "自定义系统提示词\n{knowledge_context}\n{template_spec}\n{style_hints}\n"
        + "请生成准确、专业且适合公司传播的初稿。"
    )
    request, _, draft_gen = _build_request(
        None,
        {
            "user_id": "user-a",
            "prompt_key": "draft_system",
            "content": custom_prompt,
        },
    )

    with caplog.at_level("INFO"):
        result = await _run_v2_single_workflow(request.app, ARTICLE_HASH, user_id="user-a")

    assert result["ok"] is True
    assert draft_gen.generate.await_args.kwargs["system_prompt_template"] == custom_prompt
    assert "使用自定义提示词: user-a" in caplog.text


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


@pytest.mark.asyncio
async def test_run_v2_single_reviews_generated_drafts_before_persisting():
    request, collections, _draft_gen = _build_request(None)
    reviewer = MagicMock()
    reviewer.review = AsyncMock(
        return_value=DraftReview(
            status="completed",
            content_hash="a" * 64,
            summary="未发现需要修改的问题",
            issues=[],
            counts={"high": 0, "medium": 0, "low": 0},
            fact_check_available=True,
        )
    )
    request.app.state.draft_reviewer = reviewer

    result = await _run_v2_single_workflow(request.app, ARTICLE_HASH, user_id="user-a")

    reviewer.review.assert_awaited_once()
    saved_draft = collections["user_drafts"].update_one.await_args.args[1]["$set"]["drafts"][0]
    assert saved_draft["review"]["status"] == "completed"
    assert any(
        step == {"phase": "review", "review_count": 1, "review_failed_count": 0}
        for step in result["steps"]
    )
