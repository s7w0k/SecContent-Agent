"""AutonomousRunService — 阶段四 4A Step 4A-9。

自主运行编排服务（API 层复用）：
  - create_run：构造 RuntimeState + 冻结运行预算，写入 runtime_runs；
  - start_run：后台 asyncio 任务执行 AgentRuntime 受控状态机；
  - get_run / list_runs：读取运行（多租户隔离，user_id 必填）；
  - cancel_run / resume_run：取消与恢复；
  - approve / reject：人工审批（一次性授权，参数变化失效）；
  - events：运行事件流（SSE 断线续传由 API 层实现）。

写入均为脱敏数据；运行时事件只记录事件类型/状态/原因码/指纹，不落正文。

默认（demo）planner/executor 为确定性实现，保证无外部依赖可跑通端到端；
生产环境通过 planner_factory / executor_factory 注入真实 Worker/LLM 实现。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent.agent_runtime import AgentRuntime, PlannedAction
from agent.goal_validator import GoalValidator
from agent.model_router import ModelRouter
from agent.policy_engine import ApprovalService, PolicyEngine
from agent.runtime_events import RuntimeEventStore
from agent.runtime_state import (
    BudgetUsage,
    RunBudget,
    RuntimeState,
    RuntimeStatus,
)
from agent.runtime_store import RuntimeStateStore
from pydantic import BaseModel

logger = logging.getLogger("backend.agent.autonomous_service")

# 默认演示工具链（L0/L1：读取 → 分类 → 打分 → 导出草稿）
DEFAULT_TOOL_CHAIN = [
    "retrieve_articles",
    "classify_articles",
    "score_articles",
    "export_articles_csv",
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ═══════════════════════════════════════════════════════════════
# 默认（demo）planner / executor —— 确定性、可测
# ═══════════════════════════════════════════════════════════════


class DemoPlanner:
    """顺序执行 pending_steps 的确定性规划器（服务端权威，不可被输入绕过）。"""

    def __init__(self, *, chain: list[str] | None = None):
        self.chain = list(chain or DEFAULT_TOOL_CHAIN)

    async def __call__(self, state: RuntimeState) -> PlannedAction | None:
        if len(state.completed_steps) >= len(self.chain):
            return None
        next_tool = self.chain[len(state.completed_steps)]
        return PlannedAction(
            step_id=f"step-{len(state.completed_steps) + 1}",
            tool_name=next_tool,
            args={}
            if next_tool != "export_articles_csv"
            else {"idempotency_key": f"ik-{state.run_id}-export"},
        )


class DemoExecutor:
    """确定性执行器：按工具名返回结构化结果（含证据，驱动端到端演示）。"""

    async def __call__(
        self, state: RuntimeState, action: PlannedAction, meta: dict[str, Any]
    ) -> dict[str, Any]:
        if action.tool_name == "export_articles_csv":
            return {
                "ok": True,
                "evidence": [{"acceptance_index": 0}],
                "result_hash": f"sha256:demo-export-{len(state.completed_steps)}",
                "duration_ms": 5,
            }
        return {
            "ok": True,
            "result_hash": f"sha256:demo-{action.tool_name}-{len(state.completed_steps)}",
            "duration_ms": 3,
        }


# ═══════════════════════════════════════════════════════════════
# 自主运行服务
# ═══════════════════════════════════════════════════════════════


class CreateRunRequest(BaseModel):
    goal: str
    acceptance_criteria: list[str]
    thread_id: str = ""
    trace_id: str = ""
    tool_chain: list[str] | None = None  # 覆盖默认工具链（服务端校验白名单）
    max_steps: int | None = None  # 覆盖预算（0 = 使用默认）


class AutonomousRunService:
    """自主运行编排：创建 → 后台执行 → 查询/取消/恢复/审批/事件。"""

    def __init__(
        self,
        *,
        store: RuntimeStateStore,
        event_store: RuntimeEventStore,
        policy: PolicyEngine | None = None,
        approval_service: ApprovalService | None = None,
        model_router: ModelRouter | None = None,
        settings: Any | None = None,
        db: Any = None,
        planner_factory: Callable[[RuntimeState], Any] | None = None,
        executor_factory: Callable[[RuntimeState], Any] | None = None,
    ):
        self.store = store
        self.event_store = event_store
        self.db = db
        self.policy = policy or PolicyEngine()
        self.settings = settings
        self.model_router = model_router
        self.approval_service = approval_service or ApprovalService(db=db)
        self.planner_factory = planner_factory
        self.executor_factory = executor_factory
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._default_budget = (
            RunBudget.from_settings(settings)
            if settings is not None and hasattr(settings, "AUTONOMOUS_MAX_STEPS")
            else RunBudget()
        )

    # ── 创建 / 读取 ──────────────────────────────────────────

    async def create_run(
        self,
        *,
        user_id: str,
        goal: str,
        acceptance_criteria: list[str],
        thread_id: str = "",
        trace_id: str = "",
        tool_chain: list[str] | None = None,
        max_steps: int | None = None,
    ) -> RuntimeState:
        chain = tool_chain or DEFAULT_TOOL_CHAIN
        # 服务端校验：仅允许规则表中存在的工具（安全优先）
        chain = [t for t in chain if t in self.policy.rules]
        if not chain:
            raise ValueError("tool_chain contains no allowed tools")
        budget = self._default_budget
        if max_steps is not None and max_steps > 0:
            budget = budget.model_copy(update={"max_steps": max_steps})
        state = RuntimeState(
            run_id="run-" + uuid.uuid4().hex[:12],
            thread_id=thread_id,
            trace_id=trace_id,
            user_id=user_id,
            goal=goal[:2000],
            acceptance_criteria=[c[:500] for c in acceptance_criteria],
            budget=budget,
            usage=BudgetUsage(),
            pending_steps=list(chain),
            status=RuntimeStatus.PENDING,
        )
        await self.store.save(state)
        await self.event_store.append(
            run_id=state.run_id,
            event_type="run_created",
            status=state.status.value,
            payload={
                "goal_len": len(state.goal),
                "criteria": len(state.acceptance_criteria),
                "chain": list(chain),
            },
        )
        return state

    async def get_run(self, run_id: str, user_id: str) -> RuntimeState | None:
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return None
        return state

    async def list_runs(
        self, user_id: str, *, status: str = "", limit: int = 50
    ) -> list[RuntimeState]:
        return await self.store.list_runs(user_id=user_id, status=status, limit=limit)

    # ── 启动 / 取消 / 恢复 ────────────────────────────────────

    async def start_run(self, run_id: str, user_id: str) -> bool:
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return False
        if state.is_terminal or state.status == RuntimeStatus.RUNNING:
            return False
        if run_id in self._tasks:
            return False
        cancel_event = asyncio.Event()
        self._cancel_events[run_id] = cancel_event
        task = asyncio.create_task(self._execute(state, cancel_event), name=f"autonomous-{run_id}")
        self._tasks[run_id] = task
        return True

    async def cancel_run(self, run_id: str, user_id: str) -> bool:
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return False
        # 先置内存取消事件（运行时安全点立即感知），再持久化 cancel_requested
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        return await self.store.request_cancel(run_id, reason="canceled by user")

    async def resume_run(self, run_id: str, user_id: str) -> bool:
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return False
        if state.status != RuntimeStatus.WAITING_APPROVAL:
            return False
        return await self.start_run(run_id, user_id)

    # ── 人工审批 ─────────────────────────────────────────────

    async def approve(self, approval_id: str, user_id: str) -> RuntimeState | None:
        """审批通过：一次性授权进入 state.approved_tokens，供后续消费。"""
        run_id = await self._approval_run_id(approval_id)
        if run_id is None:
            return None
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return None
        pending = next(
            (a for a in state.approval_state.pending_approvals if a.approval_id == approval_id),
            None,
        )
        if pending is None:
            return None
        approved = await self.approval_service.approve(pending, approver=user_id)
        if approved.status != "approved":
            return None
        new_state = state.model_copy(
            update={
                "approval_state": state.approval_state.model_copy(
                    update={
                        "pending_approvals": [
                            a if a.approval_id != approval_id else approved
                            for a in state.approval_state.pending_approvals
                        ],
                        "approved_tokens": [
                            *state.approval_state.approved_tokens,
                            approved.one_time_token,
                        ],
                    }
                )
            }
        )
        await self.store.save(new_state)
        await self.event_store.append(
            run_id=run_id,
            event_type="approval_approved",
            status=new_state.status.value,
            payload={"approval_id": approval_id, "approver": user_id},
        )
        return new_state

    async def reject(self, approval_id: str, user_id: str) -> RuntimeState | None:
        run_id = await self._approval_run_id(approval_id)
        if run_id is None:
            return None
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return None
        pending = next(
            (a for a in state.approval_state.pending_approvals if a.approval_id == approval_id),
            None,
        )
        if pending is None:
            return None
        rejected = await self.approval_service.reject(
            pending, approver=user_id, reason="rejected by user"
        )
        new_state = state.model_copy(
            update={
                "approval_state": state.approval_state.model_copy(
                    update={
                        "pending_approvals": [
                            a if a.approval_id != approval_id else rejected
                            for a in state.approval_state.pending_approvals
                        ]
                    }
                )
            }
        )
        await self.store.save(new_state)
        await self.event_store.append(
            run_id=run_id,
            event_type="approval_rejected",
            status=new_state.status.value,
            payload={"approval_id": approval_id, "approver": user_id},
        )
        return new_state

    async def _approval_run_id(self, approval_id: str) -> str | None:
        if self.db is not None:
            try:
                doc = await self.db["runtime_approvals"].find_one({"approval_id": approval_id})
                if doc is not None and doc.get("run_id"):
                    return str(doc["run_id"])
            except Exception:
                logger.warning("[autonomous] read approval failed: %s", approval_id)
        # fallback：扫描最近运行状态中的待审批
        for state in await self.store.list_runs(limit=100):
            if any(a.approval_id == approval_id for a in state.approval_state.pending_approvals):
                return state.run_id
        return None

    # ── 事件 ─────────────────────────────────────────────────

    async def events(
        self, run_id: str, user_id: str, *, last_sequence: int = 0, limit: int = 500
    ) -> list[Any]:
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return []
        return await self.event_store.read_after_sequence(run_id, last_sequence, limit=limit)

    # ── 内部 ─────────────────────────────────────────────────

    def _build_runtime(self, state: RuntimeState) -> AgentRuntime:
        planner = (
            self.planner_factory(state)
            if self.planner_factory
            else DemoPlanner(chain=state.pending_steps)
        )
        executor = self.executor_factory(state) if self.executor_factory else DemoExecutor()

        async def _checkpoint(s: RuntimeState) -> None:
            await self.store.save(s)

        return AgentRuntime(
            planner=planner,
            executor=executor,
            policy=self.policy,
            approval_service=self.approval_service,
            goal_validator=GoalValidator(
                required_artifact_keys=(), high_risk_requires_confirm=False
            ),
            model_router=self.model_router,
            checkpointer=_checkpoint,
            event_emitter=self._emit_event,
            max_retries=getattr(self._default_budget, "max_retries", 2),
            backoff_jitter=0.0,
        )

    async def _emit_event(
        self, event_type: str, state: RuntimeState, payload: dict[str, Any]
    ) -> None:
        await self.event_store.append(
            run_id=state.run_id, event_type=event_type, status=state.status.value, payload=payload
        )

    async def _execute(
        self, state: RuntimeState, cancel_event: asyncio.Event | None = None
    ) -> None:
        runtime = self._build_runtime(state)
        try:
            result = await runtime.run(state, cancel_event=cancel_event)
            await self.store.save(result.final_state)
            logger.info(
                "[autonomous] run %s finished: %s (%s)",
                state.run_id,
                result.status.value,
                result.reason_code,
            )
        except asyncio.CancelledError:
            logger.info("[autonomous] run %s task canceled", state.run_id)
        except Exception:
            logger.exception("[autonomous] run %s failed unexpectedly", state.run_id)
        finally:
            self._tasks.pop(state.run_id, None)
            self._cancel_events.pop(state.run_id, None)

    async def shutdown(self) -> None:
        """取消所有进行中的后台运行（应用关闭时调用）。"""
        for event in self._cancel_events.values():
            event.set()
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()
