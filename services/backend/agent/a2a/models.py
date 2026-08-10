"""A2A 1.0 协议数据模型（自研精简实现）— 阶段四 4B Step 4B-1。

协议依据 A2A 1.0 规范（https://a2a-protocol.org/v1.0.0/specification/），
本项目首版采用 HTTP + JSON/REST 传输，不引入 a2a-sdk 运行时依赖。

安全约束：
  - 本模块模型是「外部契约层」：外部 Message/Artifact/URL 一律视为不可信输入，
    使用前必须经过 mapper.validate_external_input 的净化与校验；
  - 持久化时只保留协议元数据与脱敏摘要，不保存提示词、密钥或私有推理；
  - 未知/未实现能力返回明确协议错误，不静默伪装成功。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── 协议常量 ──────────────────────────────────────────────
PROTOCOL_VERSION = "1.0"  # ADR-001：固定 A2A 1.0
VERSION_HEADER = "A2A-Version"  # 请求显式携带
AGENT_CARD_PATH = "/.well-known/agent-card.json"  # ADR-001：Agent Card 固定路径
SDK_VERSION = "a2a-sdk 1.1.2"  # ADR-001：官方 SDK 精确版本（仅用于互操作记录）


# ═══════════════════════════════════════════════════════════════
# 错误
# ═══════════════════════════════════════════════════════════════


class A2AError(Exception):
    """A2A 基础错误。"""


class ProtocolError(A2AError):
    """协议格式/版本错误。"""


class InvalidInputError(A2AError):
    """不可信输入被拒绝（类型/大小/来源/恶意内容）。"""


class MethodNotImplementedError(A2AError):
    """Agent Card 未声明的能力被调用：返回明确协议错误。"""


# ═══════════════════════════════════════════════════════════════
# Task 状态（A2A 1.0 八种状态，4B-1 要求至少覆盖）
# ═══════════════════════════════════════════════════════════════


class TaskStatus(str, Enum):
    """A2A Task 生命周期状态。"""

    SUBMITTED = "SUBMITTED"
    WORKING = "WORKING"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


# 终态：到达后不可再进入运行态
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
        TaskStatus.REJECTED,
    }
)


# ═══════════════════════════════════════════════════════════════
# Agent Card / Skill（发现层）
# ═══════════════════════════════════════════════════════════════


class Skill(BaseModel):
    """Agent Card 中声明的能力。只发布真实可用且获准开放的能力。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ["text"])
    output_modes: list[str] = Field(default_factory=lambda: ["text"])


class AgentCard(BaseModel):
    """Agent Card：真实能力的受控声明。"""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    url: str = Field(..., min_length=1, max_length=2000)
    version: str = Field(default="1.0.0", max_length=32)
    protocol_version: str = PROTOCOL_VERSION
    skills: list[Skill] = Field(default_factory=list)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])


# ═══════════════════════════════════════════════════════════════
# Message / Part / Artifact
# ═══════════════════════════════════════════════════════════════


class Part(BaseModel):
    """单一内容片段：text / file / data 三态（不可信输入载体）。"""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["text", "file", "data"] = "text"
    text: str | None = Field(default=None, max_length=200_000)
    name: str | None = Field(default=None, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    uri: str | None = Field(default=None, max_length=2000)
    data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_kind_field(self) -> Part:
        if self.kind == "text" and self.text is None:
            raise ValueError("text part requires text")
        if self.kind == "file" and not (self.uri or self.name):
            raise ValueError("file part requires uri or name")
        if self.kind == "data" and self.data is None:
            raise ValueError("data part requires data")
        return self


class Message(BaseModel):
    """用户或 Agent 消息（不可信输入，使用前须净化）。"""

    model_config = ConfigDict(extra="ignore")

    message_id: str = Field(..., min_length=1, max_length=100)
    task_id: str = Field(default="", max_length=100)
    role: Literal["user", "agent"] = "user"
    parts: list[Part] = Field(default_factory=list)
    context_id: str = Field(default="", max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    """Agent 产出物（对外只暴露脱敏内容与元数据）。"""

    model_config = ConfigDict(extra="ignore")

    artifact_id: str = Field(..., min_length=1, max_length=100)
    name: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=2000)
    parts: list[Part] = Field(default_factory=list)
    mime_type: str = Field(default="", max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    """A2A Task：与内部 RuntimeState 双向追溯。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1, max_length=100)
    context_id: str = Field(default="", max_length=100)
    status: TaskStatus = TaskStatus.SUBMITTED
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_updated_timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # 内部追溯字段（非协议标准扩展）：internal_run_id 仅用于站内审计，不对外发布
    internal_run_id: str = Field(default="", max_length=100)


class TaskSendResult(BaseModel):
    """Send 响应：返回 Task（新任务）或 Message（消息级应答）。"""

    model_config = ConfigDict(extra="ignore")

    task: Task | None = None
    message: Message | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> TaskSendResult:
        if (self.task is None) == (self.message is None):
            raise ValueError("TaskSendResult requires exactly one of task/message")
        return self


class TaskStatusUpdateEvent(BaseModel):
    """Subscribe 事件：Task 状态变更（支持游标重连）。"""

    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(..., min_length=1, max_length=100)
    task_id: str = Field(..., min_length=1, max_length=100)
    status: TaskStatus
    message: Message | None = None
    artifact: Artifact | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
