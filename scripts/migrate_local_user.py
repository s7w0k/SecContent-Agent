"""将阶段六的 local-user 数据迁移到一个已注册用户。

运行方式：
    python scripts/migrate_local_user.py <target_user_id>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any


def _resolve_backend_dir(script_file: str = __file__) -> str:
    """兼容源码仓库（services/backend）与容器（/app）目录结构。"""

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

logger = logging.getLogger("scripts.migrate_local_user")


async def migrate(target_user_id: str, db: Any | None = None) -> dict[str, int]:
    """迁移旧用户数据，并返回各集合修改数量。"""
    owns_connection = db is None
    if owns_connection:
        settings = get_settings()
        await MongoDB.connect(
            uri=settings.MONGODB_URI,
            db_name=settings.MONGODB_DB,
            max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
            min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
        )
        db = MongoDB.get_db()

    try:
        if await db["users"].find_one({"user_id": target_user_id}) is None:
            raise ValueError(f"Target user does not exist: {target_user_id}")

        results: dict[str, int] = {}
        for collection_name in ("feedbacks", "user_activities", "user_profiles"):
            result = await db[collection_name].update_many(
                {"user_id": "local-user"},
                {"$set": {"user_id": target_user_id}},
            )
            results[collection_name] = result.modified_count

        chat_result = await db["chat_sessions"].update_many(
            {
                "$or": [
                    {"user_id": "local-user"},
                    {"user_id": {"$exists": False}},
                ]
            },
            {"$set": {"user_id": target_user_id}},
        )
        results["chat_sessions"] = chat_result.modified_count

        log_result = await db["pipeline_logs"].update_many(
            {
                "$or": [
                    {"user_id": "local-user"},
                    {"user_id": {"$exists": False}},
                ]
            },
            {"$set": {"user_id": target_user_id}},
        )
        results["pipeline_logs"] = log_result.modified_count

        migrated_drafts = 0
        cursor = db["articles"].find(
            {"pr_drafts": {"$exists": True, "$ne": []}},
            {"url_hash": 1, "pr_drafts": 1, "draft_owner_id": 1},
        )
        async for article in cursor:
            article_hash = article.get("url_hash")
            drafts = article.get("pr_drafts") or []
            if not article_hash or not drafts:
                continue
            owner_id = article.get("draft_owner_id")
            if not owner_id or owner_id == "local-user":
                owner_id = target_user_id
            now = datetime.now(UTC)
            await db["user_drafts"].update_one(
                {"user_id": owner_id, "article_url_hash": article_hash},
                {
                    "$set": {"drafts": drafts, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            migrated_drafts += 1
        results["user_drafts"] = migrated_drafts
        return results
    finally:
        if owns_connection:
            await MongoDB.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target_user_id", help="已注册的目标用户 ID")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    try:
        results = asyncio.run(migrate(args.target_user_id))
    except (RuntimeError, ValueError) as exc:
        logger.error("Migration failed: %s", exc)
        raise SystemExit(1) from exc
    logger.info("Migration complete: %s", results)


if __name__ == "__main__":
    main()
