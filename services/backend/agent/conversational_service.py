"""Stateful stage-3 turn service used by the unified Agent API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from agent.business_tools import BusinessToolAdapterKind, BusinessToolExecutor, ToolRequestContext
from agent.candidate_selection import CandidateSelector
from agent.clarification import ClarificationPolicy
from agent.contracts.task import ConversationTurn, SlotStatus, TaskEnvelope, TaskIntent
from agent.run_manifest import ExecutionMode, RunManifest, build_run_manifest
from agent.slot_merger import SlotMerger
from agent.task_state_store import (
    InMemoryTaskStateStore,
    TaskState,
    TaskStateConflictError,
    TaskStateNotFoundError,
    TaskStateStoreProtocol,
    TaskStatus,
)
from agent.task_understanding import TaskEnvelopePatch, TaskUnderstandingService
from agent.task_understanding import _NEWS_CATEGORIES as NEWS_CATEGORIES
from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(UTC)


DEFAULT_CATEGORY = "AI技术重大进展"


class AgentTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(..., min_length=1, max_length=12_000)
    turn_id: str = Field(
        default_factory=lambda: "turn-" + uuid.uuid4().hex, min_length=1, max_length=100
    )
    task_id: str = Field(default="", max_length=100)
    thread_id: str = Field(default="", max_length=100)


class AgentEvent(BaseModel):
    schema_version: str = "1.0"
    event_id: str
    sequence: int = Field(ge=1)
    run_id: str
    task_id: str
    turn_id: str = ""
    event_type: str
    status: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_now)


class AgentRunRecord(BaseModel):
    run_id: str
    task_id: str
    thread_id: str
    user_id: str
    tenant_id: str
    turn_id: str
    status: Literal[
        "pending",
        "running",
        "waiting_user",
        "waiting_approval",
        "completed",
        "failed",
        "canceled",
    ] = "pending"
    intent: str = "unknown"
    tool_registry_version: str = ""
    changed_slots: list[str] = Field(default_factory=list)
    invalidated_steps: list[str] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgentTurnResult(BaseModel):
    duplicate: bool = False
    task: TaskEnvelope
    run: AgentRunRecord


class ConversationalAgentService:
    """Owns multi-turn persistence and stage-3 decisions, not the legacy Chat API."""

    TOOL_SCOPES = frozenset(
        {
            "articles:read",
            "news:search",
            "news:crawl",
            "articles:classify",
            "products:read",
            "articles:score",
            "drafts:write",
            "drafts:review",
            "drafts:export",
        }
    )

    def __init__(
        self,
        *,
        task_store: TaskStateStoreProtocol | None = None,
        understanding: TaskUnderstandingService | None = None,
        slot_merger: SlotMerger | None = None,
        clarification: ClarificationPolicy | None = None,
        candidate_selector: CandidateSelector | None = None,
        tool_executor: BusinessToolExecutor | None = None,
        tool_adapter: BusinessToolAdapterKind | str = BusinessToolAdapterKind.FAKE,
        event_store: Any = None,
        manifest_store: Any = None,
    ):
        self.task_store = task_store or InMemoryTaskStateStore()
        self.understanding = understanding or TaskUnderstandingService()
        self.slot_merger = slot_merger or SlotMerger()
        self.clarification = clarification or ClarificationPolicy()
        self.candidate_selector = candidate_selector or CandidateSelector()
        self.tool_executor = tool_executor
        self.tool_adapter = tool_adapter
        self.event_store = event_store
        self.manifest_store = manifest_store
        self._runs: dict[str, AgentRunRecord] = {}
        self._events: dict[str, list[AgentEvent]] = {}
        self._turn_runs: dict[tuple[str, str, str], str] = {}
        self._thread_tasks: dict[tuple[str, str, str], str] = {}
        self._asked_slots: dict[str, set[str]] = {}
        self._candidates: dict[str, list[dict[str, Any]]] = {}
        self._manifests: dict[str, RunManifest] = {}

    async def submit_turn(
        self,
        body: AgentTurnInput,
        *,
        user_id: str,
        tenant_id: str,
    ) -> AgentTurnResult:
        if not user_id or not tenant_id:
            raise ValueError("user_id and tenant_id are required")
        task_id, thread_id = self._resolve_identity(body, user_id=user_id, tenant_id=tenant_id)
        state = await self.task_store.get(task_id, user_id=user_id, tenant_id=tenant_id)
        if state is None:
            envelope = TaskEnvelope.from_user_input(
                task_id=task_id,
                thread_id=thread_id,
                user_id=user_id,
                tenant_id=tenant_id,
                goal=body.content,
                intent=TaskIntent.UNKNOWN,
                turn_id=body.turn_id,
            )
            state = await self.task_store.create(TaskState.create(envelope))
        else:
            thread_id = state.thread_id

        # Persistence is deliberately first. Parsing/model/tool failures happen after this point.
        existing_turn = next((turn for turn in state.turns if turn.turn_id == body.turn_id), None)
        if existing_turn is not None and existing_turn.content != body.content:
            raise TaskStateConflictError("turn_id already exists with different content")

        duplicate_run_id = self._turn_runs.get((tenant_id, task_id, body.turn_id))
        duplicate_run = self._runs.get(duplicate_run_id or "")
        if duplicate_run is not None:
            return AgentTurnResult(
                duplicate=True,
                task=state.envelope,
                run=duplicate_run.model_copy(deep=True),
            )

        if existing_turn is not None and state.current_run_id:
            existing_run = self._runs.get(state.current_run_id)
            if existing_run is None:
                status = (
                    "waiting_user"
                    if state.status == TaskStatus.WAITING_USER
                    else "completed"
                    if state.status == TaskStatus.COMPLETED
                    else "running"
                )
                existing_run = AgentRunRecord(
                    run_id=state.current_run_id,
                    task_id=state.task_id,
                    thread_id=state.thread_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    turn_id=body.turn_id,
                    status=status,
                    intent=str(state.envelope.intent.value or TaskIntent.UNKNOWN.value),
                )
                self._runs[state.current_run_id] = existing_run
                self._events.setdefault(state.current_run_id, [])
            self._turn_runs[(tenant_id, task_id, body.turn_id)] = state.current_run_id
            return AgentTurnResult(
                duplicate=True, task=state.envelope, run=existing_run.model_copy(deep=True)
            )
        if existing_turn is None:
            state = await self.task_store.append_turn(
                task_id,
                ConversationTurn(
                    turn_id=body.turn_id,
                    sequence=len(state.turns) + 1,
                    role="user",
                    content=body.content,
                ),
                user_id=user_id,
                tenant_id=tenant_id,
                expected_version=state.version,
            )

        run_id = "run-" + uuid.uuid4().hex
        run = AgentRunRecord(
            run_id=run_id,
            task_id=task_id,
            thread_id=thread_id,
            user_id=user_id,
            tenant_id=tenant_id,
            turn_id=body.turn_id,
            tool_registry_version=(
                self.tool_executor.registry.manifest_version if self.tool_executor else "none"
            ),
        )
        self._runs[run_id] = run
        self._events[run_id] = []
        self._turn_runs[(tenant_id, task_id, body.turn_id)] = run_id
        await self._event(run, "turn.persisted", "pending", {"turn_sequence": len(state.turns)})

        try:
            understanding = await self.understanding.understand(body.content)
            merge = self.slot_merger.merge(
                state.envelope, understanding.patch, turn_id=body.turn_id
            )
            # 用户授权默认值时，为 generate_draft 填充默认类别与产品（需在状态保存前）。
            if understanding.patch.assumptions:
                merge = self._apply_generate_defaults(merge, turn_id=body.turn_id)

            # A reply may be selecting from candidates produced by the previous turn.
            candidates = self._candidates.get(task_id, [])
            if candidates and not merge.envelope.selected_article_ids.value:
                selection = self.candidate_selector.select(candidates, body.content)
                if selection.selected is not None:
                    selection_patch = {
                        "selected_article_ids": [selection.selected.article_id],
                        "explicit_slots": ["selected_article_ids"],
                    }
                    merge = self.slot_merger.merge(
                        merge.envelope, selection_patch, turn_id=body.turn_id
                    )
                    await self._event(
                        run,
                        "candidate.selected",
                        "running",
                        {
                            "article_id": selection.selected.article_id,
                            "matched_by": selection.matched_by,
                        },
                    )

            state = await self.task_store.compare_and_set(
                state.model_copy(
                    update={
                        "envelope": merge.envelope,
                        "slot_states": merge.envelope.slot_states(),
                        "current_run_id": run_id,
                        "status": TaskStatus.PLANNING,
                    }
                ),
                expected_version=state.version,
            )
            intent = str(merge.envelope.intent.value or TaskIntent.UNKNOWN.value)
            run = run.model_copy(
                update={
                    "status": "running",
                    "intent": intent,
                    "changed_slots": merge.changed_slots,
                    "invalidated_steps": merge.invalidated_steps,
                    "assumptions": [item.text for item in merge.envelope.assumptions],
                    "updated_at": _now(),
                }
            )
            self._runs[run_id] = run
            manifest = build_run_manifest(
                run_id=run_id,
                user_id=user_id,
                tenant_id=tenant_id,
                thread_id=thread_id,
                execution_mode=ExecutionMode.AGENTLOOP,
                code_revision="stage3-conversational-runtime",
                tool_registry_version=run.tool_registry_version,
                task_schema_version=merge.envelope.schema_version,
                task_snapshot_hash=merge.envelope.fingerprint(),
                slot_snapshot_hash=merge.envelope.slot_fingerprint(),
                acceptance_criteria=list(merge.envelope.acceptance_criteria.value or []),
            )
            self._manifests[run_id] = manifest
            if self.manifest_store is not None:
                await self.manifest_store.save(manifest)
            await self._event(
                run,
                "understanding.completed",
                "running",
                {
                    "intent": intent,
                    "changed_slots": merge.changed_slots,
                    "invalidated_steps": merge.invalidated_steps,
                    "parser": understanding.parser,
                },
            )

            allow_defaults = bool(understanding.patch.assumptions)
            slot_options = self._clarify_options()
            decision = self.clarification.decide(
                merge.envelope,
                asked_slots=self._asked_slots.get(task_id, set()),
                answered_slots=set(merge.changed_slots),
                allow_defaults=allow_defaults,
                slot_options=slot_options,
            )
            if not decision.can_proceed:
                questions = [question.model_dump(mode="json") for question in decision.questions]
                self._asked_slots.setdefault(task_id, set()).update(
                    question.slot for question in decision.questions
                )
                run = run.model_copy(
                    update={"status": "waiting_user", "questions": questions, "updated_at": _now()}
                )
                state = await self.task_store.compare_and_set(
                    state.model_copy(update={"status": TaskStatus.WAITING_USER}),
                    expected_version=state.version,
                )
                self._runs[run_id] = run
                await self._event(
                    run,
                    "clarification.required" if questions else "clarification.waiting",
                    "waiting_user",
                    {
                        "questions": questions,
                        "blocked_slots": decision.blocked_slots,
                        "repeated_questions_suppressed": decision.skipped_previously_asked,
                    },
                )
                return AgentTurnResult(task=state.envelope, run=run)

            result, status = await self._execute_intent(run, state.envelope)
            run = run.model_copy(
                update={"status": status, "result": result, "updated_at": _now()}
            )
            task_status = TaskStatus.WAITING_USER if status == "waiting_user" else TaskStatus.COMPLETED
            state = await self.task_store.compare_and_set(
                state.model_copy(update={"status": task_status}), expected_version=state.version
            )
            self._runs[run_id] = run
            await self._event(run, "run.completed" if status == "completed" else "candidate.selection_required", status, result)
            return AgentTurnResult(task=state.envelope, run=run)
        except Exception as exc:
            run = run.model_copy(
                update={"status": "failed", "error": str(exc)[:500], "updated_at": _now()}
            )
            self._runs[run_id] = run
            await self._event(run, "run.failed", "failed", {"error_type": type(exc).__name__})
            return AgentTurnResult(task=state.envelope, run=run)

    def _resolve_identity(self, body, *, user_id, tenant_id) -> tuple[str, str]:
        if body.task_id:
            task_id = body.task_id
            thread_id = body.thread_id or body.task_id
            return task_id, thread_id
        thread_id = body.thread_id or "thread-" + uuid.uuid4().hex
        key = (tenant_id, user_id, thread_id)
        task_id = self._thread_tasks.get(key) or "task-" + uuid.uuid4().hex
        self._thread_tasks[key] = task_id
        return task_id, thread_id

    async def _execute_intent(
        self, run: AgentRunRecord, envelope: TaskEnvelope
    ) -> tuple[dict[str, Any], Literal["completed", "waiting_user"]]:
        intent = TaskIntent(envelope.intent.value or TaskIntent.UNKNOWN)
        if intent == TaskIntent.CANCEL:
            return {"message": "没有指定其他运行，当前取消指令已记录。"}, "completed"
        if intent == TaskIntent.ASK_STATUS:
            return {"message": "任务状态已返回。", "task_id": run.task_id}, "completed"
        if intent == TaskIntent.GENERATE_DRAFT:
            return await self._execute_generate_draft(run, envelope)
        if (
            intent in {TaskIntent.SEARCH_AND_RANK, TaskIntent.SEARCH_AND_DRAFT, TaskIntent.CURATE_NEWS}
            and self.tool_executor is not None
        ):
            query = str(envelope.news_query.value or envelope.goal.value or "")
            await self._event(
                run,
                "tool_started",
                "running",
                {"tool": "search_news", "args": {"query": query, "limit": 10}},
            )
            value = await self.tool_executor.invoke(
                "search_news",
                {"query": query, "limit": 10},
                context=ToolRequestContext(
                    user_id=run.user_id,
                    tenant_id=run.tenant_id,
                    scopes=self.TOOL_SCOPES,
                    run_id=run.run_id,
                    turn_id=run.turn_id,
                ),
                adapter=self.tool_adapter,
            )
            payload = value.model_dump(mode="json")
            self._candidates[run.task_id] = payload.get("items", [])
            selection = self.candidate_selector.select(payload.get("items", []))
            payload["selection"] = selection.model_dump(mode="json")
            await self._event(
                run,
                "tool_finished",
                "running",
                {"tool": "search_news", "items": len(payload.get("items", []))},
            )
            if not payload.get("items"):
                payload["message"] = "未检索到匹配的新闻，请更换关键词后重试。"
            if selection.outcome == "needs_selection":
                return payload, "waiting_user"
            return payload, "completed"
        return {
            "message": "任务已完成结构化理解并进入后续规划队列。",
            "intent": intent.value,
            "task_fingerprint": envelope.fingerprint(),
        }, "completed"

    async def _execute_generate_draft(
        self, run: AgentRunRecord, envelope: TaskEnvelope
    ) -> tuple[dict[str, Any], Literal["completed", "waiting_user"]]:
        """生成初稿：未选定文章时先本地库匹配 top5 候选；支持继续匹配与触发爬虫补充。"""
        if envelope.selected_article_ids.value:
            return {
                "message": "已选定新闻，进入稿件生成规划（当前为演示占位）。",
                "article_id": envelope.selected_article_ids.value,
            }, "completed"
        if self.tool_executor is None:
            return {"message": "任务已完成结构化理解并进入后续规划队列。"}, "completed"

        category = str(envelope.category.value or DEFAULT_CATEGORY)
        products = list(envelope.product_ids.value or [])
        product_keyword = " ".join(self._product_names(products))
        crawl = bool(envelope.crawl_approved.value)
        more = bool(envelope.search_more.value)

        context = ToolRequestContext(
            user_id=run.user_id,
            tenant_id=run.tenant_id,
            scopes=self.TOOL_SCOPES,
            run_id=run.run_id,
            turn_id=run.turn_id,
        )
        if crawl:
            await self._event(
                run,
                "tool_started",
                "running",
                {"tool": "crawl_news", "args": {"category": category, "query": product_keyword or category}},
            )
            crawl_result = await self.tool_executor.invoke(
                "crawl_news",
                {
                    "query": product_keyword or category,
                    "max_results": 50,
                    "idempotency_key": f"agent-crawl-{run.task_id[:24]}",
                },
                context=context,
                adapter=self.tool_adapter,
            )
            await self._event(
                run,
                "tool_finished",
                "running",
                {"tool": "crawl_news", "added": crawl_result.added},
            )

        limit = 20 if more else 5
        await self._event(
            run,
            "tool_started",
            "running",
            {"tool": "list_articles", "args": {"category": category, "query": product_keyword, "limit": limit}},
        )
        value = await self.tool_executor.invoke(
            "list_articles",
            {"category": category, "query": product_keyword, "limit": limit},
            context=context,
            adapter=self.tool_adapter,
        )
        payload = value.model_dump(mode="json")
        self._candidates[run.task_id] = payload.get("items", [])
        selection = self.candidate_selector.select(payload.get("items", []))
        payload["selection"] = selection.model_dump(mode="json")
        payload["candidate_source"] = "crawl" if crawl else ("more" if more else "local")
        payload["category"] = category
        payload["product_ids"] = products
        await self._event(
            run,
            "tool_finished",
            "running",
            {"tool": "list_articles", "items": len(payload.get("items", []))},
        )
        if not payload.get("items"):
            payload["message"] = (
                "本地库暂无匹配的新闻，可选择「由用户补充」触发爬虫爬取最新新闻，或调整类别与产品。"
            )
        return payload, "waiting_user"

    def _clarify_options(self) -> dict[str, dict[str, Any]]:
        products = self.understanding.catalog.list_products(published_only=True)
        return {
            "category": {
                "options": list(NEWS_CATEGORIES),
                "default": DEFAULT_CATEGORY,
            },
            "product_ids": {
                "options": [product.name for product in products],
                "default": products[0].name if products else "",
                "multi": True,
            },
        }

    def _apply_generate_defaults(
        self, merge: Any, *, turn_id: str
    ) -> Any:
        """用户授权默认值时，为 generate_draft 填充默认类别与产品，避免继续追问。"""
        envelope = merge.envelope
        if envelope.intent.value != TaskIntent.GENERATE_DRAFT.value:
            return merge
        patch: dict[str, Any] = {}
        if not self._slot_available(envelope.category):
            patch["category"] = DEFAULT_CATEGORY
        products = self.understanding.catalog.list_products(published_only=True)
        if not self._slot_available(envelope.product_ids) and products:
            patch["product_ids"] = [products[0].product_id]
        if not patch:
            return merge
        patch["explicit_slots"] = frozenset({"category", "product_ids"})
        return self.slot_merger.merge(
            envelope, TaskEnvelopePatch(**patch), turn_id=turn_id
        )

    @staticmethod
    def _slot_available(slot: Any) -> bool:
        return (
            slot.status in {SlotStatus.INFERRED, SlotStatus.CONFIRMED}
            and slot.value not in (None, "", [])
        )

    def _product_names(self, product_ids: list[str]) -> list[str]:
        products = {
            product.product_id: product
            for product in self.understanding.catalog.list_products(published_only=True)
        }
        return [products[pid].name for pid in product_ids if pid in products]

    async def get_run(self, run_id: str, *, user_id: str, tenant_id: str) -> AgentRunRecord | None:
        run = self._runs.get(run_id)
        if run is None or run.user_id != user_id or run.tenant_id != tenant_id:
            return None
        return run.model_copy(deep=True)

    async def list_runs(
        self, *, user_id: str, tenant_id: str, limit: int = 30
    ) -> list[AgentRunRecord]:
        """返回当前用户/租户最近的 run 记录（按更新时间倒序）。"""
        runs = [
            run.model_copy(deep=True)
            for run in self._runs.values()
            if run.user_id == user_id and run.tenant_id == tenant_id
        ]
        runs.sort(key=lambda r: r.updated_at, reverse=True)
        return runs[: max(1, min(limit, 200))]

    async def get_manifest(
        self, run_id: str, *, user_id: str, tenant_id: str
    ) -> RunManifest | None:
        if await self.get_run(run_id, user_id=user_id, tenant_id=tenant_id) is None:
            return None
        return self._manifests.get(run_id)

    async def events(
        self, run_id: str, *, user_id: str, tenant_id: str, last_sequence: int = 0
    ) -> list[AgentEvent] | None:
        run = await self.get_run(run_id, user_id=user_id, tenant_id=tenant_id)
        if run is None:
            return None
        return [
            event.model_copy(deep=True)
            for event in self._events.get(run_id, [])
            if event.sequence > last_sequence
        ]

    async def cancel(self, run_id: str, *, user_id: str, tenant_id: str) -> AgentRunRecord | None:
        run = await self.get_run(run_id, user_id=user_id, tenant_id=tenant_id)
        if run is None:
            return None
        if run.status in {"completed", "failed", "canceled"}:
            return run
        run = run.model_copy(update={"status": "canceled", "updated_at": _now()})
        self._runs[run_id] = run
        await self._event(run, "run.canceled", "canceled", {})
        return run

    async def approve(self, run_id: str, *, user_id: str, tenant_id: str) -> AgentRunRecord | None:
        run = await self.get_run(run_id, user_id=user_id, tenant_id=tenant_id)
        if run is None:
            return None
        if run.status != "waiting_approval":
            return run
        run = run.model_copy(update={"status": "running", "updated_at": _now()})
        self._runs[run_id] = run
        await self._event(run, "run.approved", "running", {})
        return run

    async def _require_task(self, task_id, user_id, tenant_id) -> TaskState:
        state = await self.task_store.get(task_id, user_id=user_id, tenant_id=tenant_id)
        if state is None:
            raise TaskStateNotFoundError("task not found")
        return state

    async def _event(
        self,
        run: AgentRunRecord,
        event_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        sequence = len(self._events.setdefault(run.run_id, [])) + 1
        event = AgentEvent(
            event_id=f"{run.run_id}:{sequence}",
            sequence=sequence,
            run_id=run.run_id,
            task_id=run.task_id,
            turn_id=run.turn_id,
            event_type=event_type,
            status=status,
            payload=payload,
        )
        self._events[run.run_id].append(event)
        if self.event_store is not None:
            await self.event_store.append(
                run_id=run.run_id,
                event_type=event_type,
                status=status,
                payload=payload,
                turn_id=run.turn_id,
                deduplication_key=event.event_id,
            )
        return event
