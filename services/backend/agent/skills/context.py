"""SkillExecutionContext - Skill 执行的受限运行上下文（计划 §12 / §15）。

职责：
  - 通过注入的 BusinessToolExecutor 统一执行 Tool（禁止 Skill 直接 import 业务服务）
  - 执行 Tool 白名单校验（只允许 manifest.required_tools 内的 Tool）
  - 执行 SkillBudget 记账（tool_calls / llm_calls / runtime_seconds）
  - 提供 Artifact Store 写入（Artifact-based Handoff，§18 / §46）

Skill 禁止直接访问 Mongo / 第三方 HTTP / Wiki 内部类，一律经 Tool 边界。
"""

from __future__ import annotations

import time
from contextlib import suppress
from typing import Any, Protocol

from agent.business_tools.contracts import ToolRequestContext
from agent.skills.contracts import SkillBudget, SkillRequest


class SkillContextError(RuntimeError):
    """Skill 上下文不变量违反。"""


class SkillToolNotAllowed(SkillContextError):  # noqa: N818
    """Skill 尝试调用未声明 Tool（计划 §12 禁止项）。"""


class SkillBudgetExceeded(SkillContextError):  # noqa: N818
    """Skill 预算耗尽。"""


class ArtifactStoreProtocol(Protocol):
    """Artifact Store 最小写入接口（real 实现见 agent/artifacts/store.py）。

    Skill 只写不读：把产物（EvidenceBundleArtifact/ScoringArtifact/DraftArtifact）
    落库后回传 artifact_id + ref，供上层通过 ArtifactRef 交接。
    """

    async def put(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        producer: str,
        run_id: str,
        step_id: str,
        parent_ref: str | None = None,
    ) -> dict[str, Any]:
        """写入并返回 artifact 记录（含 artifact_id / version / ref / content_hash）。"""
        ...


class SkillExecutionContext:
    """不可序列化运行时上下文：Skill 通过它访问受限 Tool 与 Artifact Store。"""

    def __init__(
        self,
        request: SkillRequest,
        *,
        tool_executor: Any,
        artifact_store: ArtifactStoreProtocol,
        allowed_tools: list[str] | tuple[str, ...],
        scopes: frozenset[str] | None = None,
        adapter: str = "production",
        trace_emitter: Any | None = None,
    ):
        self.request = request
        self.budget: SkillBudget = request.budget
        self.tool_executor = tool_executor
        self.artifact_store = artifact_store
        self.allowed_tools = frozenset(allowed_tools)
        self.scopes = frozenset(scopes) if scopes is not None else frozenset()
        self.adapter = adapter
        self.trace_emitter = trace_emitter  # 可选 event 发射器（skill_completed 等）
        self._tool_calls: list[dict[str, Any]] = []
        self._llm_calls = 0
        self._started_at = time.monotonic()

    # ── 预算 / 账本 ────────────────────────────────────────

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return list(self._tool_calls)

    @property
    def tool_call_count(self) -> int:
        return len(self._tool_calls)

    @property
    def llm_call_count(self) -> int:
        return self._llm_calls

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def _check_runtime_budget(self) -> None:
        if self.tool_call_count >= self.budget.max_tool_calls:
            raise SkillBudgetExceeded(
                f"tool budget exhausted: {self.tool_call_count}>={self.budget.max_tool_calls}"
            )
        if self._llm_calls >= self.budget.max_llm_calls:
            raise SkillBudgetExceeded(
                f"llm budget exhausted: {self._llm_calls}>={self.budget.max_llm_calls}"
            )
        if self.elapsed_seconds >= self.budget.max_runtime_seconds:
            raise SkillBudgetExceeded(f"runtime budget exhausted: {self.elapsed_seconds:.1f}s")

    def record_llm_call(self) -> None:
        """LLM 调用前登记（raise on budget）。由 Skill 内 Judge/Wrapper 调用。"""
        self._check_runtime_budget()
        self._llm_calls += 1

    # ── Tool 执行（统一经 BusinessToolExecutor）────────────

    def _tool_request_context(self) -> ToolRequestContext:
        return ToolRequestContext(
            user_id=self.request.user_id,
            tenant_id=self.request.tenant_id,
            scopes=self.scopes,
            run_id=self.request.run_id,
            turn_id=self.request.trace_id,
        )

    async def execute_tool(
        self,
        name: str,
        args: dict[str, Any] | Any,
        *,
        adapter: str | None = None,
    ) -> Any:
        """执行一个 Tool。未声明 Tool 立即拒绝（即使 executor 有该能力）。"""
        if name not in self.allowed_tools:
            raise SkillToolNotAllowed(
                f"skill '{self.request.skill_name}' 调用未声明 Tool '{name}'；"
                f"允许列表: {sorted(self.allowed_tools)}"
            )
        self._check_runtime_budget()
        result = await self.tool_executor.invoke(
            name,
            args,
            context=self._tool_request_context(),
            adapter=adapter or self.adapter,
        )
        self._tool_calls.append({"name": name, "args": args})
        return result

    # ── Artifact Store（§18 / §46 Artifact-based Handoff）────

    async def store_artifact(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        producer: str,
        step_id: str = "",
        parent_ref: str | None = None,
    ) -> dict[str, Any]:
        """把产物落库，返回 artifact 记录（含 ref）。"""
        return await self.artifact_store.put(
            artifact_type=artifact_type,
            payload=payload,
            producer=producer,
            run_id=self.request.run_id,
            step_id=step_id,
            parent_ref=parent_ref,
            tenant_id=self.request.tenant_id,
            user_id=self.request.user_id,
        )

    def emit_trace(self, event_type: str, **fields: Any) -> None:
        """可选结构化 Trace（计划 §48 / §70）。"""
        if self.trace_emitter is not None:
            with suppress(Exception):  # Trace 失败不影响 Skill 主流程
                self.trace_emitter(
                    skill=self.request.skill_name,
                    run_id=self.request.run_id,
                    trace_id=self.request.trace_id,
                    event_type=event_type,
                    **fields,
                )
