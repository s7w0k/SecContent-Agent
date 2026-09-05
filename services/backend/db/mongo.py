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
            "user_prompts": [
                IndexModel(
                    [("user_id", ASCENDING), ("prompt_key", ASCENDING)],
                    unique=True,
                    name="uq_user_prompt_key",
                ),
            ],
            "user_prompt_versions": [
                IndexModel(
                    [("user_id", ASCENDING), ("prompt_key", ASCENDING), ("version", DESCENDING)],
                    unique=True,
                    name="uq_user_prompt_version",
                ),
            ],
            "user_generation_preferences": [
                IndexModel(
                    [("user_id", ASCENDING)],
                    unique=True,
                    name="uq_user_generation_preferences",
                ),
            ],
            "user_article_assessments": [
                IndexModel(
                    [("user_id", ASCENDING), ("article_url_hash", ASCENDING)],
                    unique=True,
                    name="uq_user_article_assessment",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("scoring.candidate_score", DESCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="idx_user_candidate_score",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("classification.category_v2", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="idx_user_assessment_category",
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
                IndexModel(
                    [("run_id", ASCENDING), ("loop_round", ASCENDING)],
                    name="idx_llm_run_loop_round",
                ),
                IndexModel(
                    [("trace_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_llm_trace_created",
                ),
            ],
            "agent_run_events": [
                IndexModel(
                    [("run_id", ASCENDING), ("sequence", ASCENDING)],
                    unique=True,
                    name="idx_agent_event_run_sequence",
                ),
                IndexModel(
                    [("trace_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_agent_event_trace_created",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_agent_event_user_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_agent_event_expires",
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
            # ── 用户记忆与个性化 ──────────────────────────
            "user_profile_policies": [
                IndexModel([("user_id", ASCENDING)], unique=True, name="uq_profile_policy_user"),
            ],
            "user_memory_events": [
                IndexModel([("event_id", ASCENDING)], unique=True, name="uq_memory_event_id"),
                IndexModel(
                    [("idempotency_key", ASCENDING)],
                    unique=True,
                    name="uq_memory_event_idempotency",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_memory_event_processing",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("source_type", ASCENDING),
                        ("created_at", DESCENDING),
                    ],
                    name="idx_memory_event_user_source",
                ),
            ],
            "user_memory_items": [
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("normalized_key", ASCENDING),
                        ("scope.category_v2", ASCENDING),
                        ("scope.template_id", ASCENDING),
                        ("scope.stage", ASCENDING),
                        ("polarity", ASCENDING),
                    ],
                    unique=True,
                    name="uq_memory_item_scope",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("status", ASCENDING),
                        ("scope.category_v2", ASCENDING),
                        ("scope.stage", ASCENDING),
                        ("confidence", DESCENDING),
                    ],
                    name="idx_memory_item_retrieval",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    partialFilterExpression={"expires_at": {"$type": "date"}},
                    name="ttl_memory_item_expiry",
                ),
            ],
            "user_memory_summaries": [
                IndexModel(
                    [("user_id", ASCENDING), ("scope_key", ASCENDING)],
                    unique=True,
                    name="uq_memory_summary_scope",
                ),
            ],
            "generation_runs": [
                IndexModel([("generation_id", ASCENDING)], unique=True, name="uq_generation_id"),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("article_url_hash", ASCENDING),
                        ("draft_index", ASCENDING),
                    ],
                    name="idx_generation_user_draft",
                ),
                IndexModel(
                    [("experiment.experiment_id", ASCENDING), ("experiment.group", ASCENDING)],
                    name="idx_generation_experiment",
                ),
                IndexModel([("created_at", DESCENDING)], name="idx_generation_created"),
            ],
            "personalization_candidates": [
                IndexModel([("candidate_id", ASCENDING)], unique=True, name="uq_candidate_id"),
                IndexModel(
                    [("target_type", ASCENDING), ("status", ASCENDING)],
                    name="idx_candidate_type_status",
                ),
            ],
            "personalization_feedbacks": [
                IndexModel([("feedback_id", ASCENDING)], unique=True, name="uq_pers_feedback_id"),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_pers_feedback_user_date",
                ),
            ],
            "knowledge_drafts": [
                IndexModel([("draft_id", ASCENDING)], unique=True, name="uq_knowledge_draft_id"),
                IndexModel(
                    [("relative_path", ASCENDING), ("status", ASCENDING)],
                    name="idx_knowledge_draft_path_status",
                ),
                IndexModel([("updated_at", DESCENDING)], name="idx_knowledge_draft_updated"),
            ],
            "knowledge_revisions": [
                IndexModel(
                    [("revision_id", ASCENDING)],
                    unique=True,
                    name="uq_knowledge_revision_id",
                ),
                IndexModel([("publication_id", ASCENDING)], name="idx_knowledge_revision_pub"),
                IndexModel([("relative_path", ASCENDING)], name="idx_knowledge_revision_path"),
            ],
            "knowledge_publications": [
                IndexModel(
                    [("publication_id", ASCENDING)],
                    unique=True,
                    name="uq_knowledge_pub_id",
                ),
                IndexModel([("published_at", DESCENDING)], name="idx_knowledge_pub_date"),
                IndexModel([("status", ASCENDING)], name="idx_knowledge_pub_status"),
            ],
            "knowledge_publish_locks": [
                IndexModel([("lock_key", ASCENDING)], unique=True, name="uq_knowledge_lock_key"),
                IndexModel([("expires_at", ASCENDING)], name="idx_knowledge_lock_expires"),
            ],
            "knowledge_audit_logs": [
                IndexModel([("audit_id", ASCENDING)], unique=True, name="uq_knowledge_audit_id"),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_knowledge_audit_user_date",
                ),
                IndexModel(
                    [("action", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_knowledge_audit_action_date",
                ),
            ],
            # ── Web 搜索 (SearXNG) ──────────────────────────
            "search_sessions": [
                IndexModel(
                    [("search_id", ASCENDING)],
                    unique=True,
                    name="idx_search_id",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_search_user_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    name="idx_search_session_ttl",
                    expireAfterSeconds=0,
                ),
            ],
            "search_import_batches": [
                IndexModel(
                    [("user_id", ASCENDING), ("idempotency_key", ASCENDING)],
                    unique=True,
                    name="idx_import_batch_user_idem",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_import_batch_user_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    name="idx_import_batch_ttl",
                    expireAfterSeconds=0,
                ),
            ],
            "search_import_items": [
                IndexModel(
                    [("user_id", ASCENDING), ("search_id", ASCENDING), ("result_id", ASCENDING)],
                    unique=True,
                    name="idx_import_item_user_search_result",
                ),
                IndexModel(
                    [("batch_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_import_item_batch_created",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_import_item_user_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    name="idx_import_item_ttl",
                    expireAfterSeconds=0,
                ),
            ],
            "user_knowledge_entries": [
                IndexModel(
                    [("entry_id", ASCENDING)],
                    unique=True,
                    name="uq_user_knowledge_entry",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("product_id", ASCENDING),
                        ("doc_type", ASCENDING),
                    ],
                    name="idx_user_knowledge_product_doc",
                ),
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("enabled", ASCENDING),
                        ("sort_order", ASCENDING),
                    ],
                    name="idx_user_knowledge_enabled",
                ),
            ],
            "user_products": [
                IndexModel(
                    [("user_id", ASCENDING), ("product_id", ASCENDING)],
                    unique=True,
                    name="uq_user_product",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("enabled", ASCENDING)],
                    name="idx_user_product_enabled",
                ),
            ],
            "crawl_runs": [
                IndexModel(
                    [("run_key", ASCENDING)],
                    unique=True,
                    name="uq_crawl_run_key",
                ),
                IndexModel(
                    [("job_type", ASCENDING), ("started_at", DESCENDING)],
                    name="idx_crawl_run_job_started",
                ),
                IndexModel(
                    [("status", ASCENDING), ("updated_at", DESCENDING)],
                    name="idx_crawl_run_status_updated",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="idx_crawl_run_expires",
                ),
            ],
            "articles": [
                IndexModel(
                    [("url_hash", ASCENDING)],
                    unique=True,
                    name="idx_url_hash",
                ),
                IndexModel(
                    [("added_at", DESCENDING)],
                    name="idx_added_at",
                ),
                IndexModel(
                    [("source_type", ASCENDING), ("added_at", DESCENDING)],
                    name="idx_source_added",
                ),
                IndexModel(
                    [("crawl_run_id", ASCENDING)],
                    sparse=True,
                    name="idx_articles_crawl_run",
                ),
            ],
            "user_article_scores": [
                IndexModel(
                    [("user_id", ASCENDING), ("url_hash", ASCENDING)],
                    unique=True,
                    name="uq_user_article_score",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("pr_total_score", DESCENDING)],
                    name="idx_user_score_total",
                ),
            ],
            # ── MultiAgent 编排（阶段三）：Step Ledger ──────────────
            "execution_step_ledger": [
                # (run_id, step_id) 唯一
                IndexModel(
                    [("run_id", ASCENDING), ("step_id", ASCENDING)],
                    unique=True,
                    name="uq_step_ledger_run_step",
                ),
                # idempotency_key 唯一（失败/跳过为空串，sparse 允许空串并存）
                IndexModel(
                    [("idempotency_key", ASCENDING)],
                    unique=True,
                    sparse=True,
                    name="uq_step_ledger_idempotency",
                ),
                # 过期 running 接管查询
                IndexModel(
                    [("status", ASCENDING), ("lease_expires_at", ASCENDING)],
                    name="idx_step_ledger_status_lease",
                ),
                # 按 plan 检索
                IndexModel(
                    [("plan_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_step_ledger_plan_created",
                ),
                # dead-letter 查询
                IndexModel(
                    [("status", ASCENDING), ("run_id", ASCENDING)],
                    name="idx_step_ledger_deadletter",
                ),
                # TTL/归档
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_step_ledger_expires",
                ),
            ],
            "ledger_repair_queue": [
                IndexModel(
                    [("run_id", ASCENDING), ("step_id", ASCENDING)],
                    name="idx_ledger_repair_run_step",
                ),
                IndexModel(
                    [("status", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_ledger_repair_status_created",
                ),
            ],
            # ── MultiAgent 观测事件（Step 9）──────────────────
            "pipeline_events": [
                IndexModel(
                    [("run_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_pipeline_events_run_created",
                ),
                IndexModel(
                    [("event_type", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_pipeline_events_type_created",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_pipeline_events_expires",
                ),
            ],
            # ── 全自主 Agent（阶段四 4A）：运行/事件/审批 ────────
            "runtime_runs": [
                IndexModel(
                    [("run_id", ASCENDING)],
                    unique=True,
                    name="uq_runtime_run_id",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_runtime_run_user_created",
                ),
                IndexModel(
                    [("status", ASCENDING), ("updated_at", DESCENDING)],
                    name="idx_runtime_run_status_updated",
                ),
            ],
            "runtime_events": [
                IndexModel(
                    [("run_id", ASCENDING), ("sequence", ASCENDING)],
                    unique=True,
                    name="uq_runtime_event_run_seq",
                ),
                IndexModel(
                    [("run_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_runtime_event_run_created",
                ),
                IndexModel(
                    [("run_id", ASCENDING), ("deduplication_key", ASCENDING)],
                    unique=True,
                    partialFilterExpression={"deduplication_key": {"$gt": ""}},
                    name="uq_runtime_event_dedup",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_runtime_events_expires",
                ),
            ],
            "runtime_approvals": [
                IndexModel(
                    [("approval_id", ASCENDING)],
                    unique=True,
                    name="uq_runtime_approval_id",
                ),
                IndexModel(
                    [("status", ASCENDING), ("expires_at", ASCENDING)],
                    name="idx_runtime_approval_status_expires",
                ),
                IndexModel(
                    [("run_id", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_runtime_approval_run_created",
                ),
            ],
            # ── 阶段三 可控追溯：清单/租约/Outbox（与各模块 index_specs 保持一致）──
            "runtime_manifests": [
                IndexModel(
                    [("run_id", ASCENDING)],
                    unique=True,
                    name="uq_manifest_run_id",
                ),
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_manifest_user_created",
                ),
                IndexModel(
                    [("code_revision", ASCENDING)],
                    name="idx_manifest_code_revision",
                ),
            ],
            # ── 全链路对话任务（阶段 1）：租户状态、CAS 与 TTL ─────────
            "conversation_tasks": [
                IndexModel(
                    [("task_id", ASCENDING)],
                    unique=True,
                    name="uq_task_state_task_id",
                ),
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("thread_id", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="idx_task_state_scope_thread_updated",
                ),
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("status", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="idx_task_state_scope_active",
                ),
                IndexModel(
                    [("expires_at", ASCENDING)],
                    expireAfterSeconds=0,
                    name="ttl_task_state_expires",
                ),
            ],
            # ── 阶段 2 业务 Tool：不可变草稿版本与导出引用 ───────────
            "agent_draft_artifacts": [
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("artifact_id", ASCENDING),
                    ],
                    unique=True,
                    name="uq_agent_artifact_scope_id",
                ),
                IndexModel(
                    [("tenant_id", ASCENDING), ("tool_idempotency_key", ASCENDING)],
                    unique=True,
                    partialFilterExpression={"tool_idempotency_key": {"$gt": ""}},
                    name="uq_agent_artifact_tool_idempotency",
                ),
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("parent_artifact_id", ASCENDING),
                        ("version", ASCENDING),
                    ],
                    name="idx_agent_artifact_lineage",
                ),
            ],
            "agent_draft_exports": [
                IndexModel(
                    [("tenant_id", ASCENDING), ("export_id", ASCENDING)],
                    unique=True,
                    name="uq_agent_export_scope_id",
                ),
                IndexModel(
                    [
                        ("tenant_id", ASCENDING),
                        ("user_id", ASCENDING),
                        ("artifact_id", ASCENDING),
                        ("artifact_version", ASCENDING),
                    ],
                    name="idx_agent_export_artifact_version",
                ),
            ],
            # ── Artifact Layer MongoArtifactStore（OneShot Cutover §17 / §18）──
            "agent_artifacts": [
                IndexModel(
                    [
                        ("artifact_type", ASCENDING),
                        ("artifact_id", ASCENDING),
                        ("version", ASCENDING),
                    ],
                    unique=True,
                    name="uq_artifact_type_id_version",
                ),
                IndexModel(
                    [("run_id", ASCENDING)],
                    name="idx_artifact_run_id",
                ),
                IndexModel(
                    [("tenant_id", ASCENDING), ("artifact_id", ASCENDING)],
                    name="idx_artifact_tenant_id",
                ),
                IndexModel(
                    [("parent_ref", ASCENDING)],
                    name="idx_artifact_parent_ref",
                ),
                IndexModel(
                    [("created_at", DESCENDING)],
                    name="idx_artifact_created_at",
                ),
            ],
            "runtime_leases": [
                IndexModel(
                    [("run_id", ASCENDING)],
                    unique=True,
                    name="uq_lease_run_id",
                ),
                IndexModel(
                    [("owner_id", ASCENDING)],
                    name="idx_lease_owner",
                ),
            ],
            # ── skill_planned Durable Resume（Final Closure EPIC-A §5 / §6）──
            "agent_execution_runs": [
                IndexModel(
                    [("run_id", ASCENDING)],
                    unique=True,
                    name="uq_exec_run_id",
                ),
                IndexModel(
                    [("task_id", ASCENDING)],
                    unique=True,
                    name="uq_exec_run_task",
                ),
                IndexModel(
                    [("tenant_id", ASCENDING), ("created_at", DESCENDING)],
                    name="idx_exec_run_tenant_created",
                ),
                IndexModel(
                    [("status", ASCENDING), ("updated_at", DESCENDING)],
                    name="idx_exec_run_status_updated",
                ),
            ],
            "event_outbox": [
                IndexModel(
                    [("dedup_key", ASCENDING)],
                    unique=True,
                    sparse=True,
                    name="uq_outbox_dedup",
                ),
                IndexModel(
                    [("status", ASCENDING), ("created_at", ASCENDING)],
                    name="idx_outbox_status_created",
                ),
                IndexModel(
                    [("run_id", ASCENDING)],
                    name="idx_outbox_run_id",
                ),
            ],
            "event_outbox_dead_letter": [
                IndexModel(
                    [("entry_id", ASCENDING)],
                    unique=True,
                    name="uq_outbox_dead_entry",
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
