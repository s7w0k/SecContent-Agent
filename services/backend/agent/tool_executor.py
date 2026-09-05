"""同轮多工具并发执行器 — 阶段1 1.2 节（WBS 1.2）。

执行规则：
  1. 对所有 tool calls 先完成工具名、参数 Schema、策略和预算验证；
  2. 按 dependency group 划分可并行与串行调用；
  3. 使用 asyncio.gather(return_exceptions=True) 真实并发（等价于 TaskGroup，
     但一个工具失败不会取消允许部分成功的兄弟工具）；
  4. 实施全局 / 租户 / provider / 工具四层 semaphore；
  5. 同轮结果按原 tool call 顺序回填；
  6. 一个工具失败不得取消允许部分成功的兄弟工具；
  7. 超过剩余工具预算的调用不得执行。

安全约束：参数校验失败 / 策略拒绝的调用不执行；不记录参数原文，只记录 hash。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent.agent_contracts import ToolPermission, ToolPolicy, TypedToolResult
from agent.budget_manager import ConcurrencyLimiter
from agent.loop_detector import LoopDetector

logger = logging.getLogger("backend.agent.tool_executor")

# 默认 provider（用于 provider 层 semaphore key）
DEFAULT_PROVIDER = "deepseek"


# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolCallSpec:
    """一次验证后的工具调用。"""

    tool_name: str
    tool_call_id: str
    args: dict[str, Any]
    args_hash: str = ""


@dataclass
class ToolExecutionResult:
    """单个工具执行结果（按原顺序回填）。"""

    tool_call_id: str
    tool_name: str
    ok: bool
    message: str  # ToolMessage content（模型可见）
    result_hash: str = ""
    source_ids: list[str] = field(default_factory=list)
    duration_ms: int = 0
    error_code: str = ""
    truncated: bool = False
    char_count: int = 0
    args_hash: str = ""

    @classmethod
    def blocked(
        cls, *, tool_call_id: str, tool_name: str, error_code: str, message: str
    ) -> ToolExecutionResult:
        return cls(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            ok=False,
            message=message,
            error_code=error_code,
        )


class ToolValidationError(Exception):
    """工具验证失败（不执行）。"""


# ═══════════════════════════════════════════════════════════════
# ToolExecutor
# ═══════════════════════════════════════════════════════════════


class ToolExecutor:
    """同轮多工具并发执行器（单 run 实例）。

    Args:
        tools_by_name: {工具名: 工具对象}（langchain @tool 或 mock）
        tool_policies: {工具名: ToolPolicy}
        max_parallel_tools: run 级全局并发上限
        limiter: 四层并发限制器（可选，不传则自建）
        run_context: RunContext（策略白名单校验，可选）
        tenant_id / provider: 租户 / provider 层 semaphore key
        detector: LoopDetector（可选，执行时同步观察动作）
        budget_reserve: 可选的预留回调 callable(spec) -> 预留对象或 None
            用于在工具执行前预留工具级预算；返回 None 表示预算不足不执行。
        on_event: 事件回调（tool_started / tool_finished / tool_failed）
    """

    def __init__(
        self,
        *,
        tools_by_name: dict[str, Any],
        tool_policies: dict[str, ToolPolicy] | None = None,
        max_parallel_tools: int = 3,
        limiter: ConcurrencyLimiter | None = None,
        run_context: Any = None,
        tenant_id: str = "",
        provider: str = DEFAULT_PROVIDER,
        detector: LoopDetector | None = None,
        budget_reserve: Any = None,
        on_event: Any = None,
    ) -> None:
        self.tools_by_name = tools_by_name
        self.tool_policies = tool_policies or {}
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.limiter = limiter or ConcurrencyLimiter()
        self.run_context = run_context
        self.tenant_id = tenant_id
        self.provider = provider
        self.detector = detector
        self.budget_reserve = budget_reserve
        self.on_event = on_event
        self._reservations: dict[str, Any] = {}

        # 注册四层 semaphore 上限
        self.limiter.set_limit("global", self.max_parallel_tools)
        if tenant_id:
            self.limiter.set_limit(f"tenant:{tenant_id}", max(1, self.max_parallel_tools))
        self.limiter.set_limit(f"provider:{provider}", max(1, self.max_parallel_tools * 2))

    # ── 对外接口 ──────────────────────────────────────────

    async def execute_many(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        remaining_tool_budget: int = -1,
    ) -> list[ToolExecutionResult]:
        """验证并并发执行同轮工具调用。

        Args:
            tool_calls: LLM 返回的 tool_calls 列表
                （[{name, id, args, ...}]，顺序为 LLM 原始顺序）
            remaining_tool_budget: 剩余工具调用预算；>=0 时超出部分不执行

        Returns:
            按原始顺序排列的 ToolExecutionResult 列表
        """
        if not tool_calls:
            return []

        # 1. 验证全部调用
        specs: list[ToolCallSpec] = []
        results: list[ToolExecutionResult | None] = [None] * len(tool_calls)
        budget_left = max(0, remaining_tool_budget) if remaining_tool_budget >= 0 else float("inf")

        for idx, tc in enumerate(tool_calls):
            name = str(tc.get("name", ""))
            call_id = str(tc.get("id", ""))
            args = tc.get("args", {}) or {}
            args_hash = self._args_hash(args)

            if idx >= budget_left:
                results[idx] = ToolExecutionResult.blocked(
                    tool_call_id=call_id,
                    tool_name=name,
                    error_code="budget_exhausted",
                    message=f"[工具预算不足] 剩余工具调用预算 {int(budget_left)} 次，本次调用不执行",
                )
                continue

            try:
                spec = self._validate(name=name, call_id=call_id, args=args, args_hash=args_hash)
            except ToolValidationError as exc:
                error_code = exc.args[0] if exc.args else "validation_failed"
                results[idx] = ToolExecutionResult.blocked(
                    tool_call_id=call_id,
                    tool_name=name,
                    error_code=error_code,
                    message=f"[{error_code}] {name} 未执行",
                )
                continue

            # 工具级预算预留（返回 None = 预算不足）
            if self.budget_reserve is not None:
                reservation = self.budget_reserve(spec)
                if reservation is None:
                    results[idx] = ToolExecutionResult.blocked(
                        tool_call_id=call_id,
                        tool_name=name,
                        error_code="budget_exhausted",
                        message=f"[工具预算不足] {name} 未预留到预算，不执行",
                    )
                    continue
                self._reservations[call_id] = reservation
            specs.append(spec)

        if not specs:
            return [r for r in results if r is not None]

        # 2. 依赖分组（拓扑分层）
        groups = self._dependency_groups(specs)

        # 3. 按组顺序执行（组间串行，组内并发）
        executed: dict[str, ToolExecutionResult] = {}
        for group in groups:
            group_results = await self._run_group(group)
            for res in group_results:
                executed[res.tool_call_id] = res

        # 5. 按原顺序回填
        for idx, tc in enumerate(tool_calls):
            if results[idx] is None:
                call_id = str(tc.get("id", ""))
                results[idx] = executed.get(call_id)
        return [r for r in results if r is not None]

    # ── 验证 ──────────────────────────────────────────────

    def _validate(self, *, name: str, call_id: str, args: dict, args_hash: str) -> ToolCallSpec:
        """工具名 / 参数 Schema / 策略验证。失败抛 ToolValidationError。"""
        tool = self.tools_by_name.get(name)
        if tool is None:
            raise ToolValidationError("tool_not_found")

        # 参数 Schema 验证（langchain @tool 提供 args_schema；mock 可能没有）
        schema = getattr(tool, "args_schema", None)
        if schema is not None:
            try:
                schema.model_validate(args)
            except Exception as exc:
                logger.warning("[tool-executor] %s args schema invalid: %s", name, exc)
                raise ToolValidationError("contract_error") from exc

        # 策略验证
        policy = self.tool_policies.get(name)
        if policy is not None:
            permission = self._check_policy(policy, args)
            if permission != ToolPermission.ALLOWED:
                raise ToolValidationError(f"policy_denied:{permission.value}")

        return ToolCallSpec(
            tool_name=name,
            tool_call_id=call_id,
            args=args,
            args_hash=args_hash,
        )

    def _check_policy(self, policy: ToolPolicy, args: dict[str, Any]) -> ToolPermission:
        """工具策略检查（白名单类由工具内部执行，此处只做策略层面校验）。"""
        if policy.requires_product_allowlist:
            product_id = args.get("product_id")
            if product_id and not self._product_allowed(product_id):
                return ToolPermission.DENIED_POLICY
        if policy.requires_article_allowlist:
            url_hash = args.get("url_hash")
            if url_hash and not self._article_allowed(url_hash):
                return ToolPermission.DENIED_POLICY
        return ToolPermission.ALLOWED

    def _product_allowed(self, product_id: str) -> bool:
        run_context = self.run_context
        if run_context is None or not hasattr(run_context, "is_product_allowed"):
            return True
        return run_context.is_product_allowed(product_id)

    def _article_allowed(self, url_hash: str) -> bool:
        run_context = self.run_context
        if run_context is None or not hasattr(run_context, "is_article_allowed"):
            return True
        return run_context.is_article_allowed(url_hash)

    def take_reservation(self, tool_call_id: str) -> Any | None:
        """取走一次工具预算预留（供 Loop 结算后使用）。"""
        return self._reservations.pop(tool_call_id, None)

    # ── 依赖分组 ──────────────────────────────────────────

    def _dependency_groups(self, specs: list[ToolCallSpec]) -> list[list[ToolCallSpec]]:
        """按工具策略中的 depends_on 声明做拓扑分层。

        默认（无依赖声明）所有调用同一层并发执行。
        Returns:
            [[...], [...]] 组间串行（依赖先执行），组内可并发。
        """
        depends: dict[str, list[str]] = {}
        for spec in specs:
            policy = self.tool_policies.get(spec.tool_name)
            deps = tuple(getattr(policy, "depends_on", ()) or ())
            depends[spec.tool_name] = list(deps)

        groups: list[list[ToolCallSpec]] = []
        remaining = list(specs)
        while remaining:
            ready = [
                s
                for s in remaining
                if all(
                    dep in {g.tool_name for grp in groups for g in grp}
                    for dep in depends[s.tool_name]
                )
            ]
            if not ready:
                # 循环依赖兜底：剩余全部放入同一组
                groups.append(remaining)
                break
            groups.append(ready)
            remaining = [s for s in remaining if s not in ready]
        return groups

    # ── 组内并发执行 ──────────────────────────────────────

    async def _run_group(self, group: list[ToolCallSpec]) -> list[ToolExecutionResult]:
        """组内真实并发执行；一个失败不取消兄弟工具。"""
        # 全局 + 租户 + provider + 工具 四层 semaphore
        global_sem = self.limiter.semaphore("global")
        tenant_sem = self.limiter.semaphore(f"tenant:{self.tenant_id}") if self.tenant_id else None
        provider_sem = self.limiter.semaphore(f"provider:{self.provider}")
        tool_sems = {
            name: self.limiter.semaphore(f"tool:{name}") for name in {s.tool_name for s in group}
        }

        async def run_one(spec: ToolCallSpec) -> ToolExecutionResult:
            if global_sem:
                await global_sem.acquire()
            if tenant_sem:
                await tenant_sem.acquire()
            if provider_sem:
                await provider_sem.acquire()
            tool_sem = tool_sems.get(spec.tool_name)
            if tool_sem:
                await tool_sem.acquire()
            try:
                return await self._execute(spec)
            finally:
                if tool_sem:
                    tool_sem.release()
                if provider_sem:
                    provider_sem.release()
                if tenant_sem:
                    tenant_sem.release()
                if global_sem:
                    global_sem.release()

        raw = await asyncio.gather(*(run_one(s) for s in group), return_exceptions=True)
        results: list[ToolExecutionResult] = []
        for spec, outcome in zip(group, raw, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, Exception):
                logger.warning(
                    "[tool-executor] %s raised %s: %s",
                    spec.tool_name,
                    type(outcome).__name__,
                    str(outcome)[:100],
                )
                results.append(
                    ToolExecutionResult(
                        tool_call_id=spec.tool_call_id,
                        tool_name=spec.tool_name,
                        ok=False,
                        message=f"[工具执行失败] {type(outcome).__name__}: {str(outcome)[:100]}",
                        error_code=type(outcome).__name__,
                        args_hash=spec.args_hash,
                    )
                )
            else:
                results.append(outcome)
        return results

    async def _execute(self, spec: ToolCallSpec) -> ToolExecutionResult:
        """执行单个工具（含超时、事件、结果解析）。"""
        self._emit(
            "tool_started",
            {
                "tool_name": spec.tool_name,
                "input_hash": spec.args_hash,
                "step_id": spec.tool_call_id,
            },
        )
        tool = self.tools_by_name[spec.tool_name]
        policy = self.tool_policies.get(spec.tool_name)
        timeout = policy.timeout_seconds if policy and policy.timeout_seconds else 10
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(tool.ainvoke(dict(spec.args)), timeout=timeout)
        except TimeoutError:
            duration = int((time.perf_counter() - started) * 1000)
            self._emit(
                "tool_failed",
                {"tool_name": spec.tool_name, "error_code": "timeout", "duration_ms": duration},
            )
            return ToolExecutionResult(
                tool_call_id=spec.tool_call_id,
                tool_name=spec.tool_name,
                ok=False,
                message=f"[工具超时] {spec.tool_name} 执行超过 {timeout}s",
                error_code="timeout",
                duration_ms=duration,
                args_hash=spec.args_hash,
            )
        except Exception as exc:
            duration = int((time.perf_counter() - started) * 1000)
            self._emit(
                "tool_failed",
                {
                    "tool_name": spec.tool_name,
                    "error_code": type(exc).__name__,
                    "duration_ms": duration,
                },
            )
            return ToolExecutionResult(
                tool_call_id=spec.tool_call_id,
                tool_name=spec.tool_name,
                ok=False,
                message=f"[工具执行失败] {spec.tool_name}: {type(exc).__name__}: {str(exc)[:100]}",
                error_code=type(exc).__name__,
                duration_ms=duration,
                args_hash=spec.args_hash,
            )

        duration = int((time.perf_counter() - started) * 1000)

        # 结果解析：TypedToolResult 结构化结果 或 纯文本
        source_ids: list[str] = []
        result_hash = ""
        truncated = False
        char_count = 0
        if isinstance(raw, TypedToolResult):
            ok = raw.ok
            message = raw.to_tool_message_content()
            source_ids = list(raw.source_ids)
            truncated = raw.truncated
            char_count = raw.char_count
            result_hash = self._result_hash(message)
            error_code = raw.error_code if not raw.ok else ""
        else:
            message = str(raw)
            char_count = len(message)
            ok = not message.startswith("[工具执行失败]") and not message.startswith("[工具超时]")
            result_hash = self._result_hash(message)
            error_code = ""

        if ok:
            self._emit(
                "tool_finished",
                {
                    "tool_name": spec.tool_name,
                    "input_hash": spec.args_hash,
                    "result_hash": result_hash,
                    "source_ids": source_ids,
                    "duration_ms": duration,
                },
            )
        else:
            self._emit(
                "tool_failed",
                {
                    "tool_name": spec.tool_name,
                    "error_code": error_code or "tool_error",
                    "duration_ms": duration,
                },
            )

        # 同步观察动作到 LoopDetector（用于循环检测）
        if self.detector is not None:
            self.detector.observe_action(
                tool_name=spec.tool_name,
                args_hash=spec.args_hash,
                result_hash=result_hash,
                new_evidence_count=len(source_ids),
                error_code=error_code,
            )

        return ToolExecutionResult(
            tool_call_id=spec.tool_call_id,
            tool_name=spec.tool_name,
            ok=ok,
            message=message,
            result_hash=result_hash,
            source_ids=source_ids,
            duration_ms=duration,
            error_code=error_code,
            truncated=truncated,
            char_count=char_count,
            args_hash=spec.args_hash,
        )

    # ── 工具 ──────────────────────────────────────────────

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event_type, payload)
            except Exception:
                logger.warning("[tool-executor] on_event failed for %s", event_type, exc_info=True)

    @staticmethod
    def _args_hash(args: dict[str, Any]) -> str:
        combined = str(sorted(args.items())) if isinstance(args, dict) else str(args)
        return f"sha256:{hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _result_hash(text: str) -> str:
        return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
