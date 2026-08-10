"""A2A 1.0 协议实现 — 阶段四 4B。

模块：
  - models.py     协议数据模型 / Agent Card / Task 八态；
  - mapper.py     协议 ↔ 内部 RuntimeState 映射与不可信输入净化；
  - task_store.py A2A Task 持久化（版本乐观锁 + 幂等 + 多租户）。
"""

from __future__ import annotations

from agent.a2a.models import (
    AGENT_CARD_PATH,
    PROTOCOL_VERSION,
    SDK_VERSION,
    VERSION_HEADER,
    A2AError,
    AgentCard,
    Artifact,
    InvalidInputError,
    Message,
    MethodNotImplementedError,
    Part,
    ProtocolError,
    Skill,
    Task,
    TaskSendResult,
    TaskStatus,
    TaskStatusUpdateEvent,
    TERMINAL_TASK_STATUSES,
)
from agent.a2a.task_store import A2ATaskConflictError, A2ATaskStore

__all__ = [
    "AGENT_CARD_PATH",
    "PROTOCOL_VERSION",
    "SDK_VERSION",
    "VERSION_HEADER",
    "A2AError",
    "A2ATaskConflictError",
    "A2ATaskStore",
    "AgentCard",
    "Artifact",
    "InvalidInputError",
    "Message",
    "MethodNotImplementedError",
    "Part",
    "ProtocolError",
    "Skill",
    "Task",
    "TaskSendResult",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TERMINAL_TASK_STATUSES",
]
