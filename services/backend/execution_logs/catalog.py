"""全链路日志事件字典、多租户归属规则和脱敏基线。

本模块只定义 L0 阶段冻结的稳定契约，不负责持久化或脱敏实现。后续日志
模型、写入服务和 API 必须引用这里的枚举及规则，避免业务模块自行创造 action。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Scope(StrEnum):
    """执行记录的租户可见范围。"""

    USER = "user"
    SHARED = "shared"
    SYSTEM = "system"


class Relation(StrEnum):
    """用户与公共执行的关系。"""

    INITIATOR = "initiator"
    WAITER = "waiter"
    REUSER = "reuser"


class ExecutionType(StrEnum):
    PIPELINE_V1 = "pipeline_v1"
    PIPELINE_V2 = "pipeline_v2"
    CRAWL_OVERSEAS = "crawl_overseas"
    CRAWL_WEWE = "crawl_wewe"
    CLASSIFY = "classify"
    SCORE = "score"
    DRAFT = "draft"
    CHAT = "chat"
    REVISE = "revise"
    PROFILE_REBUILD = "profile_rebuild"


class Phase(StrEnum):
    REQUEST = "request"
    AUTH = "auth"
    TASK = "task"
    LOCK = "lock"
    CRAWL = "crawl"
    PERSIST = "persist"
    CLASSIFY = "classify"
    SCORE = "score"
    PROFILE = "profile"
    DRAFT = "draft"
    CHAT = "chat"
    FEEDBACK = "feedback"
    SYSTEM = "system"


class Action(StrEnum):
    REQUEST_RECEIVED = "request_received"
    AUTHENTICATION_COMPLETED = "authentication_completed"
    TASK_CREATED = "task_created"
    SHARED_LOCK_ACQUIRE = "shared_lock_acquire"
    SHARED_EXECUTION_REUSED = "shared_execution_reused"
    MCP_REQUEST = "mcp_request"
    SITE_FEED_RESULT = "site_feed_result"
    ARTICLES_UPSERTED = "articles_upserted"
    CLASSIFICATION_BATCH = "classification_batch"
    SCORING_BATCH = "scoring_batch"
    STYLE_PROFILE_LOADED = "style_profile_loaded"
    STYLE_PROFILE_REBUILT = "style_profile_rebuilt"
    DRAFTS_GENERATED = "drafts_generated"
    REVISION_APPLIED = "revision_applied"
    CHAT_COMPLETED = "chat_completed"
    FEEDBACK_RECORDED = "feedback_recorded"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    SHARED_RESULT_REUSED = "shared_result_reused"
    SYSTEM_RECOVERY = "system_recovery"


class ErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_DENIED = "authorization_denied"
    INVALID_REQUEST = "invalid_request"
    TASK_NOT_FOUND = "task_not_found"
    LOCK_TIMEOUT = "lock_timeout"
    MCP_TIMEOUT = "mcp_timeout"
    MCP_UNAVAILABLE = "mcp_unavailable"
    CRAWL_SOURCE_FAILED = "crawl_source_failed"
    DATABASE_WRITE_FAILED = "database_write_failed"
    LLM_TIMEOUT = "llm_timeout"
    LLM_PROVIDER_ERROR = "llm_provider_error"
    PIPELINE_FAILED = "pipeline_failed"
    LOG_WRITE_FAILED = "log_write_failed"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个标准 action 的归属及最小 detail 契约。"""

    phase: Phase
    allowed_scopes: frozenset[Scope]
    detail_fields: frozenset[str]


_USER = frozenset({Scope.USER})
_SHARED = frozenset({Scope.SHARED})
_ANY = frozenset(Scope)

ACTION_SPECS: Final = MappingProxyType(
    {
        Action.REQUEST_RECEIVED: ActionSpec(
            Phase.REQUEST, _ANY, frozenset({"method", "route", "client_ip_hash"})
        ),
        Action.AUTHENTICATION_COMPLETED: ActionSpec(
            Phase.AUTH, _USER | _SHARED, frozenset({"authentication_method"})
        ),
        Action.TASK_CREATED: ActionSpec(Phase.TASK, _USER, frozenset({"task_type"})),
        Action.SHARED_LOCK_ACQUIRE: ActionSpec(
            Phase.LOCK, _SHARED, frozenset({"lock_key", "acquired", "wait_ms"})
        ),
        Action.SHARED_EXECUTION_REUSED: ActionSpec(Phase.LOCK, _SHARED, frozenset({"wait_ms"})),
        Action.MCP_REQUEST: ActionSpec(
            Phase.CRAWL, _SHARED, frozenset({"tool", "timeout_ms", "attempt"})
        ),
        Action.SITE_FEED_RESULT: ActionSpec(
            Phase.CRAWL,
            _SHARED,
            frozenset(
                {
                    "site",
                    "feed_type",
                    "http_status",
                    "feed_entries",
                    "accepted",
                    "skipped_old",
                    "skipped_no_date",
                    "retry_count",
                }
            ),
        ),
        Action.ARTICLES_UPSERTED: ActionSpec(
            Phase.PERSIST,
            _SHARED,
            frozenset(
                {"collection", "operation", "requested", "inserted", "updated", "skipped_duplicate"}
            ),
        ),
        Action.CLASSIFICATION_BATCH: ActionSpec(
            Phase.CLASSIFY,
            _SHARED,
            frozenset({"count", "provider", "model", "fallback", "token_usage"}),
        ),
        Action.SCORING_BATCH: ActionSpec(
            Phase.SCORE,
            _SHARED,
            frozenset({"count", "threshold", "candidates", "provider", "model", "token_usage"}),
        ),
        Action.STYLE_PROFILE_LOADED: ActionSpec(
            Phase.PROFILE, _USER, frozenset({"found", "version"})
        ),
        Action.STYLE_PROFILE_REBUILT: ActionSpec(
            Phase.PROFILE, _USER, frozenset({"version", "source_count"})
        ),
        Action.DRAFTS_GENERATED: ActionSpec(
            Phase.DRAFT, _USER, frozenset({"article_hash", "templates", "count", "token_usage"})
        ),
        Action.REVISION_APPLIED: ActionSpec(
            Phase.DRAFT, _USER, frozenset({"article_hash", "revision_id"})
        ),
        Action.CHAT_COMPLETED: ActionSpec(
            Phase.CHAT, _USER, frozenset({"mode", "duration_ms", "token_usage"})
        ),
        Action.FEEDBACK_RECORDED: ActionSpec(
            Phase.FEEDBACK, _USER, frozenset({"target_type", "rating", "tag_count"})
        ),
        Action.TASK_COMPLETED: ActionSpec(
            Phase.TASK, _USER | _SHARED, frozenset({"totals", "duration_ms"})
        ),
        Action.TASK_FAILED: ActionSpec(
            Phase.TASK, _USER | _SHARED, frozenset({"error_code", "retryable"})
        ),
        Action.SHARED_RESULT_REUSED: ActionSpec(Phase.TASK, _USER, frozenset({"relation"})),
        Action.SYSTEM_RECOVERY: ActionSpec(
            Phase.SYSTEM, frozenset({Scope.SYSTEM}), frozenset({"recovered", "interrupted"})
        ),
    }
)

# 任何请求体/detail 都必须先按 action 的 allowlist 选取；以下名称即使误入也必须脱敏。
SENSITIVE_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "authorization",
        "secret",
        "api_key",
        "apikey",
        "cookie",
        "mongo_uri",
        "proxy",
        "auth_code",
    }
)
SENSITIVE_KEY_SUFFIXES: Final[tuple[str, ...]] = (
    "_password",
    "_secret",
    "_api_key",
    "_token",
    "_cookie",
    "_proxy",
)
SENSITIVE_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {"token", "access_token", "code", "auth", "authorization", "key", "api_key"}
)
DETAIL_ALLOWLIST: Final = MappingProxyType(
    {action: spec.detail_fields for action, spec in ACTION_SPECS.items()}
)


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    owner_user_id_required: bool
    initiator_user_id_required: bool
    link_required_for_user_read: bool
    exposed_to_normal_user: bool
    expose_owner_user_id: bool
    expose_initiator_user_id: bool
    expose_participant_user_ids: bool


SCOPE_POLICIES: Final = MappingProxyType(
    {
        Scope.USER: ScopePolicy(True, False, False, True, True, False, False),
        Scope.SHARED: ScopePolicy(False, True, True, True, False, False, False),
        Scope.SYSTEM: ScopePolicy(False, False, False, False, False, False, False),
    }
)


def action_spec(action: Action | str) -> ActionSpec:
    """返回标准 action 定义；未知 action 立即失败，禁止静默扩展字典。"""

    return ACTION_SPECS[Action(action)]
