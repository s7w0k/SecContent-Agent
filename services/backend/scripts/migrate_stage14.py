"""迁移脚本：将旧 draft_system 提示词迁移到新的 prompt key 体系。

可重复执行（幂等）：
1. 将 user_prompts 中 prompt_key=draft_system 的记录迁移到 draft_generation_business
2. 为旧草稿补充最小兼容 config_snapshot
3. 标记旧全局分数为 legacy

使用方式：
    docker exec -it pr-core-backend-1 python -m scripts.migrate_stage14
    或
    docker exec -it pr-core-backend-1 python -m scripts.migrate_stage14 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

# 添加项目根目录到 path
sys.path.insert(0, "/app")

logger = logging.getLogger("backend.migrate_stage14")


async def _get_db():
    """获取 MongoDB 数据库实例。"""
    from config import get_settings
    from db.mongo import MongoDB

    settings = get_settings()
    if not MongoDB._connected:
        await MongoDB.connect(
            uri=settings.MONGODB_URI,
            db_name=settings.MONGODB_DB,
        )
    return MongoDB.get_db()


async def migrate_draft_system(dry_run: bool = False) -> dict:
    """迁移 draft_system -> draft_generation_business。"""
    db = await _get_db()
    stats = {"found": 0, "migrated": 0, "skipped": 0}

    cursor = db["user_prompts"].find({"prompt_key": "draft_system"})
    async for doc in cursor:
        stats["found"] += 1
        user_id = doc["user_id"]

        # 检查是否已有 draft_generation_business
        existing = await db["user_prompts"].find_one({
            "user_id": user_id,
            "prompt_key": "draft_generation_business",
        })
        if existing is not None:
            stats["skipped"] += 1
            continue

        if dry_run:
            stats["migrated"] += 1
            continue

        # 迁移
        now = datetime.now(UTC)
        await db["user_prompts"].insert_one({
            "user_id": user_id,
            "prompt_key": "draft_generation_business",
            "content": doc["content"],
            "version": doc.get("version", 1),
            "base_default_version": 1,
            "content_hash": doc.get("content_hash", ""),
            "enabled": doc.get("enabled", True),
            "created_at": doc.get("created_at", now),
            "updated_at": now,
        })

        # 写入历史版本
        from uuid import uuid4
        await db["user_prompt_versions"].insert_one({
            "version_id": f"promptv-{uuid4()}",
            "user_id": user_id,
            "prompt_key": "draft_generation_business",
            "version": doc.get("version", 1),
            "content": doc["content"],
            "content_hash": doc.get("content_hash", ""),
            "base_default_version": 1,
            "change_type": "migrate_from_draft_system",
            "created_at": now,
        })

        stats["migrated"] += 1

    return stats


async def mark_legacy_scores(dry_run: bool = False) -> dict:
    """标记旧全局分数为 legacy。"""
    db = await _get_db()
    stats = {"found": 0, "marked": 0, "already_marked": 0}

    # 查找有 v2_scores 但没有 legacy_marked 的文章
    cursor = db["articles"].find(
        {"v2_scores": {"$exists": True, "$ne": None}, "legacy_score_marked": {"$ne": True}}
    )
    async for doc in cursor:
        stats["found"] += 1

        if dry_run:
            stats["marked"] += 1
            continue

        await db["articles"].update_one(
            {"_id": doc["_id"]},
            {"$set": {"legacy_score_marked": True, "legacy_marked_at": datetime.now(UTC)}},
        )
        stats["marked"] += 1

    return stats


async def patch_old_drafts(dry_run: bool = False) -> dict:
    """为旧草稿补充最小兼容 config_snapshot。"""
    db = await _get_db()
    stats = {"found": 0, "patched": 0, "skipped": 0}

    # 查找没有 config_snapshot 的草稿
    cursor = db["user_drafts"].find({"config_snapshot": {"$exists": False}})
    async for doc in cursor:
        stats["found"] += 1

        if dry_run:
            stats["patched"] += 1
            continue

        # 补充最小兼容快照
        minimal_snapshot = {
            "schema_version": 0,  # 0 表示兼容快照
            "product_relevance_enabled": True,
            "score_mode": "product_event",
            "product_target_mode": "auto",
            "selected_product_ids": [],
            "resolved_products": [],
            "prompt_refs": [],
            "knowledge_hash": "legacy",
            "config_fingerprint": "legacy",
            "force_generate": False,
            "legacy": True,
        }

        await db["user_drafts"].update_one(
            {"_id": doc["_id"]},
            {"$set": {"config_snapshot": minimal_snapshot}},
        )
        stats["patched"] += 1

    return stats


async def main(dry_run: bool = False):
    logger.info("=" * 60)
    logger.info("阶段十四迁移脚本")
    logger.info("模式: %s", "DRY RUN（预览）" if dry_run else "EXECUTE（执行）")
    logger.info("=" * 60)

    logger.info("[1/3] 迁移 draft_system -> draft_generation_business...")
    s1 = await migrate_draft_system(dry_run)
    logger.info("  找到: %d, 迁移: %d, 跳过: %d", s1["found"], s1["migrated"], s1["skipped"])

    logger.info("[2/3] 标记旧全局分数为 legacy...")
    s2 = await mark_legacy_scores(dry_run)
    logger.info("  找到: %d, 标记: %d, 已标记: %d", s2["found"], s2["marked"], s2["already_marked"])

    logger.info("[3/3] 为旧草稿补充兼容 config_snapshot...")
    s3 = await patch_old_drafts(dry_run)
    logger.info("  找到: %d, 补充: %d, 跳过: %d", s3["found"], s3["patched"], s3["skipped"])

    logger.info("=" * 60)
    logger.info("%s", "迁移完成" if not dry_run else "预览完成（未执行任何修改）")
    logger.info("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(description="阶段十四迁移脚本")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不执行修改")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run))
