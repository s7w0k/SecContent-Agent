"""Conditional routing tests for the production V2 pipeline graph."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


def _collection_with_documents(documents: list[dict]) -> MagicMock:
    collection = MagicMock()
    collection.find.return_value.to_list = AsyncMock(return_value=documents)
    collection.find_one = AsyncMock(return_value=documents[0] if documents else None)
    collection.update_one = AsyncMock()
    collection.count_documents = AsyncMock(return_value=len(documents))
    return collection


def test_route_decisions_are_bounded():
    from agent.pipeline_v2 import (
        route_after_crawl,
        route_after_quality_check,
        route_after_score,
    )

    assert route_after_crawl({"needs_enrich": True, "enriched": False}) == "enrich"
    assert route_after_crawl({"needs_enrich": True, "enriched": True}) == "classify_v2"
    assert route_after_score({"score_anomaly": True, "score_retried": False}) == "score_v2"
    assert route_after_score({"score_anomaly": True, "score_retried": True}) == "draft"
    assert route_after_quality_check({"needs_rewrite": [{"index": 1}]}) == "rewrite"
    assert route_after_quality_check({"needs_rewrite": []}) == "review"


@pytest.mark.asyncio
async def test_incomplete_article_triggers_enrichment():
    from agent.pipeline_v2 import create_state_v2, enrich_node

    article = {
        "_id": "article-1",
        "url": "https://example.com/news",
        "content_md": "short",
    }
    articles = _collection_with_documents([article])
    db = {"articles": articles}
    state = create_state_v2()
    state["needs_enrich"] = True

    with patch(
        "agent.pipeline_v2._fetch_fulltext_batch",
        new=AsyncMock(return_value={article["url"]: "x" * 300}),
    ):
        result = await enrich_node(state, {}, db)

    assert result["enriched"] is True
    assert result["needs_enrich"] is False
    assert result["enriched_count"] == 1
    articles.update_one.assert_awaited_once_with(
        {"_id": "article-1"},
        {"$set": {"content_md": "x" * 300}},
    )


@pytest.mark.asyncio
async def test_low_confidence_classification_is_marked():
    from agent.classifier_v2 import ClassifyResultV2
    from agent.pipeline_v2 import classify_v2_node, create_state_v2

    article = {"_id": "article-1", "url_hash": "hash-1"}
    articles = _collection_with_documents([article])
    classifier = MagicMock()
    classifier.classify_batch = AsyncMock(
        return_value=[ClassifyResultV2(category="爆点事件", confidence=59, reason="ambiguous")]
    )

    result = await classify_v2_node(create_state_v2(), classifier, {"articles": articles})

    assert result["low_confidence_count"] == 1
    assert result["low_confidence_articles"] == ["hash-1"]
    persisted = articles.update_one.await_args.args[1]["$set"]
    assert persisted["category_v2_low_confidence"] is True


@pytest.mark.asyncio
async def test_anomalous_score_is_retried_once():
    from agent.pipeline_v2 import create_state_v2, route_after_score, score_v2_node

    article = {"_id": "article-1", "is_pr_eligible": True}
    articles = _collection_with_documents([article])
    scorer = MagicMock()
    scorer.adjust_threshold = AsyncMock(
        return_value={
            "threshold": 80,
            "adjustment": 0,
            "directional_count": 0,
        }
    )
    scorer.score_batch = AsyncMock(
        side_effect=[
            [
                {
                    "product_relevance": 100,
                    "event_impact": 95,
                    "pr_total_score": 195,
                    "is_pr_candidate": True,
                    "_fallback": False,
                }
            ],
            [
                {
                    "product_relevance": 80,
                    "event_impact": 70,
                    "pr_total_score": 150,
                    "is_pr_candidate": True,
                    "_fallback": False,
                }
            ],
        ]
    )
    knowledge = MagicMock()
    knowledge.load = AsyncMock()
    state = create_state_v2()

    state = await score_v2_node(state, scorer, knowledge, {"articles": articles})
    assert state["score_anomaly"] is True
    assert route_after_score(state) == "score_v2"

    state = await score_v2_node(state, scorer, knowledge, {"articles": articles})
    assert state["score_retried"] is True
    assert state["score_anomaly"] is False
    assert route_after_score(state) == "draft"
    retry_query = articles.find.call_args_list[1].args[0]
    assert retry_query["$or"] == [
        {"pr_total_score": 0},
        {"pr_total_score": {"$gt": 190}},
    ]


@pytest.mark.asyncio
async def test_short_draft_is_reflection_rewritten():
    from agent.pipeline_v2 import create_state_v2, quality_check_node, rewrite_node

    user_drafts = _collection_with_documents(
        [
            {
                "article_url_hash": "hash-1",
                "drafts": [
                    {
                        "index": 1,
                        "title": "Draft title",
                        "content_md": "too short",
                    }
                ],
            }
        ]
    )
    article = {
        "url_hash": "hash-1",
        "title": "Source article",
        "product_relevance": 80,
        "event_impact": 70,
        "pr_total_score": 150,
    }
    articles = _collection_with_documents([article])
    profiles = _collection_with_documents([])
    db = {
        "user_drafts": user_drafts,
        "articles": articles,
        "user_profiles": profiles,
    }
    state = await quality_check_node(create_state_v2(user_id="user-a"), db)
    assert state["needs_rewrite"][0]["reason"] == "too_short"

    replacement = {
        "index": 1,
        "title": "Improved title",
        "content_md": "x" * 400,
    }
    draft_gen = MagicMock()
    draft_gen.generate = AsyncMock(
        return_value={"ok": True, "drafts": [replacement], "error": None}
    )
    knowledge = MagicMock()
    knowledge.load = AsyncMock()

    result = await rewrite_node(state, draft_gen, knowledge, db)

    assert result["rewritten_count"] == 1
    assert "反思重写要求" in draft_gen.generate.await_args.kwargs["style_hints"]
    user_drafts.update_one.assert_awaited_once_with(
        {"user_id": "user-a", "article_url_hash": "hash-1"},
        {"$set": {"drafts.0": replacement}},
    )
