"""A2A Server 协议逻辑 — 阶段四 4B Step 4B-2。

A2AServer 承载协议逻辑（Agent Card / Send / Get / List / Cancel / Subscribe），
services/backend/api/a2a.py 暴露 REST 路由。请求链路：

    认证 -> 协议校验 -> 输入净化 -> PolicyEngine
    -> 创建或关联内部运行 -> Runtime 执行
    -> 状态/Artifact 映射 -> 响应或事件流

安全约束：
  - Agent Card 只发布真实可用并获准开放的能力；未实现能力返回明确协议错误
    （MethodNotImplementedError），不静默伪装成功；
  - 外部 Message 一律视为不可信输入，先净化再使用；
  - A2A 身份映射为内部 service principal（principal），不冒充最终用户；
  - 对外 Task/Artifact 只含脱敏信息（不含参数原文、提示词、密钥与私有推理链）；
  - 幂等：同 task_id 重复 Send 返回既有 Task，不重复创建内部运行；
  - 状态变更带版本号（底层 RuntimeStateStore / A2ATaskStore），拒绝旧版本覆盖。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator

from agent.a2a.mapper import (
    map_runtime_to_task,
    map_state_to_task,
    validate_external_input,
)
from agent.a2a.models import (
    A2AError,
    AgentCard,
    InvalidInputError,
    Message,
    MethodNotImplementedError,
    ProtocolError,
    Skill,
    Task,
    TaskSendResult,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from agent.a2a.task_store import A2ATaskConflictError, A2ATaskStore
from agent.autonomous_service import AutonomousRunService
from agent.policy_engine import PolicyAction, PolicyEngine, RiskLevel
from agent.runtime_events import RuntimeEvent

logger = logging.getLogger("backend.agent.a2a_server")

# 首批试点开放的只读工具链（低风险；具体放行由 PolicyEngine 门禁决定）
A2A_SKILL_TOOL_CHAIN = [
    "retrieve_articles",
    "classify_articles",
    "score_articles",
    "export_articles_csv",
]

MAX_GOAL_CHARS = 2000
MAX_CRITERIA_PER_TASK = 5


def _extract_goal(message: Message) -> str:
    """从外部 Message 提取目标文本（脱敏：截断、只取 text part）。"""
    texts = [p.text for p in message.parts if p.kind == "text" and p.text]
    return " ".join(texts)[:MAX_GOAL_CHARS].strip()


class A2AServer:
    """A2A 1.0 服务端：协议编排（复用 AutonomousRunService 执行内部运行）。"""

    def __init__(
        self,
        *,
        run_service: AutonomousRunService,
        task_store: A2ATaskStore,
        policy: PolicyEngine | None = None,
        skills: list[Skill] | None = None,
        card_name: str = "PR 情报智能体",
        card_description: str = "PR 情报分析 A2A Agent（A2A 1.0，HTTP+JSON/REST）",
        card_url: str = "http://a2a.internal/.well-known/agent-card.json",
        principal_prefix: str = "a2a",
    ):
        self.run_service = run_service
        self.task_store = task_store
        self.policy = policy or PolicyEngine()
        self.skills = list(skills or [])
        self.principal_prefix = principal_prefix
        self._agent_card = AgentCard(
            name=card_name,
            description=card_description,
            url=card_url,
            skills=self.skills,
        )

    def principal(self, user_id: str) -> str:
        """最终用户 -> A2A service principal（不冒充最终用户，站内运行归属 principal）。"""
        return f"{self.principal_prefix}:{user_id}"

    # ── 发现层 ──────────────────────────────────────────────

    @property
    def agent_card(self) -> AgentCard:
        """Agent Card：只发布真实可用且获准开放的能力。"""
        return self._agent_card

    # ── Send（消息提交） ─────────────────────────────────────

    async def send(self, message: Message, *, principal: str) -> TaskSendResult:
        """Message Send：净化 → 能力门禁 → PolicyEngine → 创建/关联内部 run → 启动 → 映射。"""
        # 1. 不可信输入净化（大小 / Part 数 / 凭证 / 恶意模式 / URI）
        validate_external_input(message)

        # 2. 能力门禁：Skill 必须是 Agent Card 声明的
        skill_id = str(message.metadata.get("skill_id", "") or self.skills[0].id)
        skill = next((s for s in self.skills if s.id == skill_id), None)
        if skill is None:
            raise MethodNotImplementedError(f"skill not offered: {skill_id}")

        # 3. PolicyEngine 门禁：只放行策略允许的低风险工具链
        chain = self._allowed_chain()
        if not chain:
            raise MethodNotImplementedError("no policy-permitted tools for A2A")

        # 4. 目标抽取：无可信文本内容 → 拒绝
        goal = _extract_goal(message)
        if not goal:
            raise InvalidInputError("message contains no usable text content")

        # 5. 幂等：已有 task_id 直接返回既有任务（不重复创建内部运行）
        task_id = message.task_id or f"a2a-{uuid.uuid4().hex[:12]}"
        existing = await self.task_store.load(task_id, user_id=principal)
        if existing is not None:
            return await self._existing_result(existing, task_id, principal)

        # 6. 创建内部运行（principal 即 service principal，非最终用户）
        state = await self.run_service.create_run(
            user_id=principal,
            goal=goal,
            acceptance_criteria=["完成请求目标"],
            thread_id=message.context_id,
            trace_id=message.message_id,
            tool_chain=chain,
        )

        # 7. 持久化 A2A Task 映射（a2a_task_id <-> internal_run_id）
        created = await self.task_store.create(
            Task(
                id=task_id,
                context_id=message.context_id,
                status=TaskStatus.SUBMITTED,
                metadata={"skill_id": skill.id, "message_id": message.message_id},
                internal_run_id=state.run_id,
            ),
            user_id=principal,
        )
        if not created:
            raise A2ATaskConflictError(f"task id already in use: {task_id}")

        # 8. 启动运行并映射响应
        await self.run_service.start_run(state.run_id, principal)
        fresh = await self.run_service.get_run(state.run_id, user_id=principal)
        return TaskSendResult(task=map_state_to_task(fresh, task_id=task_id))

    async def _existing_result(
        self, existing: Task, task_id: str, principal: str
    ) -> TaskSendResult:
        """既有 task_id 的幂等返回：内部 run 存在则映射最新状态。"""
        if not existing.internal_run_id:
            return TaskSendResult(task=existing)
        state = await self.run_service.get_run(existing.internal_run_id, user_id=principal)
        if state is None:
            raise A2AError(f"task {task_id} references missing internal run")
        return TaskSendResult(task=map_state_to_task(state, task_id=task_id))

    # ── Tasks Get / List / Cancel ────────────────────────────

    async def get_task(self, task_id: str, *, principal: str) -> Task | None:
        """Tasks/Get：映射内部运行最新状态（多租户：principal 隔离）。"""
        stored = await self.task_store.load(task_id, user_id=principal)
        if stored is None:
            return None
        if not stored.internal_run_id:
            return stored
        state = await self.run_service.get_run(stored.internal_run_id, user_id=principal)
        if state is None:
            return stored
        return map_state_to_task(state, task_id=task_id)

    async def list_tasks(
        self, *, principal: str, status: str = "", limit: int = 50
    ) -> list[Task]:
        """Tasks/List / Query：按 principal 与可选状态过滤。"""
        return await self.task_store.list_tasks(
            user_id=principal, status=status, limit=min(limit, 200)
        )

    async def cancel(self, task_id: str, *, principal: str) -> Task | None:
        """Tasks/Cancel：取消关联的内部运行（安全点停止）。"""
        stored = await self.task_store.load(task_id, user_id=principal)
        if stored is None or not stored.internal_run_id:
            return None
        if not await self.run_service.cancel_run(stored.internal_run_id, principal):
            return None
        state = await self.run_service.get_run(stored.internal_run_id, user_id=principal)
        return map_state_to_task(state, task_id=task_id) if state is not None else stored

    # ── Tasks/Subscribe（事件流，游标续传 + 投递去重） ────────

    async def subscribe(
        self, task_id: str, *, principal: str, last_event_id: str = ""
    ) -> AsyncIterator[TaskStatusUpdateEvent]:
        """Subscribe：内部运行事件流 → TaskStatusUpdateEvent（Last-Event-ID 续传）。"""
        stored = await self.task_store.load(task_id, user_id=principal)
        if stored is None or not stored.internal_run_id:
            raise ProtocolError("task not found or not linked to internal run")
        last_seq = 0
        if last_event_id and last_event_id.isdigit():
            last_seq = int(last_event_id)
        async for ev in self._poll_events(stored.internal_run_id, principal, last_seq):
            event = TaskStatusUpdateEvent(
                event_id=ev.event_id,
                task_id=task_id,
                status=map_runtime_to_task(ev.status),
                metadata={"sequence": ev.sequence},
                timestamp=ev.timestamp,
            )
            # 投递去重记账：同一 (task_id, event_id) 只投递一次
            await self.task_store.record_event(task_id, ev.event_id)
            yield event

    async def _poll_events(
        self, run_id: str, principal: str, last_seq: int
    ) -> AsyncIterator[RuntimeEvent]:
        """防呆循环：先补齐已有事件，再轮询新事件直到内部 run 终态。"""
        while True:
            events = await self.run_service.events(
                run_id, principal, last_sequence=last_seq
            )
            for ev in events:
                last_seq = ev.sequence
                yield ev
            state = await self.run_service.get_run(run_id, user_id=principal)
            if state is None or state.is_terminal:
                return
            await asyncio.sleep(1)

    # ── PolicyEngine 门禁 ────────────────────────────────────

    def _allowed_chain(self) -> list[str]:
        """只放行策略允许且非 L3/默认拒绝的工具（低风险 Skill 工具链子集）。"""
        chain: list[str] = []
        for tool in A2A_SKILL_TOOL_CHAIN:
            if not self.policy.is_tool_allowed(tool, allowed_tool_names=None):
                continue
            rule = self.policy.rules[tool]
            if rule.default_action == PolicyAction.DENY or rule.risk_level == RiskLevel.L3:
                continue
            chain.append(tool)
        return chain
