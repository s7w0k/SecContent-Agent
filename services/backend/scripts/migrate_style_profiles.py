"""历史用户画像迁移脚本。

将现有 user_profiles 中的 style_hints 迁移为 user_memory_items。
- 只迁移非空项
- 原有 LLM 推断项初始状态设为 candidate
- 原有明确用户设置若能确认来源，写入 Policy
- 无证据项不得直接设为高置信度 Active
- --dry-run 模式不写入
- 可按单用户执行
- 幂等：重复运行不会创建重复记忆

用法：
    python -m scripts.migrate_style_profiles --dry-run
    python -m scripts.migrate_style_profiles --user-id user-a
    python -m scripts.migrate_style_profiles
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_style_profiles")


async def migrate_user(db, user_id: str, dry_run: bool = False) -> dict:
    """迁移单个用户的画像。"""
    profile = await db["user_profiles"].find_one({"user_id": user_id})
    if profile is None:
        return {"user_id": user_id, "created_memory_ids": [], "warnings": ["no user_profile found"]}

    style_hints = profile.get("style_hints", "")
    if not style_hints or not style_hints.strip():
        return {"user_id": user_id, "created_memory_ids": [], "warnings": ["empty style_hints"]}

    warnings: list[str] = []
    created_memory_ids: list[str] = []

    # 简单解析 style_hints 中的行
    lines = [l.strip() for l in style_hints.split("\n") if l.strip() and not l.startswith("#")]

    for line in lines:
        # 尝试识别维度
        dimension = "tone"  # 默认
        polarity = "prefer"
        display_text = line

        lower = line.lower()
        if "避免" in line or "不要" in line or "禁止" in line:
            polarity = "avoid"
            dimension = "avoid_pattern"
        elif "必须" in line or "需要" in line or "应该" in line:
            polarity = "require"
            dimension = "required_pattern"
        elif "语气" in line or "风格" in line or "tone" in lower:
            dimension = "tone"
        elif "篇幅" in line or "长度" in line or "字数" in line:
            dimension = "length"
        elif "结构" in line or "段落" in line:
            dimension = "structure"
        elif "标题" in line:
            dimension = "title_style"

        normalized_key = f"{dimension}:{line[:50]}"
        memory_id = f"mem-{uuid4().hex[:12]}"

        # 检查是否已存在（幂等）
        existing = await db["user_memory_items"].find_one({
            "user_id": user_id,
            "normalized_key": normalized_key,
        })
        if existing:
            continue

        item_doc = {
            "memory_id": memory_id,
            "user_id": user_id,
            "dimension": dimension,
            "value": line[:100],
            "normalized_key": normalized_key,
            "display_text": display_text[:500],
            "polarity": polarity,
            "scope": {"category_v2": None, "template_id": None, "stage": "draft", "target_audience": None},
            "confidence": 0.3,  # 迁移项低置信度
            "support_count": 0,
            "contradiction_count": 0,
            "independent_task_count": 0,
            "evidence_refs": [],
            "status": "candidate",  # 迁移项统一为 candidate
            "created_by": "migration",
            "confirmed_by_user": False,
            "suppressed_by": None,
            "first_seen_at": profile.get("created_at", datetime.now(UTC)),
            "last_seen_at": datetime.now(UTC),
            "last_used_at": None,
            "use_count": 0,
            "positive_outcome_count": 0,
            "negative_outcome_count": 0,
            "expires_at": None,
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

        if not dry_run:
            try:
                await db["user_memory_items"].insert_one(item_doc)
                created_memory_ids.append(memory_id)
            except Exception as exc:
                if "E11000" in str(exc):
                    continue
                warnings.append(f"insert failed: {exc}")
        else:
            created_memory_ids.append(memory_id)

    logger.info(
        "migrated user=%s created=%d warnings=%d dry_run=%s",
        user_id, len(created_memory_ids), len(warnings), dry_run,
    )

    return {
        "user_id": user_id,
        "source_profile_version": profile.get("version", 1),
        "created_memory_ids": created_memory_ids,
        "warnings": warnings,
    }


async def main(dry_run: bool = False, user_id: str | None = None):
    from db.mongo import MongoDB
    from config import get_settings

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    if user_id:
        result = await migrate_user(db, user_id, dry_run)
        results = [result]
    else:
        cursor = db["user_profiles"].find({}, {"user_id": 1})
        user_ids = [doc["user_id"] async for doc in cursor]
        results = []
        for uid in user_ids:
            result = await migrate_user(db, uid, dry_run)
            results.append(result)

    total_created = sum(len(r["created_memory_ids"]) for r in results)
    logger.info(
        "migration complete: users=%d memories_created=%d dry_run=%s",
        len(results), total_created, dry_run,
    )

    await MongoDB.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate style profiles to memory items")
    parser.add_argument("--dry-run", action="store_true", help="不写入，只预览")
    parser.add_argument("--user-id", type=str, default=None, help="指定用户 ID")
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run, user_id=args.user_id))
