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
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent.agent_runtime import AgentRuntime, PlannedAction
from agent.business_tools.execution import BusinessToolAdapterKind
from agent.contracts.task import TaskEnvelope, TaskIntent
from agent.error_taxonomy import ErrorCategory, classify_error
from agent.goal_validator import GoalValidator
from agent.model_router import ModelRouter
from agent.outbox import EventOutbox, OutboxStore
from agent.policy_engine import ApprovalService, PolicyEngine
from agent.recovery_policy import RecoveryContext, RecoveryPolicy
from agent.run_manifest import (
    ExecutionMode,
    RunManifestStore,
    build_run_manifest,
    validate_manifest,
)
from agent.runtime_events import RuntimeEventStore
from agent.runtime_state import (
    BudgetUsage,
    RunBudget,
    RuntimeState,
    RuntimeStatus,
)
from agent.runtime_store import RuntimeStateStore
from agent.slot_merger import merge_task_envelope
from agent.task_understanding import TaskUnderstandingService
from agent.trace import TraceReport, build_trace, load_trace
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
    initial_slots: dict[str, Any] = {}


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
        business_executor: Any = None,
        business_registry: Any = None,
        business_tool_adapter: BusinessToolAdapterKind | str = BusinessToolAdapterKind.PRODUCTION,
        skill_registry: Any = None,
        metrics: Any = None,
    ):
        self.store = store
        self.event_store = event_store
        self.db = db
        self.metrics = metrics  # MetricCollector（可选）：喂生产运行指标
        # Transactional Outbox（阶段3 WBS 3.7）：事件先入 outbox（幂等 dedup），
        # 立即投递到事件存储；投递失败保留 pending 供 reconcile 重试
        self.outbox: EventOutbox | None = None
        if db is not None:
            max_attempts = (
                getattr(settings, "OUTBOX_MAX_ATTEMPTS", 5)
                if settings is not None
                else 5
            )
            self.outbox = EventOutbox(
                store=OutboxStore(db=db, max_attempts=max_attempts),
                deliver=self._deliver_outbox_event,
            )
        self.policy = policy or PolicyEngine()
        self.settings = settings
        self.model_router = model_router
        self.approval_service = approval_service or ApprovalService(db=db)
        self.planner_factory = planner_factory
        self.executor_factory = executor_factory
        self.business_executor = business_executor
        self.business_registry = business_registry
        self.business_tool_adapter = business_tool_adapter
        self.skill_registry = skill_registry
        self.task_understanding = TaskUnderstandingService()
        self.production_mode = business_executor is not None or business_registry is not None
        if self.production_mode and (business_executor is None or business_registry is None):
            raise RuntimeError("production runtime requires business executor and registry")
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._default_budget = (
            RunBudget.from_settings(settings)
            if settings is not None and hasattr(settings, "AUTONOMOUS_MAX_STEPS")
            else RunBudget()
        )

    def _code_revision(self) -> str:
        """部署注入的 Git commit / 镜像 digest；未注入回退 dev-local（满足清单冻结必填）。"""
        return str(getattr(self.settings, "CODE_REVISION", "") or "dev-local")

    def _tool_registry_version(self) -> str:
        """工具契约注册表版本（RunManifest 冻结）。"""
        if self.business_registry is not None:
            return str(self.business_registry.manifest_version)
        return str(getattr(self.settings, "TOOL_REGISTRY_VERSION", "") or "1.0")

    # ── 创建 / 读取 ──────────────────────────────────────────

    async def create_run(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        goal: str,
        acceptance_criteria: list[str],
        thread_id: str = "",
        trace_id: str = "",
        tool_chain: list[str] | None = None,
        max_steps: int | None = None,
        initial_slots: dict[str, Any] | None = None,
    ) -> RuntimeState:
        chain = tool_chain or DEFAULT_TOOL_CHAIN
        if not self.production_mode:
            # Demo/legacy compatibility only. Production never trusts client tool chains.
            chain = [t for t in chain if t in self.policy.rules]
            if not chain:
                raise ValueError("tool_chain contains no allowed tools")
        budget = self._default_budget
        if max_steps is not None and max_steps > 0:
            budget = budget.model_copy(update={"max_steps": max_steps})
        run_id = "run-" + uuid.uuid4().hex[:12]
        effective_tenant_id = tenant_id or user_id
        effective_thread_id = thread_id or "thread-" + uuid.uuid4().hex[:12]
        envelope = TaskEnvelope.from_user_input(
            task_id=run_id,
            thread_id=effective_thread_id,
            user_id=user_id,
            tenant_id=effective_tenant_id,
            goal=goal[:2000],
            intent=TaskIntent.UNKNOWN,
            acceptance_criteria=[c[:500] for c in acceptance_criteria],
        )
        skill_versions: dict[str, str] = {}
        skill_snapshot_hash = ""
        planner_version = "demo-sequential-v1"
        runtime_adapter = "agent-runtime-legacy-adapter"
        tool_adapter = "demo"
        if self.production_mode:
            understanding = await TaskUnderstandingService().understand(goal)
            envelope = merge_task_envelope(
                envelope, understanding.patch, turn_id="turn-initial"
            ).envelope
            if initial_slots:
                allowed_initial = {
                    key: value
                    for key, value in initial_slots.items()
                    if key in set(TaskEnvelope.SLOT_NAMES)
                }
                if allowed_initial:
                    allowed_initial["explicit_slots"] = sorted(allowed_initial)
                    envelope = merge_task_envelope(
                        envelope, allowed_initial, turn_id="turn-initial-slots"
                    ).envelope
            from agent.rule_planner import RulePlannerV1

            planned = await RulePlannerV1(self.business_registry).plan(
                envelope, run_id=run_id
            )
            chain = [step.tool for step in planned.plan.steps]
            planner_version = planned.plan.planner_version
            runtime_adapter = "agent-runtime-production-v1"
            tool_adapter = (
                self.business_tool_adapter.value
                if isinstance(self.business_tool_adapter, BusinessToolAdapterKind)
                else str(self.business_tool_adapter)
            )
            if self.skill_registry is not None:
                selection = self.skill_registry.select(
                    intent=str(envelope.intent.value or TaskIntent.UNKNOWN.value),
                    plan_tools=chain,
                )
                skill_versions = {item.name: item.version for item in selection.skills}
                skill_snapshot_hash = self.skill_registry.snapshot_hash
        state = RuntimeState(
            run_id=run_id,
            thread_id=effective_thread_id,
            trace_id=trace_id,
            user_id=user_id,
            tenant_id=effective_tenant_id,
            goal=goal[:2000],
            acceptance_criteria=[c[:500] for c in acceptance_criteria],
            task_envelope=envelope,
            slot_states=envelope.slot_states(),
            budget=budget,
            usage=BudgetUsage(),
            pending_steps=list(chain),
            status=RuntimeStatus.PENDING,
        )
        # 启动前冻结不可变运行清单（阶段3 WBS 3.1 / 统一架构 §2）：
        # 缺失必填字段 / 预算未冻结 / DB 写失败则 raise，不得启动 Agent
        if self.db is not None:
            manifest = build_run_manifest(
                run_id=state.run_id,
                user_id=user_id,
                tenant_id=state.tenant_id,
                thread_id=effective_thread_id,
                trace_id=trace_id,
                execution_mode=ExecutionMode.AUTONOMOUS,
                code_revision=self._code_revision(),
                skill_snapshot_hash=skill_snapshot_hash,
                skill_versions=skill_versions,
                tool_registry_version=self._tool_registry_version(),
                task_schema_version=envelope.schema_version,
                task_snapshot_hash=envelope.fingerprint(),
                slot_snapshot_hash=envelope.slot_fingerprint(),
                plan_version=state.plan_version,
                planner_version=planner_version,
                runtime_adapter=runtime_adapter,
                tool_adapter=tool_adapter,
                budget=budget,
                acceptance_criteria=state.acceptance_criteria,
            )
            validate_manifest(manifest)
            await RunManifestStore(db=self.db).save(manifest)
        await self.store.save(state)
        await self._emit_event(
            "run_created",
            state,
            {
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

    async def get_trace(self, run_id: str, user_id: str) -> TraceReport | None:
        """统一追溯报告（阶段3 WBS 3.2）：合并清单 + 状态 + 事件，回答 7 问。

        多租户隔离：非本人 run 返回 None；无清单持久化时仍聚合状态 + 事件。
        """
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return None
        if self.db is None:
            # demo 内存模式：无 runtime_manifests 持久化，仍可回答追溯问题
            events = await self.event_store.list_run_events(run_id)
            return build_trace(state=state, runtime_events=events)
        return await load_trace(
            manifest_store=RunManifestStore(db=self.db),
            state_store=self.store,
            run_id=run_id,
            runtime_event_store=self.event_store,
        )

    async def get_recovery_suggestion(
        self, run_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """恢复建议对外接口（阶段3 WBS 3.6）：依据当前故障快照输出确定性恢复动作。

        输入仅来自 RuntimeState（脱敏：错误码/失败步骤/用量/审批状态）；
        输出为 RecoveryDecision（限定 11 种动作），供前端/运维直接展示与执行。
        多租户隔离：非本人 run 返回 None。
        """
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return None
        category = classify_error(state.reason_code or "")
        # 从决策摘要推断最近失败/被拦步骤
        failed = [
            d for d in state.decision_summaries if d.outcome in ("failed", "denied", "blocked")
        ]
        last_failed = failed[-1] if failed else None
        phase = last_failed.phase if last_failed else ""
        step = last_failed.step_id if last_failed else state.current_step
        tool_name = last_failed.tool_name if last_failed else ""
        rule = self.policy.rules.get(tool_name) if tool_name else None
        side_effect_level = rule.risk_level.value if rule is not None else "L0"
        # 替代工具：同风险等级的只读工具（排除当前失败工具），服务端从规则表推导
        alternative_tools: list[str] = []
        if rule is not None:
            alternative_tools = [
                t
                for t, r in self.policy.rules.items()
                if r.risk_level == rule.risk_level and t != tool_name and not r.has_side_effect
            ][:5]
        approval_status = "none"
        if state.approval_state.pending_approvals:
            approval_status = "pending"
        elif category == ErrorCategory.POLICY_DENIED:
            approval_status = "denied"
        context = RecoveryContext(
            error_category=category,
            error_code=state.reason_code,
            phase=phase,
            step=step,
            attempt=max(1, state.usage.retries + 1),
            tool_name=tool_name,
            side_effect_level=side_effect_level,
            remaining_budget_ok=state.usage.can_continue(state.budget),
            alternative_tools=alternative_tools,
            completed_steps=len(state.completed_steps),
            evidence_count=len(state.evidence),
            risk_level=side_effect_level,
            approval_status=approval_status,
        )
        decision = RecoveryPolicy().decide(context)
        return {
            "run_id": state.run_id,
            "status": state.status.value,
            "context": {
                "category": category.value,
                "error_code": state.reason_code,
                "phase": phase,
                "step": step,
                "attempt": context.attempt,
                "tool_name": tool_name,
                "side_effect_level": side_effect_level,
                "remaining_budget_ok": context.remaining_budget_ok,
                "alternative_tools": alternative_tools,
                "completed_steps": context.completed_steps,
                "evidence_count": context.evidence_count,
                "approval_status": approval_status,
            },
            "decision": {
                "action": decision.action.value,
                "reason": decision.reason,
                "max_attempts_left": decision.max_attempts_left,
                "escalate_to_approval": decision.escalate_to_approval,
                "compensate": decision.compensate,
            },
        }

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

    async def respond(
        self,
        run_id: str,
        user_id: str,
        *,
        slot_values: dict[str, Any],
        message: str = "",
        turn_id: str = "",
    ) -> RuntimeState | None:
        """Merge an explicit user answer and resume only the invalidated suffix."""
        state = await self.store.load(run_id)
        if state is None or state.user_id != user_id:
            return None
        if state.status != RuntimeStatus.WAITING_USER or state.task_envelope is None:
            raise ValueError("run is not waiting for user input")
        combined_values = dict(slot_values)
        if message.strip():
            understood = await self.task_understanding.understand(message)
            combined_values = {**understood.patch.slot_values(), **combined_values}
            normalized = message.strip().lower()
            if any(marker in normalized for marker in ("你决定", "你来定", "随便", "都可以")):
                combined_values["auto_select"] = True
            # 分类/产品关卡：用户授权"仍用这篇继续" → 打开 auto_select 跳过离题关卡
            if any(marker in normalized for marker in (
                "继续用这篇", "仍用这篇", "继续", "用这篇", "通用口径", "第一个", "方案一", "选一"
            )):
                combined_values["auto_select"] = True
            # 分类/产品关卡：用户要求换下一条候选 → 直接切到下一条（若有）
            if any(marker in normalized for marker in (
                "换下一条", "换一条", "换下一", "换一个", "下一个", "第二条", "方案二", "选二",
                "换别的", "另一条", "其他一条",
            )):
                discover = state.normalized_observations.get("discover", {}).get("data", {})
                candidates = discover.get("items") or discover.get("articles") or []
                current = state.slot_states.get("selected_article_ids")
                current_ids = list(current.value) if current and current.value else []
                if candidates:
                    # 从未选过或需前进：跳过已选，选择下一个未被选中的候选
                    picked = None
                    for cand in candidates:
                        cid = cand.get("article_id")
                        if cid and cid not in current_ids:
                            picked = cid
                            break
                    if picked is None and candidates:
                        picked = candidates[0].get("article_id")
                    if picked:
                        combined_values["selected_article_ids"] = [picked]
                        combined_values["auto_select"] = True
            # 用户对候选不满意，要求重新抓取最新 → 置 crawl_approved，使 discover 失效并改用 crawl_news 实爬
            if any(marker in normalized for marker in (
                "爬最新", "爬取最新", "抓取最新", "重新爬", "重新抓", "重新抓取",
                "重新爬取", "重爬", "重抓", "更新新闻", "拉取最新", "拉最新", "不满意",
            )):
                combined_values["crawl_approved"] = True
            ordinal_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
            match = re.search(r"第?\s*([一二三四五\d]+)\s*(?:条|篇|个)?", normalized)
            if match and "selected_article_ids" not in combined_values:
                raw_index = match.group(1)
                index = ordinal_map.get(raw_index, int(raw_index) if raw_index.isdigit() else 0)
                discover = state.normalized_observations.get("discover", {}).get("data", {})
                candidates = discover.get("items") or discover.get("articles") or []
                if 0 < index <= len(candidates):
                    candidate_id = candidates[index - 1].get("article_id")
                    if candidate_id:
                        combined_values["selected_article_ids"] = [candidate_id]
        patch = {
            key: value
            for key, value in combined_values.items()
            if key in set(TaskEnvelope.SLOT_NAMES)
        }
        if not patch:
            raise ValueError("answer contains no allowed task slots")
        patch["explicit_slots"] = sorted(patch)
        merged = merge_task_envelope(
            state.task_envelope,
            patch,
            turn_id=turn_id or "turn-" + uuid.uuid4().hex[:12],
            completed_steps=set(state.completed_steps),
        )
        invalidate_by_slot = {
            "selected_article_ids": {"article", "classify", "products", "score", "draft", "review", "save", "export"},
            "product_ids": {"score", "draft", "review", "save", "export"},
            "template_key": {"draft", "review", "save", "export"},
            "angle": {"draft", "review", "save", "export"},
            "tone": {"draft", "review", "save", "export"},
            "length": {"draft", "review", "save", "export"},
            "revision_instruction": {"revise", "review", "save", "export"},
            "crawl_approved": {"discover", "article", "classify", "products", "score", "draft", "review"},
            "auto_select": {"article", "classify", "products", "score", "draft", "review"},
        }
        invalidated: set[str] = set()
        for name in merged.changed_slots:
            invalidated.update(invalidate_by_slot.get(name, set()))
        resumed = state.model_copy(
            update={
                "task_envelope": merged.envelope,
                "slot_states": merged.envelope.slot_states(),
                "completed_steps": [s for s in state.completed_steps if s not in invalidated],
                "failed_steps": [s for s in state.failed_steps if s not in invalidated],
                "normalized_observations": {
                    key: value
                    for key, value in state.normalized_observations.items()
                    if key not in invalidated
                },
                "pending_questions": [],
                "status": RuntimeStatus.PENDING,
                "reason": "",
                "reason_code": "",
            }
        )
        await self.store.save(resumed)
        await self._emit_event(
            "user_response_applied",
            resumed,
            {"changed_slots": merged.changed_slots, "invalidated_steps": sorted(invalidated)},
        )
        await self.start_run(run_id, user_id)
        return resumed

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
        if self.production_mode:
            from agent.production_runtime import (
                ProductionActionPlanner,
                ProductionBusinessExecutor,
                ProductionGoalAdapter,
                build_business_policy,
            )
            from agent.rule_planner import RulePlannerV1

            rule_planner = RulePlannerV1(self.business_registry)
            if str(getattr(self.settings, "AGENT_PLANNER", "rule") or "rule").lower() == "llm":
                from agent.llm_action_planner import LLMActionPlanner

                planner = LLMActionPlanner(rule_planner, llm_factory=self._planner_llm_factory)
            else:
                planner = ProductionActionPlanner(rule_planner)
            executor = ProductionBusinessExecutor(
                self.business_executor,
                planner,
                adapter=self.business_tool_adapter,
            )

            async def _production_checkpoint(s: RuntimeState) -> None:
                await self.store.save(s)

            return AgentRuntime(
                planner=planner,
                executor=executor,
                policy=build_business_policy(self.business_registry),
                approval_service=self.approval_service,
                goal_validator=ProductionGoalAdapter(planner),
                model_router=self.model_router,
                checkpointer=_production_checkpoint,
                event_emitter=self._emit_event,
                max_retries=getattr(self._default_budget, "max_retries", 2),
                backoff_jitter=0.0,
            )
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

    def _planner_llm_factory(self):
        """为 LLM 选步规划器懒构造低温度 LLM；构造失败抛错由规划器兜底逻辑接管。"""
        if getattr(self, "_planner_llm", None) is None:
            from langchain_openai import ChatOpenAI

            self._planner_llm = ChatOpenAI(
                model=getattr(self.settings, "DEEPSEEK_MODEL", "deepseek-chat"),
                api_key=getattr(self.settings, "DEEPSEEK_API_KEY", ""),
                base_url=getattr(self.settings, "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                temperature=0,
                timeout=getattr(self.settings, "DEEPSEEK_TIMEOUT", 60),
                max_tokens=getattr(self.settings, "DEEPSEEK_MAX_TOKENS", 2000),
            )
        return self._planner_llm

    async def _emit_event(
        self, event_type: str, state: RuntimeState, payload: dict[str, Any]
    ) -> None:
        # 指标喂入（无论走 outbox 还是直接写，事件已发生即计数）
        if self.metrics is not None:
            if event_type in ("step_failed", "run_failed"):
                self.metrics.inc("error_events_total")
            if (payload or {}).get("recovery_action"):
                self.metrics.inc("recovery_actions_total")
            if event_type == "policy_checked" and payload.get("action") == "deny":
                self.metrics.inc("policy_denied_total")
                self.metrics.inc("unsafe_action_count")
        if self.outbox is not None:
            try:
                entry = await self.outbox.enqueue_run_event(
                    run_id=state.run_id,
                    event_type=event_type,
                    payload=payload,
                    status=state.status.value,
                )
                if entry is not None:
                    # 立即投递到事件存储（SSE 可读）；失败保留 pending 供对账重试
                    await self.outbox.flush(run_id=state.run_id, limit=100)
                return  # 幂等去重：相同事件（dedup_key）不再重复投递
            except Exception:
                logger.exception("[autonomous] outbox emit failed, fallback direct append")
        await self.event_store.append(
            run_id=state.run_id, event_type=event_type, status=state.status.value, payload=payload
        )

    async def _deliver_outbox_event(self, entry: Any) -> bool:
        """outbox 投递回调：写入事件存储（幂等由 outbox dedup_key 唯一索引保证）。"""
        try:
            await self.event_store.append(
                run_id=entry.run_id,
                event_type=entry.event_type,
                status=entry.event_status,
                payload=entry.payload,
            )
            return True
        except Exception:
            logger.exception("[autonomous] outbox deliver failed: %s", getattr(entry, "entry_id", "?"))
            return False

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
            self._record_run_metrics(result.final_state, result)
        except asyncio.CancelledError:
            logger.info("[autonomous] run %s task canceled", state.run_id)
        except Exception:
            logger.exception("[autonomous] run %s failed unexpectedly", state.run_id)
        finally:
            self._tasks.pop(state.run_id, None)
            self._cancel_events.pop(state.run_id, None)

    def _record_run_metrics(self, state: RuntimeState, result: Any) -> None:
        """自主运行结束喂生产指标（脱敏数值，供告警规则消费）。"""
        if self.metrics is None:
            return
        usage = state.usage
        self.metrics.inc("run_finished_total")
        status = result.status.value
        self.metrics.inc(f"run_{status}_total")
        if status == "budget_exceeded":
            self.metrics.inc("budget_exhausted")
        if usage.total_tokens:
            self.metrics.inc("llm_tokens_total", amount=usage.total_tokens)
        if usage.cost_usd > 0:
            self.metrics.inc("cost_usd_micro_total", amount=int(usage.cost_usd * 1_000_000))
        if usage.tool_calls:
            self.metrics.inc("tool_calls_total", amount=usage.tool_calls)
        if usage.retries:
            self.metrics.inc("retries_total", amount=usage.retries)
        if usage.steps:
            self.metrics.inc("steps_total", amount=usage.steps)
        elapsed = usage.elapsed_seconds()
        self.metrics.observe("run_duration_seconds", value=elapsed)
        if result.status.value == "stopped":
            self.metrics.set_gauge("stuck_running_seconds", value=elapsed)

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
