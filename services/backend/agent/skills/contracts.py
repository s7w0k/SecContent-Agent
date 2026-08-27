"""Skill Layer 契约 - SkillBudget / SkillRequest / SkillResult / SkillExecutor。

对应计划 §11 / §12。Skill 承载"这一类任务怎样完成"：
有版本、有前置/后置条件、有 Tool 白名单、有产物契约。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

SkillResultStatus = Literal["SUCCEEDED", "PARTIAL", "BLOCKED", "FAILED"]


class SkillBudget(BaseModel):
    """一次 Skill 执行的资源上限（计划 §11）。"""

    max_tool_calls: int = Field(default=20, ge=0)
    max_llm_calls: int = Field(default=10, ge=0)
    max_runtime_seconds: int = Field(default=120, ge=1)
    max_input_tokens: int = Field(default=16000, ge=1)
    max_output_tokens: int = Field(default=8000, ge=1)


class SkillRequest(BaseModel):
    """一次 Skill 执行请求（计划 §11）。"""

    model_config = {"extra": "forbid"}

    skill_name: str = Field(..., pattern=r"^[a-z0-9-]+$")
    run_id: str
    user_id: str
    tenant_id: str
    trace_id: str = ""
    input_refs: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    budget: SkillBudget = Field(default_factory=SkillBudget)


class SkillResult(BaseModel):
    """一次 Skill 执行结果（计划 §11）。"""

    skill_name: str
    status: SkillResultStatus
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    next_recommendations: list[str] = Field(default_factory=list)
    error_code: str = ""
    message: str = ""

    @classmethod
    def succeeded(
        cls,
        skill_name: str,
        *,
        artifact_refs: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        next_recommendations: list[str] | None = None,
    ) -> SkillResult:
        return cls(
            skill_name=skill_name,
            status="SUCCEEDED",
            artifact_refs=list(artifact_refs or []),
            evidence_refs=list(evidence_refs or []),
            next_recommendations=list(next_recommendations or []),
        )

    @classmethod
    def failed(cls, skill_name: str, error_code: str, message: str = "") -> SkillResult:
        return cls(
            skill_name=skill_name,
            status="FAILED",
            error_code=error_code,
            message=message,
        )

    @classmethod
    def blocked(cls, skill_name: str, error_code: str, message: str = "") -> SkillResult:
        return cls(
            skill_name=skill_name,
            status="BLOCKED",
            error_code=error_code,
            message=message,
        )

    @classmethod
    def partial(
        cls,
        skill_name: str,
        *,
        artifact_refs: list[str] | None = None,
        error_code: str = "",
        message: str = "",
        next_recommendations: list[str] | None = None,
    ) -> SkillResult:
        return cls(
            skill_name=skill_name,
            status="PARTIAL",
            artifact_refs=list(artifact_refs or []),
            error_code=error_code,
            message=message,
            next_recommendations=list(next_recommendations or []),
        )


@runtime_checkable
class SkillExecutor(Protocol):
    """Skill Executor 协议（计划 §12）。

    实现方必须提供 `name`（与 manifest.name 一致）与 `execute`。
    """

    name: str

    async def execute(
        self,
        request: SkillRequest,
        context: SkillExecutionContext,
    ) -> SkillResult: ...


class SkillManifest(BaseModel):
    """可执行 Skill 清单 V3（计划 §13 / §81）。

    与文件扫描类 `agent.skill_registry.SkillManifest` 互补：本结构是由
    ExecutableSkillRegistry 管理、可执行、被 SkillRuntime 校验的代码内清单。
    服务端白名单（INTENT_SKILLS）仍由 orchestrator 层决定。
    """

    model_config = {"frozen": True, "extra": "forbid"}

    name: str = Field(..., pattern=r"^[a-z0-9-]+$")
    version: str = Field(..., pattern=r"^\d+\.\d+(?:\.\d+)?$")
    description: str = Field(..., min_length=1)
    purpose: str = Field(default="")
    required_tools: tuple[str, ...] = Field(default_factory=tuple)
    preconditions: tuple[str, ...] = Field(default_factory=tuple)
    postconditions: tuple[str, ...] = Field(default_factory=tuple)
    prohibited_actions: tuple[str, ...] = Field(default_factory=tuple)
    risk_level: str = Field(default="low")  # low / medium / high
    required_scopes: frozenset[str] = Field(default_factory=frozenset)
    max_tool_calls: int = Field(default=20, ge=1)
    output_artifact_type: str = Field(default="")
    status: str = Field(default="published")


# 前向引用（避免循环导入）：SkillExecutionContext 定义在 skills/context.py
if False:  # pragma: no cover - 仅供类型标注，不真正导入
    from agent.skills.context import SkillExecutionContext

__all__ = [
    "SkillBudget",
    "SkillExecutor",
    "SkillManifest",
    "SkillRequest",
    "SkillResult",
    "SkillResultStatus",
]
