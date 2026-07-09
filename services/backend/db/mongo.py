"""
MongoDB 连接管理器 — 基于 Motor (AsyncIO) 的单例连接池。

使用方式:
    from db.mongo import MongoDB
    await MongoDB.connect("mongodb://...", "pr_agent")
    db = MongoDB.get_db()
    await db.articles.find_one({})
    await MongoDB.disconnect()
"""

from __future__ import annotations

import logging

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

logger = logging.getLogger("backend.db.mongo")


class MongoDB:
    """MongoDB 连接管理器。

    特性:
      - 连接池管理（min/max pool size）
      - 启动时 ping 验证连通性
      - 优雅关闭
    """

    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None
    _db_name: str = "pr_agent"
    _connected: bool = False

    @classmethod
    async def connect(
        cls,
        uri: str,
        db_name: str = "pr_agent",
        max_pool_size: int = 20,
        min_pool_size: int = 2,
    ) -> None:
        """建立 MongoDB 连接。

        Args:
            uri: MongoDB 连接字符串
            db_name: 数据库名
            max_pool_size: 连接池最大值
            min_pool_size: 连接池最小值

        Raises:
            ConnectionFailure: 无法连接时抛出
        """
        if cls._connected:
            logger.warning("MongoDB already connected, skipping")
            return

        cls._db_name = db_name

        cls.client = AsyncIOMotorClient(
            uri,
            maxPoolSize=max_pool_size,
            minPoolSize=min_pool_size,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=30000,
            # 生产环境推荐
            retryWrites=True,
            retryReads=True,
        )

        # 验证连接
        try:
            await cls.client.admin.command("ping")
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            cls.client = None
            logger.error("MongoDB connection failed: %s", e)
            raise ConnectionFailure(f"Cannot connect to MongoDB at {uri}") from e

        cls.db = cls.client[db_name]
        cls._connected = True
        logger.info("MongoDB connected: db=%s, uri=%s", db_name, cls._mask_uri(uri))

    @classmethod
    async def disconnect(cls) -> None:
        """关闭 MongoDB 连接。"""
        if cls.client:
            cls.client.close()
            cls.client = None
            cls.db = None
            cls._connected = False
            logger.info("MongoDB disconnected")

    @classmethod
    def get_db(cls) -> AsyncIOMotorDatabase:
        """获取数据库实例。

        Raises:
            RuntimeError: 尚未连接时抛出
        """
        if cls.db is None or not cls._connected:
            raise RuntimeError(
                "MongoDB not initialized. Call MongoDB.connect() before using the database."
            )
        return cls.db

    @classmethod
    def get_collection(cls, name: str) -> AsyncIOMotorCollection:
        """获取指定集合。

        Args:
            name: 集合名 (articles / reports / knowledge_base)
        """
        return cls.get_db()[name]

    @classmethod
    async def ensure_indexes(cls) -> dict[str, list[str]]:
        """幂等创建阶段六所需的 MongoDB 索引。

        Returns:
            按集合名分组的索引名称。
        """
        index_specs = {
            "feedbacks": [
                IndexModel(
                    [("feedback_id", ASCENDING)],
                    unique=True,
                    name="idx_feedback_id",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("target_type", ASCENDING),
                        ("target_ref.article_url_hash", ASCENDING),
                    ],
                    name="idx_feedback_user_target_article",
                ),
                IndexModel(
                    [("created_at", DESCENDING)],
                    name="idx_feedback_created_at",
                ),
                IndexModel(
                    [
                        ("target_ref.article_url_hash", ASCENDING),
                        ("target_ref.draft_index", ASCENDING),
                    ],
                    name="idx_feedback_article_draft",
                ),
            ],
            "user_activities": [
                IndexModel(
                    [("activity_id", ASCENDING)],
                    unique=True,
                    name="idx_activity_id",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_activity_user_created",
                ),
                IndexModel(
                    [("action", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_activity_action_created",
                ),
                IndexModel(
                    [("target.article_url_hash", ASCENDING)],
                    name="idx_activity_article",
                ),
            ],
            "user_profiles": [
                IndexModel(
                    [("user_id", ASCENDING)],
                    unique=True,
                    name="idx_profile_user_id",
                ),
                IndexModel(
                    [("updated_at", DESCENDING)],
                    name="idx_profile_updated_at",
                ),
            ],
        }

        created: dict[str, list[str]] = {}
        for collection_name, indexes in index_specs.items():
            index_names = await cls.get_collection(collection_name).create_indexes(indexes)
            created[collection_name] = index_names
            logger.info(
                "MongoDB indexes ensured: collection=%s, indexes=%s",
                collection_name,
                ", ".join(index_names),
            )
        return created

    @classmethod
    def is_connected(cls) -> bool:
        """检查是否已连接。"""
        return cls._connected and cls.client is not None

    @classmethod
    async def health_check(cls) -> dict:
        """健康检查：返回连接和延迟信息。"""
        if not cls._connected or cls.client is None:
            return {"status": "disconnected", "latency_ms": None}

        try:
            start = __import__("time").monotonic()
            await cls.client.admin.command("ping")
            latency = (__import__("time").monotonic() - start) * 1000
            return {"status": "connected", "latency_ms": round(latency, 2)}
        except Exception as e:
            return {"status": "error", "error": str(e), "latency_ms": None}

    # ── 辅助 ──

    @staticmethod
    def _mask_uri(uri: str) -> str:
        """隐藏 URI 中的密码部分，用于日志输出"""
        import re
        return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", uri)
