"""设置或撤销指定用户的开发者权限。

用法：
    python scripts/set_developer.py <user_id或username>
    python scripts/set_developer.py <user_id或username> --disable
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
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

logger = logging.getLogger("scripts.set_developer")


async def set_developer(
    identifier: str,
    *,
    enabled: bool = True,
    db: Any | None = None,
) -> dict[str, Any]:
    """按 user_id 或 username 设置权限，并返回被修改的用户概要。"""

    if not identifier.strip():
        raise ValueError("User identifier must not be empty")
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
        query = {"$or": [{"user_id": identifier}, {"username": identifier}]}
        user = await db["users"].find_one(query)
        if user is None:
            raise ValueError(f"User does not exist: {identifier}")
        await db["users"].update_one(
            {"user_id": user["user_id"]},
            {"$set": {"is_developer": enabled}},
        )
        return {
            "user_id": user["user_id"],
            "username": user["username"],
            "is_developer": enabled,
        }
    finally:
        if owns_connection:
            await MongoDB.disconnect()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifier", help="目标用户的 user_id 或 username")
    parser.add_argument("--disable", action="store_true", help="撤销开发者权限")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    try:
        result = asyncio.run(set_developer(args.identifier, enabled=not args.disable))
    except (RuntimeError, ValueError) as exc:
        logger.error("Developer permission update failed: %s", exc)
        raise SystemExit(1) from exc
    logger.info("Developer permission updated: %s", result)


if __name__ == "__main__":
    main()
