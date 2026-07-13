"""全链路执行日志的稳定契约。"""

from .catalog import (
    ACTION_SPECS,
    DETAIL_ALLOWLIST,
    SCOPE_POLICIES,
    SENSITIVE_KEY_NAMES,
    SENSITIVE_KEY_SUFFIXES,
    SENSITIVE_QUERY_KEYS,
    Action,
    ErrorCode,
    ExecutionType,
    Phase,
    Relation,
    Scope,
    action_spec,
)

__all__ = [
    "ACTION_SPECS",
    "DETAIL_ALLOWLIST",
    "SCOPE_POLICIES",
    "SENSITIVE_KEY_NAMES",
    "SENSITIVE_KEY_SUFFIXES",
    "SENSITIVE_QUERY_KEYS",
    "Action",
    "ErrorCode",
    "ExecutionType",
    "Phase",
    "Relation",
    "Scope",
    "action_spec",
]
