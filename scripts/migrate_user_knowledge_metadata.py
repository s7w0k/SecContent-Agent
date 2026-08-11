"""migrate_user_knowledge_metadata - 用户知识条目 doc_type 元数据迁移（幂等、可回滚）。

阶段二 Step 3：历史 user_knowledge_entries 可能存在缺失/非法 doc_type。
模型层限定 doc_type ∈ {overview, market-brief, sales-brief, custom}；
本脚本只负责「统计 + 修复缺失/非法值」，不删除原字段、不做破坏性反向迁移。

用法：
    python scripts/migrate_user_knowledge_metadata.py --dry-run        # 只统计，不写库
    python scripts/migrate_user_knowledge_metadata.py                  # 修复缺失/非法 doc_type → custom
    python scripts/migrate_user_knowledge_metadata.py --batch-size 200 # 批次游标大小

特性：
  - --dry-run：仅输出分布统计与待修复清单
  - 批次游标：按 _id 游标分批，避免全量加载
  - 幂等：重复执行不会重复修复（缺失才写 custom）
  - 统计：enabled / product_scope / doc_type 分布
  - 不删除原字段（只 $set doc_type）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import Counter

from db.mongo import MongoDB

logger = logging.getLogger("migrate_user_knowledge_metadata")

VALID_DOC_TYPES = {"overview", "market-brief", "sales-brief", "custom"}
BATCH_SIZE = 200


async def _collect_stats(coll) -> dict:
    """统计 doc_type / enabled / product_scope 分布。"""
    stats: dict = {
        "total": 0,
        "doc_type": Counter(),
        "enabled": Counter(),
        "product_scope": Counter(),
        "missing_or_invalid_doc_type": 0,
        "samples_missing": [],
    }
    cursor = coll.find({}, {"_id": 1, "doc_type": 1, "enabled": 1, "product_scope": 1})
    async for doc in cursor:
        stats["total"] += 1
        doc_type = doc.get("doc_type")
        stats["enabled"][bool(doc.get("enabled", True))] += 1
        stats["product_scope"][doc.get("product_scope", "<missing>")] += 1
        if doc_type in VALID_DOC_TYPES:
            stats["doc_type"][doc_type] += 1
        else:
            stats["missing_or_invalid_doc_type"] += 1
            stats["doc_type"][f"<invalid:{doc_type!r}>"] += 1
            if len(stats["samples_missing"]) < 10:
                stats["samples_missing"].append(str(doc.get("_id")))
    return stats


async def _repair(coll, batch_size: int) -> int:
    """将缺失/非法 doc_type 修复为 custom（幂等），返回修复条数。"""
    fixed = 0
    last_id = None
    while True:
        query: dict = {
            "doc_type": {"$nin": list(VALID_DOC_TYPES)},
        }
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        batch = await coll.find(query).sort("_id", 1).limit(batch_size).to_list(batch_size)
        if not batch:
            break
        for doc in batch:
            result = await coll.update_one(
                {"_id": doc["_id"], "doc_type": {"$nin": list(VALID_DOC_TYPES)}},
                {"$set": {"doc_type": "custom"}},
            )
            if result.modified_count:
                fixed += 1
            last_id = doc["_id"]
        if len(batch) < batch_size:
            break
    return fixed


async def main() -> None:
    parser = argparse.ArgumentParser(description="用户知识 doc_type 元数据迁移")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="批次游标大小")
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get(
            "MONGO_URI", "mongodb://admin:pr_agent_2024@mongodb:27017"
        ),
        help="MongoDB URI",
    )
    parser.add_argument("--mongo-db", default=os.environ.get("MONGO_DB", "pr_agent"))
    args = parser.parse_args()

    await MongoDB.connect(uri=args.mongo_uri, db_name=args.mongo_db)
    coll = MongoDB.get_collection("user_knowledge_entries")

    stats = await _collect_stats(coll)
    print("=== user_knowledge_entries 统计 ===")
    print(f"total: {stats['total']}")
    print(f"doc_type 分布: {dict(stats['doc_type'])}")
    print(f"enabled 分布: {dict(stats['enabled'])}")
    print(f"product_scope 分布: {dict(stats['product_scope'])}")
    print(f"缺失/非法 doc_type: {stats['missing_or_invalid_doc_type']}")
    if stats["samples_missing"]:
        print(f"  样例 _id: {stats['samples_missing']}")

    if args.dry_run:
        print("\n[dry-run] 未执行任何写入。")
        await MongoDB.disconnect()
        return

    fixed = await _repair(coll, args.batch_size)
    print(f"\n已修复（缺失/非法 → custom）: {fixed} 条")
    # 幂等验证
    after = await _collect_stats(coll)
    print(f"修复后缺失/非法: {after['missing_or_invalid_doc_type']}")

    await MongoDB.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
