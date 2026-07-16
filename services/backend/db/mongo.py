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
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.errors import ConnectionFailure, OperationFailure, ServerSelectionTimeoutError

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
        """幂等创建反馈、画像与用户认证所需的 MongoDB 索引。

        Returns:
            按集合名分组的索引名称。
        """
        index_specs = {
            "users": [
                IndexModel(
                    [("user_id", ASCENDING)],
                    unique=True,
                    name="idx_user_id",
                ),
                IndexModel(
                    [("username", ASCENDING)],
                    unique=True,
                    name="idx_user_username",
                ),
                IndexModel(
                    [("email", ASCENDING)],
                    sparse=True,
                    name="idx_user_email",
                ),
            ],
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
            "chat_sessions": [
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("article_url_hash", ASCENDING),
                        ("draft_index", ASCENDING),
                    ],
                    unique=True,
                    name="idx_chat_user_article_draft",
                ),
            ],
            "user_drafts": [
                IndexModel(
                    [("user_id", ASCENDING), ("article_url_hash", ASCENDING)],
                    unique=True,
                    name="idx_user_draft_user_article",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("updated_at", DESCENDING)],
                    name="idx_user_draft_user_updated",
                ),
            ],
            "user_pr_templates": [
                IndexModel(
                    [("user_id", ASCENDING), ("template_key", ASCENDING)],
                    unique=True,
                    name="uq_user_template_key",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("category_v2", ASCENDING),
                        ("slot", ASCENDING),
                    ],
                    unique=True,
                    name="uq_user_category_slot",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("updated_at", DESCENDING)],
                    name="idx_user_template_updated",
                ),
            ],
            "user_pr_template_versions": [
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("template_id", ASCENDING),
                        ("version", DESCENDING),
                    ],
                    unique=True,
                    name="uq_user_template_version",
                ),
            ],
            "pipeline_locks": [
                IndexModel(
                    [("lock_key", ASCENDING)],
                    unique=True,
                    name="idx_pipeline_lock_key",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_pipeline_lock_expires",
                ),
            ],
            "pipeline_tasks": [
                IndexModel(
                    [("task_id", ASCENDING)],
                    unique=True,
                    name="idx_pipeline_task_id",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_pipeline_task_user_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_pipeline_task_expires",
                ),
                IndexModel(
                    [("thread_id", ASCENDING)],
                    sparse=True,
                    name="idx_pipeline_task_thread_id",
                ),
            ],
            "pipeline_logs": [
                IndexModel(
                    [("user_id", ASCENDING), ("date", DESCENDING)],
                    name="idx_pipeline_log_user_date",
                ),
                IndexModel(
                    [("trace_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_pipeline_log_trace_created",
                ),
                IndexModel(
                    [("phase", ASCENDING), ("date", DESCENDING)],
                    name="idx_pipeline_log_phase_date",
                ),
                IndexModel(
                    [("level", ASCENDING), ("date", DESCENDING)],
                    name="idx_pipeline_log_level_date",
                ),
                IndexModel(
                    [("date", DESCENDING), ("created_at", DESCENDING)],
                    name="idx_pipeline_log_date_created",
                ),
            ],
            "llm_call_logs": [
                IndexModel(
                    [("call_id", ASCENDING)],
                    unique=True,
                    name="idx_llm_call_id",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_llm_user_created",
                ),
                IndexModel(
                    [("task_id", ASCENDING), ("agent_type", ASCENDING)],
                    name="idx_llm_task_agent",
                ),
                IndexModel(
                    [("agent_type", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_llm_agent_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_llm_expires",
                ),
            ],
            "execution_runs": [
                IndexModel([("execution_id", ASCENDING)], unique=True, name="idx_run_execution_id"),
                IndexModel(
                    [("owner_user_id", ASCENDING), ("started_at", DESCENDING)],
                    name="idx_run_owner_started",
                ),
                IndexModel(
                    [("initiator_user_id", ASCENDING), ("started_at", DESCENDING)],
                    name="idx_run_initiator_started",
                ),
                IndexModel([("task_id", ASCENDING)], name="idx_run_task_id"),
                IndexModel(
                    [
                        ("scope", ASCENDING),
                        ("execution_type", ASCENDING),
                        ("status", ASCENDING),
                        ("started_at", DESCENDING),
                    ],
                    name="idx_run_scope_type_status_started",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_run_expires",
                ),
            ],
            "execution_events": [
                IndexModel([("event_id", ASCENDING)], unique=True, name="idx_event_event_id"),
                IndexModel(
                    [("execution_id", ASCENDING), ("sequence", ASCENDING)],
                    unique=True,
                    name="idx_event_execution_sequence",
                ),
                IndexModel(
                    [("execution_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_event_execution_created",
                ),
                IndexModel(
                    [("task_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_event_task_created",
                ),
                IndexModel(
                    [("owner_user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_event_owner_created",
                ),
                IndexModel(
                    [("level", ASCENDING), ("phase", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_event_level_phase_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_event_expires",
                ),
            ],
            "execution_links": [
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("shared_execution_id", ASCENDING),
                        ("task_id", ASCENDING),
                    ],
                    unique=True,
                    name="idx_link_user_shared_task",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("joined_at", DESCENDING)],
                    name="idx_link_user_joined",
                ),
                IndexModel([("shared_execution_id", ASCENDING)], name="idx_link_shared_execution"),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_link_expires",
                ),
            ],
        }

        try:
            await cls.get_collection("chat_sessions").drop_index("article_url_hash_1_draft_index_1")
            logger.info("Dropped legacy chat_sessions index")
        except OperationFailure as exc:
            if exc.code != 27:  # IndexNotFound
                raise

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
    async def allocate_execution_sequence(cls, execution_id: str) -> int:
        """通过单文档原子自增为 execution event 分配稳定序号。"""

        if not execution_id:
            raise ValueError("execution_id must not be empty")
        run = await cls.get_collection("execution_runs").find_one_and_update(
            {"execution_id": execution_id},
            {"$inc": {"next_sequence": 1}},
            projection={"next_sequence": True, "_id": False},
            return_document=ReturnDocument.AFTER,
        )
        if run is None:
            raise LookupError(f"execution run not found: {execution_id}")
        return int(run["next_sequence"])

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
