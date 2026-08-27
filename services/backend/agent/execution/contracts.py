"""Unified Execution Contract（Cutover 计划 §4 / §5 / §6 / §7 / §21）。

所有入口（API / ARQ / Scheduler / Retry / Resume）统一转换为 ExecutionRequest，
所有 Runtime 最终返回统一 ExecutionResult，路由经由 WorkflowExecutor Protocol。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

ExecutionStatus = Literal["SUCCEEDED", "PARTIAL", "BLOCKED", "FAILED"]
ExecutionEngine = Literal["legacy", "skill_planned"]

# 生产当前仅两种 Engine；Router 决定具体路径。
EXECUTION_ENGINES: tuple[ExecutionEngine, ...] = ("legacy", "skill_planned")


class ExecutionRequest(BaseModel):
    """统一执行请求（§5）。"""

    task_id: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    goal: str = ""

    user_id: str = ""
    tenant_id: str = ""

    trace_id: str = ""
    request_id: str = ""

    crawl_days: int = 1
    article_url_hash: str | None = None

    resume_token: str | None = None

    # 其他入口透传字段
    username: str = ""
    run_id: str = ""
    execution_mode: str = ""
    input_snapshot_hash: str = ""

    # Sticky routing：任务创建时写入，Retry/Resume 复用（§31 / §32）
    selected_engine: ExecutionEngine | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    """统一执行结果（§6）。"""

    run_id: str = ""
    task_id: str = ""
    status: ExecutionStatus = "SUCCEEDED"
    engine: ExecutionEngine = "legacy"

    artifact_refs: list[str] = Field(default_factory=list)

    output: dict[str, Any] = Field(default_factory=dict)

    error_code: str | None = None
    error_message: str | None = None

    trace_id: str = ""

    latency_ms: float = 0

    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class WorkflowExecutor(Protocol):
    """所有可执行引擎的统一契约（§7）。"""

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...

    async def resume(self, request: ExecutionRequest) -> ExecutionResult: ...


@dataclass
class ExecutionRuntimeBundle:
    """一次装配得到的全部 Execution 运行时句柄（§21）。"""

    execution_router: Any

    orchestration_runtime: Any | None = None
    skill_runtime: Any | None = None
    skill_registry: Any | None = None

    business_tool_executor: Any | None = None

    legacy_executor: WorkflowExecutor | None = None

    shadow_executor: Any | None = None
    shadow_comparator: Any | None = None
    rollout: Any | None = None

    mode: str = "legacy"
    skill_snapshot_hash: str = ""
    knowledge_backend: str = ""
    wiki_version: str = ""
    business_tool_snapshot: str = ""
    legacy_loaded: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "EXECUTION_ENGINES",
    "ExecutionEngine",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionRuntimeBundle",
    "ExecutionStatus",
    "WorkflowExecutor",
]
