"""阶段十六 16.3：文章去重迁移脚本。

检查并清理 articles 集合中重复的 url_hash 文档，
为创建唯一索引做准备。

用法：
    # 预览（不修改数据）
    python -m scripts.dedupe_articles --dry-run

    # 执行迁移
    python -m scripts.dedupe_articles --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

logger = logging.getLogger("scripts.dedupe_articles")


async def find_duplicates(db) -> list[dict[str, Any]]:
    """查找重复 url_hash 分组。

    Returns:
        每组包含 _id (url_hash), count, docs (文档列表)
    """
    pipeline = [
        {
            "$group": {
                "_id": "$url_hash",
                "count": {"$sum": 1},
                "docs": {
                    "$push": {
                        "_id": "$_id",
                        "title": "$title",
                        "content_md": {"$ifNull": ["$content_md", ""]},
                        "category_v2": {"$ifNull": ["$category_v2", None]},
                        "pr_total_score": {"$ifNull": ["$pr_total_score", None]},
                        "report_id": {"$ifNull": ["$report_id", None]},
                        "added_at": {"$ifNull": ["$added_at", None]},
                        "source": {"$ifNull": ["$source", ""]},
                    }
                },
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    return await db["articles"].aggregate(pipeline).to_list(length=1000)


def select_primary(doc: dict[str, Any]) -> bool:
    """判断文档是否适合作为主文档。

    优先级：
    1. content_md 非空
    2. 已完成分类 (category_v2 存在)
    3. 已完成打分 (pr_total_score 存在)
    4. 已关联报道 (report_id 存在)
    5. added_at 更新
    """
    score = 0
    if doc.get("content_md"):
        score += 100
    if doc.get("category_v2"):
        score += 10
    if doc.get("pr_total_score") is not None:
        score += 10
    if doc.get("report_id"):
        score += 10
    return score  # 返回分数，高的为主文档


async def merge_and_dedupe(db, dry_run: bool = True) -> dict[str, int]:
    """执行去重。

    Returns:
        统计结果
    """
    dup_groups = await find_duplicates(db)

    if not dup_groups:
        logger.info("未发现重复 url_hash 文档")
        return {"dup_groups": 0, "deleted": 0, "merged": 0}

    logger.info("发现 %d 组重复 url_hash", len(dup_groups))

    total_deleted = 0
    total_merged = 0

    for group in dup_groups:
        url_hash = group["_id"]
        docs = group["docs"]
        logger.info("  url_hash=%s count=%d", url_hash, len(docs))

        # 选择主文档
        scored = [(select_primary(d), d) for d in docs]
        scored.sort(key=lambda x: x[0], reverse=True)
        primary = scored[0][1]
        redundant = [d for _, d in scored[1:]]

        # 合并缺失字段到主文档
        merge_fields: dict[str, Any] = {}
        for doc in redundant:
            for field in (
                "content_md",
                "summary_cn",
                "category_v2",
                "pr_total_score",
                "report_id",
                "source",
            ):
                if not primary.get(field) and doc.get(field):
                    merge_fields[field] = doc[field]

        if merge_fields and not dry_run:
            await db["articles"].update_one(
                {"_id": primary["_id"]},
                {"$set": merge_fields},
            )
            total_merged += 1
            logger.info("  合并字段到主文档: %s", list(merge_fields.keys()))

        # 删除冗余文档
        redundant_ids = [d["_id"] for d in redundant]
        if not dry_run:
            result = await db["articles"].delete_many({"_id": {"$in": redundant_ids}})
            total_deleted += result.deleted_count
            logger.info("  删除 %d 个冗余文档", result.deleted_count)
        else:
            total_deleted += len(redundant_ids)
            logger.info("  [dry-run] 将删除 %d 个冗余文档", len(redundant_ids))

    return {
        "dup_groups": len(dup_groups),
        "deleted": total_deleted,
        "merged": total_merged,
    }


async def main(dry_run: bool = True) -> None:
    """主入口。"""
    from config import get_settings
    from db.mongo import MongoDB

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    # 检查重复
    await merge_and_dedupe(db, dry_run=dry_run)

    if dry_run:
        pass
    else:
        pass

    await MongoDB.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="文章去重迁移")
    parser.add_argument("--dry-run", action="store_true", default=True, help="预览模式（默认）")
    parser.add_argument("--apply", action="store_true", default=False, help="执行迁移")
    args = parser.parse_args()

    asyncio.run(main(dry_run=not args.apply))
