"""Concurrency isolation tests for the stateless V2 scorer."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.wiki.provider import LegacyKnowledgeProvider


@pytest.mark.asyncio
async def test_shared_scorer_keeps_concurrent_thresholds_isolated():
    from agent.scorer_v2 import ScoringAgentV2

    llm = MagicMock()
    llm.temperature = None

    async def score_response(_messages):
        await asyncio.sleep(0.01)
        return AIMessage(
            content=json.dumps(
                {
                    "relevance": 30,
                    "event_impact": 25,
                    "reason": "fixed score",
                }
            )
        )

    llm.ainvoke = AsyncMock(side_effect=score_response)
    knowledge = MagicMock()
    knowledge.as_scoring_prompt.return_value = "knowledge"
    scorer = ScoringAgentV2(
        llm=llm, knowledge=knowledge, knowledge_provider=LegacyKnowledgeProvider()
    )
    article = {"title": "A", "source": "S", "category_v2": "爆点事件"}

    low_threshold, high_threshold = await asyncio.gather(
        scorer.score_single(article, threshold=50, threshold_adjustment=-30),
        scorer.score_single(article, threshold=90, threshold_adjustment=10),
    )

    assert low_threshold["pr_total_score"] == 55
    assert low_threshold["is_pr_candidate"] is True
    assert low_threshold["pr_threshold"] == 50
    assert low_threshold["threshold_adjustment"] == -30
    assert high_threshold["is_pr_candidate"] is False
    assert high_threshold["pr_threshold"] == 90
    assert high_threshold["threshold_adjustment"] == 10
    assert not hasattr(scorer, "pr_threshold")
    assert not hasattr(scorer, "threshold_adjustment")


@pytest.mark.asyncio
async def test_batch_propagates_explicit_threshold_metadata():
    from agent.scorer_v2 import ScoringAgentV2

    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "relevance": 40,
                    "event_impact": 40,
                    "reason": "batch score",
                }
            )
        )
    )
    knowledge = MagicMock()
    knowledge.as_scoring_prompt.return_value = "knowledge"
    scorer = ScoringAgentV2(
        llm=llm, knowledge=knowledge, knowledge_provider=LegacyKnowledgeProvider()
    )

    results = await scorer.score_batch(
        [{"title": "A"}, {"title": "B"}],
        threshold=86,
        threshold_adjustment=6,
    )

    assert [result["pr_threshold"] for result in results] == [86, 86]
    assert [result["threshold_adjustment"] for result in results] == [6, 6]
    assert all(result["is_pr_candidate"] is False for result in results)


@pytest.mark.asyncio
async def test_fallback_preserves_task_threshold_metadata():
    from agent.scorer_v2 import ScoringAgentV2

    llm = MagicMock()
    llm.temperature = None
    llm.ainvoke = AsyncMock(side_effect=RuntimeError("unavailable"))
    knowledge = MagicMock()
    knowledge.as_scoring_prompt.return_value = "knowledge"
    scorer = ScoringAgentV2(
        llm=llm, knowledge=knowledge, knowledge_provider=LegacyKnowledgeProvider()
    )

    result = await scorer.score_single(
        {"title": "A"},
        threshold=84,
        threshold_adjustment=4,
    )

    assert result["_fallback"] is True
    assert result["pr_threshold"] == 84
    assert result["threshold_adjustment"] == 4
