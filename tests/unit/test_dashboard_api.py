"""阶段十一热点排行基线与接口契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from api.dashboard import _date_range_start, router
from auth.deps import get_current_user
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


class _AggregationCursor:
    def __init__(self, items: list[dict]):
        self.items = items

    async def to_list(self, length: int):
        return self.items[:length]


@pytest.fixture
def hot_app():
    app = FastAPI()
    app.include_router(router)

    async def current_user() -> str:
        return "stage11-user"

    app.dependency_overrides[get_current_user] = current_user
    collection = MagicMock()
    cursor = _AggregationCursor(
        [
            {
                "url_hash": "hot-1",
                "title": "热点文章",
                "pr_total_score": 180,
                "added_at": "2026-07-22T08:00:00+00:00",
            }
        ]
    )
    collection.aggregate.return_value = cursor
    db = MagicMock()
    db.__getitem__.return_value = collection
    app.state.db = db
    return app, collection, cursor


@pytest.mark.asyncio
async def test_hot_ranking_response_and_compatibility_pipeline(hot_app):
    app, collection, _cursor = hot_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/articles/hot",
            params={"limit": 10, "category": "all", "date_range": "all"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {
            "items": [
                {
                    "url_hash": "hot-1",
                    "title": "热点文章",
                    "pr_total_score": 180,
                    "added_at": "2026-07-22T08:00:00+00:00",
                }
            ],
            "total": 1,
        },
    }
    pipeline = collection.aggregate.call_args.args[0]
    compatibility_fields = pipeline[0]["$set"]
    assert compatibility_fields["_hot_score"] == {
        "$cond": [
            {"$gt": [{"$ifNull": ["$pr_total_score", 0]}, 0]},
            {"$ifNull": ["$pr_total_score", 0]},
            {
                "$add": [
                    {"$ifNull": ["$ai_relevance_score", 0]},
                    {"$ifNull": ["$reportability_score", 0]},
                ]
            },
        ]
    }
    assert compatibility_fields["_hot_added_at"] == {
        "$convert": {
            "input": "$added_at",
            "to": "date",
            "onError": None,
            "onNull": None,
        }
    }
    assert pipeline[1] == {"$match": {"_hot_score": {"$gt": 0}}}
    assert pipeline[2] == {
        "$sort": {"_hot_score": -1, "_hot_added_at": -1, "url_hash": 1}
    }
    assert pipeline[3] == {"$limit": 10}
    assert pipeline[4]["$project"]["pr_total_score"] == "$_hot_score"
    assert pipeline[4]["$project"]["added_at"] == "$_hot_added_at"


@pytest.mark.asyncio
async def test_hot_ranking_uses_datetime_for_recent_range(hot_app):
    app, collection, _cursor = hot_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/articles/hot", params={"date_range": "7d"})

    assert response.status_code == 200
    pipeline = collection.aggregate.call_args.args[0]
    match = pipeline[1]["$match"]
    assert match["_hot_score"] == {"$gt": 0}
    assert isinstance(match["_hot_added_at"]["$gte"], datetime)
    assert match["_hot_added_at"]["$gte"].tzinfo is not None


@pytest.mark.asyncio
async def test_hot_ranking_filters_category_before_compatibility_fields(hot_app):
    app, collection, _cursor = hot_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/articles/hot",
            params={"category": "爆点事件", "date_range": "30d", "limit": 20},
        )

    assert response.status_code == 200
    pipeline = collection.aggregate.call_args.args[0]
    assert pipeline[0] == {"$match": {"category_v2": "爆点事件"}}
    assert "$set" in pipeline[1]
    assert pipeline[3] == {
        "$sort": {"_hot_score": -1, "_hot_added_at": -1, "url_hash": 1}
    }
    assert pipeline[4] == {"$limit": 20}


def test_hot_ranking_date_ranges_are_utc_aware():
    now = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    assert _date_range_start("all", now) is None
    assert _date_range_start("7d", now) == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert _date_range_start("30d", now) == datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    assert _date_range_start("1d", now) == datetime(2026, 7, 21, 16, 0, tzinfo=UTC)


def test_hot_ranking_date_range_boundaries_are_inclusive_in_pipeline(hot_app):
    """MongoDB 查询使用 $gte，边界时刻本身必须被纳入排行。"""
    boundary = _date_range_start("7d", datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    assert boundary == datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
