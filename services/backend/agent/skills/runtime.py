"""SkillRuntime - 标准 Skill 执行链路（计划 §14）。

链路：Resolve Manifest → Validate Status/Scope → Reserve Budget →
Trace Span → Execute Skill → Validate Output → Validate Postconditions →
Persist Artifacts → Emit skill_completed。

安全不变量（§12 / §51）：
  - Skill 只能调用 manifest.required_tools 内 Tool（context 强制白名单）
  - Skill 不得绕过 Tool Harness：context.tool_executor 是唯一 Tool 出口
  - Budget 超限 / Scope 不足 / 未发布 Skill → 显式 BLOCKED/FAILED，无静默回退
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from agent.skills.context import SkillBudgetExceeded, SkillExecutionContext, SkillToolNotAllowed
from agent.skills.contracts import SkillBudget, SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry, SkillExecutionError


class SkillRuntime:
    def __init__(
        self,
        registry: ExecutableSkillRegistry,
        *,
        tool_executor: Any,
        artifact_store: Any,
        trace_emitter: Any | None = None,
        default_adapter: str = "production",
    ):
        self.registry = registry
        self.tool_executor = tool_executor
        self.artifact_store = artifact_store
        self.trace_emitter = trace_emitter
        self.default_adapter = default_adapter

    # ── 公开入口 ──────────────────────────────────────────

    async def execute(
        self,
        request: SkillRequest,
        *,
        scopes: frozenset[str] | set[str] | None = None,
        adapter: str | None = None,
    ) -> SkillResult:
        manifest = self._resolve(request.skill_name)
        scope_error = self._validate_scope(manifest, scopes or frozenset())
        if scope_error:
            return SkillResult.blocked(request.skill_name, "scope_insufficient", scope_error)

        budget = self._skill_budget(manifest, request)
        budgeted = request.model_copy(update={"budget": budget})

        context = SkillExecutionContext(
            budgeted,
            tool_executor=self.tool_executor,
            artifact_store=self.artifact_store,
            allowed_tools=manifest.required_tools,
            scopes=frozenset(scopes) if scopes is not None else frozenset(),
            adapter=adapter or self.default_adapter,
            trace_emitter=self.trace_emitter,
        )
        executor = self.registry.get(request.skill_name)
        self._emit(request, "skill_started", manifest=manifest.name, version=manifest.version)

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                executor.execute(budgeted, context),
                timeout=budget.max_runtime_seconds + 5,
            )
        except TimeoutError:
            return self._fail(request, "timeout", "skill timed out")
        except SkillToolNotAllowed as exc:
            return self._block(request, "undeclared_tool", str(exc))
        except SkillBudgetExceeded as exc:
            return self._block(request, "budget_exceeded", str(exc))
        except SkillExecutionError as exc:
            return self._fail(request, exc.code, str(exc))
        except Exception as exc:  # Skill 内部未知错误 → FAILED，无静默回退
            return self._fail(request, "internal_error", f"{type(exc).__name__}: {exc!s}")

        result = self._validate_output(request, result)
        self._emit(
            request,
            "skill_completed",
            status=result.status,
            latency_ms=(time.monotonic() - start) * 1000,
            tool_calls=context.tool_call_count,
            artifact_refs=result.artifact_refs,
        )
        return result

    # ── 内部链路 ──────────────────────────────────────────

    def _resolve(self, skill_name: str) -> SkillManifest:
        if skill_name not in self.registry:
            raise SkillExecutionError("unknown_skill", f"unknown skill: {skill_name}")
        manifest = self.registry.get_manifest(skill_name)
        if manifest.status != "published":
            raise SkillExecutionError("unpublished_skill", f"skill not published: {skill_name}")
        return manifest

    def _validate_scope(self, manifest: SkillManifest, scopes: frozenset[str] | set[str]) -> str:
        """要求调用方 scopes ⊇ 该 Skill 的 required_scopes（§52 / §44 Maintainer 最小权限）。"""
        missing = sorted(set(manifest.required_scopes) - set(scopes))
        if missing:
            return f"skill '{manifest.name}' 需要 scope {missing}，缺失: {sorted(scopes)}"
        return ""

    def _skill_budget(self, manifest: SkillManifest, request: SkillRequest) -> SkillBudget:
        # manifest.max_tool_calls 是对该 Skill 的 Tool 上限，叠加在请求预算上
        max_tool = min(request.budget.max_tool_calls, manifest.max_tool_calls)
        return request.budget.model_copy(update={"max_tool_calls": max_tool})

    def _validate_output(self, request: SkillRequest, result: SkillResult) -> SkillResult:
        if result.skill_name != request.skill_name:
            return SkillResult.failed(
                request.skill_name,
                "wrong_skill_result",
                f"executor 返回 skill_name={result.skill_name}，应为 {request.skill_name}",
            )
        if result.status == "SUCCEEDED" and not result.artifact_refs:
            # SUCCEEDED 至少要产出 artifact（产物契约 §18）
            return SkillResult.partial(
                request.skill_name,
                error_code="no_artifact",
                message="SUCCEEDED 状态需要至少一个 artifact_ref",
            )
        return result

    def _emit(self, request: SkillRequest, event_type: str, **fields: Any) -> None:
        if self.trace_emitter is None:
            return
        with suppress(Exception):  # Trace 失败不影响主流程
            self.trace_emitter(
                event_type=f"skill.{event_type}",
                skill=request.skill_name,
                run_id=request.run_id,
                trace_id=request.trace_id,
                user_id=request.user_id,
                **fields,
            )

    def _fail(self, request: SkillRequest, code: str, message: str) -> SkillResult:
        self._emit(request, "skill_failed", error_code=code, message=message)
        return SkillResult.failed(request.skill_name, code, message)

    def _block(self, request: SkillRequest, code: str, message: str) -> SkillResult:
        self._emit(request, "skill_blocked", error_code=code, message=message)
        return SkillResult.blocked(request.skill_name, code, message)
