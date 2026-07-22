"""只读诊断热点排行所依赖的文章评分与时间字段。

运行方式：
    python scripts/diagnose_hot_ranking.py

脚本只执行 count/aggregate 查询，不会修改数据库。连接参数沿用后端配置。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import Any


def _resolve_backend_dir(script_file: str = __file__) -> str:
    project_dir = os.path.abspath(os.path.join(os.path.dirname(script_file), ".."))
    candidates = (project_dir, os.path.join(project_dir, "services", "backend"))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "config.py")):
            return candidate
    raise RuntimeError("Backend config.py was not found")


BACKEND_DIR = _resolve_backend_dir()
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from config import get_settings  # noqa: E402
from db.mongo import MongoDB  # noqa: E402


def _range_starts(now: datetime) -> dict[str, datetime | None]:
    utc_now = now.astimezone(UTC)
    utc8_now = utc_now.astimezone(timezone(timedelta(hours=8)))
    today = utc8_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return {
        "1d": today,
        "7d": utc_now - timedelta(days=7),
        "30d": utc_now - timedelta(days=30),
        "all": None,
    }


async def collect_diagnostics(db: Any, now: datetime | None = None) -> dict[str, Any]:
    """收集热点排行字段覆盖率；所有数据库操作均为只读。"""

    articles = db["articles"]
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    legacy_score_expr = {
        "$gt": [
            {
                "$add": [
                    {"$ifNull": ["$ai_relevance_score", 0]},
                    {"$ifNull": ["$reportability_score", 0]},
                ]
            },
            0,
        ]
    }

    total = await articles.count_documents({})
    pr_score_present = await articles.count_documents({"pr_total_score": {"$exists": True}})
    pr_score_positive = await articles.count_documents({"pr_total_score": {"$gt": 0}})
    legacy_score_positive = await articles.count_documents({"$expr": legacy_score_expr})
    legacy_only = await articles.count_documents(
        {
            "$and": [
                {"$expr": legacy_score_expr},
                {
                    "$or": [
                        {"pr_total_score": {"$exists": False}},
                        {"pr_total_score": {"$lte": 0}},
                    ]
                },
            ]
        }
    )

    added_at_types: dict[str, int] = {}
    async for row in articles.aggregate(
        [
            {"$group": {"_id": {"$type": "$added_at"}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
    ):
        added_at_types[str(row.get("_id", "unknown"))] = int(row.get("count", 0))

    category_values: dict[str, int] = {}
    async for row in articles.aggregate(
        [
            {"$group": {"_id": {"$ifNull": ["$category_v2", "<missing>"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
    ):
        category_values[str(row.get("_id", "<missing>"))] = int(row.get("count", 0))

    current_filter_counts: dict[str, int] = {}
    for range_name, since in _range_starts(observed_at).items():
        query: dict[str, Any] = {"pr_total_score": {"$gt": 0}}
        if since is not None:
            query["added_at"] = {"$gte": since}
        current_filter_counts[range_name] = await articles.count_documents(query)

    return {
        "observed_at": observed_at.isoformat(),
        "total_articles": total,
        "score_coverage": {
            "pr_total_score_present": pr_score_present,
            "pr_total_score_positive": pr_score_positive,
            "legacy_score_positive": legacy_score_positive,
            "legacy_positive_but_not_pr_total_positive": legacy_only,
        },
        "added_at_bson_types": added_at_types,
        "category_v2_values": category_values,
        "current_hot_filter_counts": current_filter_counts,
    }


async def _run() -> dict[str, Any]:
    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    try:
        return await collect_diagnostics(MongoDB.get_db())
    finally:
        await MongoDB.disconnect()


def main() -> None:
    try:
        report = asyncio.run(_run())
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "detail": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        raise SystemExit(1) from exc
    sys.stdout.write(
        json.dumps({"ok": True, "data": report}, ensure_ascii=False, indent=2, default=str) + "\n"
    )


if __name__ == "__main__":
    main()
