"""聊天式 Agent 线程服务：线程持久化 + SSE 事件缓冲 + 后台执行引擎。

形态与市面主流 Agent 工作台一致：
  - 用户发一条消息 -> 追加用户消息 -> 后台跑 AgentEngine（LLM tool-loop）；
  - 引擎逐事件推入线程内存缓冲（agent_message/tool_call/tool_result/...）；
  - 前端通过 SSE 实时拉取并渲染；执行完成追加助手终稿并落库。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agent.agent_contracts import RunContext
from agent.agent_engine import AgentEngine
from agent.business_tools.contracts import ToolRequestContext
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.chat_agent_service")

SYSTEM_PROMPT = """你是一名资深的安全 / Agent 产品 PR 情报与撰稿 Agent。用户的诉求通常是：根据某条安全新闻，结合公司产品，产出一篇 PR 初稿。

你拥有下列工具，请按需自主调用，循环"思考 -> 调用工具 -> 观察结果 -> 规划下一步 -> 交付"：
- list_articles:     列出近期入库的文章（标题/来源/时间）
- get_article:       按 article_id 读取某篇文章全文
- search_news:       在本地与网络搜索新闻候选
- crawl_news:        按关键词抓取一批最新新闻
- classify_article:  对一篇文章做安全六分类（判断是否与目标主题相关）
- match_products:    为一篇文章匹配公司产品
- score_article:     给"文章 x 产品"打分（PR 价值）
- generate_draft:    基于文章与产品生成 PR 初稿（返回 content 正文）
- review_draft:      检查初稿（事实/话术）
- revise_draft:      按修改意见改写初稿
- save_draft_version:保存一个稿件版本
- export_draft:      导出稿件

工作规范：
1. 先理解用户给了哪些条件。若用户没指定"哪篇新闻"或"哪个产品"，你应自行从库里物色最合适的新闻与产品（先 list_articles / search_news，再 match_products），不要反复追问。
2. 选定文章后：get_article -> classify_article -> match_products -> score_article -> generate_draft。生成成功后，把 PR 初稿作为最终回复交给用户（在最终回复里给出正文，不依赖用户再点任何按钮）。
3. 只有当信息严重不足、且你自己确实无法决定时，才用自然语言向用户提一个简短问题，然后停止（不带工具调用即视为结束本轮，等用户回复）。
4. 不要调用不在列表里的工具。若一个工具失败，先复述原因，再换一种工具或换一篇新闻继续，不要卡死。
5. 最终交付：既给出你能确定的关键判断（选的新闻、匹配的产品、打分），也要把初稿正文完整给到用户。
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 触发"从断点续跑"的关键词：仅当用户明确表达继续意图时才恢复中断任务，
# 否则像 ChatGPT/DeepSeek 一样开启新一轮对话（并丢弃未完成的中断快照）。
_RESUME_KEYWORDS = ("继续", "接着", "续跑", "往下", "continue", "go on", "keep going")


def _is_resume_intent(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(k in t for k in _RESUME_KEYWORDS)


class ChatEvent(BaseModel):
    sequence: int
    event_type: str
    run_id: str = ""
    thread_id: str
    timestamp: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ThreadMessage(BaseModel):
    role: str
    content: str = ""
    draft: dict[str, Any] | None = None
    thinking: list[dict[str, Any]] | None = None
    created_at: str = Field(default_factory=_now)


class ChatThread(BaseModel):
    thread_id: str
    user_id: str
    tenant_id: str
    title: str = ""
    status: str = "idle"  # idle | generating
    messages: list[ThreadMessage] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class _Live:
    __slots__ = ("events", "running", "task", "engine")

    def __init__(self) -> None:
        self.events: deque[ChatEvent] = deque(maxlen=2000)
        self.running = False
        self.task: asyncio.Task | None = None
        self.engine: Any = None  # 当前正在运行的 AgentEngine（用于中断协作）


class ChatAgentService:
    def __init__(
        self,
        *,
        llm_wrapper,
        executor,
        registry,
        adapter: str,
        tenant_id_default: str = "default",
        db=None,
    ):
        self.llm_wrapper = llm_wrapper
        self.executor = executor
        self.registry = registry
        self.adapter = adapter
        self.tenant_id_default = tenant_id_default
        self.db = db
        self._live: dict[str, _Live] = {}
        self._memory: dict[str, ChatThread] = {}
        # HITL 待审批任务：{approval_id: asyncio.Future[bool]}，用户确认后 set_result 恢复生成
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self.max_rounds = 8
        self.hitl_enabled = True
        self.hitl_min_side_effect = "L2"
        # 记忆 / Skill / 自进化上下文增强（受配置开关控制）
        from config import get_settings

        _settings = get_settings()
        self.memory_enabled = bool(getattr(_settings, "CHAT_AGENT_MEMORY_ENABLED", True))
        self.skill_enabled = bool(getattr(_settings, "CHAT_AGENT_SKILL_ENABLED", True))
        self.evolution_enabled = bool(getattr(_settings, "CHAT_AGENT_EVOLUTION_ENABLED", False))
        self.history_tokens = int(getattr(_settings, "CHAT_AGENT_HISTORY_TOKENS", 6000))
        self._skill_registry_cache = None
        self._memory_retriever = None
        # 所有工具 role scope 并集，作为单次调用注入的 ToolRequestContext.scopes
        self._scopes = frozenset().union(*[set(registry.get(n).required_scopes) for n in registry.names()])

    # ── 线程 CRUD ─────────────────────────────────────────
    async def create_thread(self, user_id: str, tenant_id: str) -> ChatThread:
        thread = ChatThread(
            thread_id=f"thr-{uuid4().hex[:12]}",
            user_id=user_id,
            tenant_id=tenant_id or self.tenant_id_default,
        )
        self._live[thread.thread_id] = _Live()
        await self._persist(thread)
        return thread

    async def list_threads(self, user_id: str, limit: int = 50) -> list[ChatThread]:
        query = {"user_id": user_id}
        docs: list[ChatThread] = []
        if self.db is not None:
            cursor = self.db["chat_threads"].find(query).sort("updated_at", -1).limit(limit)
            for d in await cursor.to_list(length=limit):
                docs.append(_thread_from_db(d))
        # 内存中尚未落库的活跃线程也并入
        for tid, live in self._live.items():
            if docs and any(d.thread_id == tid for d in docs):
                continue
            live_thread = self._thread_memory(tid)
            if live_thread and live_thread.user_id == user_id:
                docs.append(live_thread)
        docs.sort(key=lambda t: t.updated_at, reverse=True)
        return docs[:limit]

    async def get_thread(self, thread_id: str, user_id: str) -> ChatThread | None:
        live = self._live.get(thread_id)
        if live is not None:
            thread = self._thread_memory(thread_id)
            if thread and thread.user_id == user_id:
                return thread
        if self.db is not None:
            d = await self.db["chat_threads"].find_one({"thread_id": thread_id, "user_id": user_id})
            if d:
                t = _thread_from_db(d)
                self._live.setdefault(thread_id, _Live()).running = False
                return t
        return None

    # ── 发消息并后台执行 ───────────────────────────────────
    async def send_message(
        self,
        thread_id: str,
        user_id: str,
        content: str,
        manuscript_id: str | None = None,
    ) -> ChatThread:
        content = content.strip()
        if not content:
            raise ValueError("message cannot be empty")

        thread = await self.get_thread(thread_id, user_id)
        if thread is None:
            raise KeyError("thread not found")

        live = self._live.setdefault(thread_id, _Live())
        if live.running:
            raise ValueError("该会话正在生成中，请稍候")

        thread.messages.append(ThreadMessage(role="user", content=content))
        if not thread.title:
            thread.title = content[:24]
        thread.status = "generating"
        thread.updated_at = _now()
        await self._persist(thread)

        # 用户消息事件
        live.events.append(
            ChatEvent(
                sequence=len(live.events) + 1,
                event_type="user_message",
                thread_id=thread_id,
                timestamp=_now(),
                payload={"content": content},
            )
        )

        history = [
            {"role": m.role, "content": m.content}
            for m in thread.messages
            if m.role in ("user", "assistant") and m.content
        ][:-1]  # 去掉刚追加的当前用户消息（引擎会单独加）

        # "继续"续跑：仅当用户明确表达继续意图（存在未完成中断快照）时才从断点恢复；
        # 否则按 ChatGPT/DeepSeek 行为开启新一轮对话，但【不丢弃】断点——保留待命，
        # 用户后续随时说"继续"仍可恢复到该中断任务。
        resume_snapshot = await self._load_snapshot(thread_id)
        if resume_snapshot and not _is_resume_intent(content):
            resume_snapshot = None

        # 可选：把一份稿件作为"改稿上下文附件"注入本次生成（如前端已绑定当前稿件）
        manuscript_text = ""
        if manuscript_id and self.db is not None:
            try:
                doc = await self.db["user_manuscripts"].find_one(
                    {"_id": manuscript_id, "user_id": user_id}
                )
                if doc:
                    manuscript_text = (doc.get("content_md") or "").strip()
                else:
                    manuscript_id = None
            except Exception as exc:
                logger.warning("[chat] load manuscript %s failed: %s", manuscript_id, exc)
                manuscript_id = None

        run_context = RunContext(
            trace_id=f"chat-{uuid4().hex[:12]}",
            run_id=f"run-{uuid4().hex[:12]}",
            user_id=user_id,
            tenant_id=thread.tenant_id or self.tenant_id_default,
            deadline_at=None,
        )
        tool_ctx = ToolRequestContext(
            user_id=user_id,
            tenant_id=thread.tenant_id or self.tenant_id_default,
            scopes=self._scopes,
            run_id=run_context.run_id,
            turn_id=thread_id,
        )

        live.running = True
        live.task = asyncio.get_event_loop().create_task(
            self._run_generation(
                thread=thread,
                run_context=run_context,
                tool_ctx=tool_ctx,
                history=history,
                user_message=content,
                live=live,
                resume_snapshot=resume_snapshot,
                resumed=resume_snapshot is not None,
                manuscript_text=manuscript_text,
            )
        )
        return thread

    def _seq(self, live: _Live) -> int:
        return len(live.events) + 1

    # ── 上下文增强：记忆(Skill) + token 预算组装 system prompt ──
    def _skill_registry(self):
        """懒加载 SkillRegistry（skills 目录存在才启用，避免无目录时报错）。"""
        if not self.skill_enabled:
            return None
        if self._skill_registry_cache is None:
            from agent.skill_registry import SkillRegistry
            from config import get_settings

            skills_root = f"{get_settings().KNOWLEDGE_BASE_DIR}/skills"
            try:
                registry = SkillRegistry(skills_root)
                registry.load()
                self._skill_registry_cache = registry
            except Exception as exc:
                logger.warning("[chat] skill registry unavailable: %s", exc)
                self._skill_registry_cache = False  # 标记不可用
        return self._skill_registry_cache if self._skill_registry_cache is not False else None

    async def _memory_text(self, user_id: str) -> str:
        """检索用户长期偏好记忆，返回渲染文本（未启用/无数据/异常时返回空串）。"""
        if not self.memory_enabled or self.db is None:
            return ""
        try:
            from agent.memory_retriever import MemoryRetriever
            from models.memory import MemoryStage

            retriever = MemoryRetriever(self.db)
            pack = await retriever.retrieve(user_id, stage=MemoryStage.DRAFT)
            if pack is None or getattr(pack, "item_count", 0) <= 0:
                return ""
            return getattr(pack, "rendered_text", "") or ""
        except Exception as exc:
            logger.debug("[chat] memory retrieve failed: %s", exc)
            return ""

    async def _build_context(
        self,
        *,
        user_id: str,
        user_message: str,
        base_system_prompt: str,
        manuscript_text: str = "",
    ) -> tuple[str, dict]:
        """组装最终 system prompt + 上下文 telemetry。"""
        from agent.chat_context import build_chat_context
        from config import get_settings

        total_skill = {}
        skill_instructions: list[tuple[str, str]] = []
        skill_reasons: list[str] = []
        registry = self._skill_registry()
        if registry is not None:
            try:
                # 依据用户本次诉求召回命中 Skill；再按意图/tool 补齐，最后读取指令
                hit_names = registry.match_chat(user_message)
                for name in hit_names:
                    if name not in skill_reasons:
                        skill_reasons.append(name)
                        skill_instructions.append((name, registry.load_instructions(name)))
                for name in skill_reasons:
                    manifest = registry.snapshot.skills.get(name) if registry.snapshot else None
                    if manifest is not None:
                        total_skill[name] = manifest.version or "1.0"
            except Exception as exc:
                logger.debug("[chat] skill match failed: %s", exc)

        memory_text = await self._memory_text(user_id)

        ctx = build_chat_context(
            base_system_prompt=base_system_prompt,
            model_id=self._model_id(),
            max_input_tokens=int(get_settings().CHAT_AGENT_MAX_INPUT_TOKENS),
            skill_instructions=skill_instructions,
            memory_text=memory_text,
        )
        telemetry = ctx.telemetry()
        telemetry["skill_versions"] = total_skill

        # 用户提供的稿件（改稿上下文附件）——若存在则追加为系统级"改稿对象"，Agent 基于它来回答
        if manuscript_text:
            attach = manuscript_text[:60000]  # 超大稿件截断，避免撑爆上下文
            ctx.system_prompt += (
                "\n\n===== 用户提供的稿件（本次请基于这份稿件来回答/改稿） =====\n"
                f"{attach}"
            )
            telemetry["manuscript_attached_chars"] = len(attach)
        return ctx.system_prompt, telemetry

    def _model_id(self) -> str:
        try:
            return str(
                getattr(self.llm_wrapper.llm, "model_name", None)
                or getattr(self.llm_wrapper.llm, "model", None)
                or "deepseek-chat"
            )
        except Exception:
            return "deepseek-chat"

    # ── 自进化闭环：把一次完成的 chat run 落库为可学习信号 ──
    async def _record_generation_feedback(
        self,
        *,
        user_id: str,
        run_id: str,
        thread_id: str,
        tools_used: list[str],
        final_ok: bool,
        context_telemetry: dict,
    ) -> None:
        """记录一次 chat 生成的可学习信号（幂等，不影响主流程）。

        - 写入 generation_runs（供 evolution.DatasetBuilder 训练/评测采样）；
        - 触发一条 personalization 记忆事件（供 memory_learner 沉淀偏好）。
        自进化开关 evolution_enabled 关闭时静默跳过。
        """
        if not self.evolution_enabled or self.db is None:
            return
        try:
            import hashlib
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            third_party_text = " ".join(tools_used or [])
            await self.db["generation_runs"].insert_one(
                {
                    "generation_id": f"chat-{run_id}",
                    "user_id": user_id,
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "system_prompt_type": "chat_agent",
                    "tool_names": list(tools_used or []),
                    "tool_hash": hashlib.sha256(
                        third_party_text.encode("utf-8")
                    ).hexdigest(),
                    "category_v2": "chat",
                    "status": "completed" if final_ok else "error",
                    "generation_status": "completed" if final_ok else "error",
                    "article_url_hash": "",
                    "draft_index": None,
                    "context": context_telemetry or {},
                    "created_at": now,
                    "updated_at": now,
                }
            )
            # 记忆事件（学习用户的表达/改写偏好）
            from agent.memory_event_service import create_memory_event
            from models.memory import MemorySourceType

            await create_memory_event(
                self.db,
                user_id,
                MemorySourceType.FINAL_DIFF,
                source_id=run_id,
                generation_id=run_id,
                category_v2="chat",
                stage="revise",
                payload={
                    "channel": "chat_agent",
                    "tools_used": tools_used,
                    "final_ok": final_ok,
                },
                idempotency_key=f"chat:{run_id}",
            )
        except Exception:
            logger.debug("[chat] generation feedback record failed", exc_info=True)

    async def _run_generation(
        self,
        *,
        thread: ChatThread,
        run_context: RunContext,
        tool_ctx: ToolRequestContext,
        history: list[dict],
        user_message: str,
        live: _Live,
        resume_snapshot: list[dict] | None = None,
        resumed: bool = False,
        manuscript_text: str = "",
    ) -> None:
        def _seq() -> int:
            return len(live.events) + 1

        async def sink(seq: int, event_type: str, payload: dict[str, Any]) -> None:
            # seq 来自引擎（每轮自增）；这里以队列长度为准保证单调
            live.events.append(
                ChatEvent(
                    sequence=_seq(),
                    event_type=event_type,
                    run_id=run_context.run_id,
                    thread_id=thread.thread_id,
                    timestamp=_now(),
                    payload=payload,
                )
            )

        async def approval_gate(name: str, args: dict, call_id: str) -> bool | None:
            """HITL 审批门：广播待审批事件并暂停，等待用户确认后恢复。

            Returns:
                True=批准执行; False=拒绝; None=无需审批
            """
            approval_id = f"{run_context.run_id}:{call_id}"
            future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            self._pending_approvals[approval_id] = future
            await sink(
                0,
                "approval_requested",
                {
                    "run_id": run_context.run_id,
                    "thread_id": thread.thread_id,
                    "approval_id": approval_id,
                    "tool": name,
                    "args": dict(args) if isinstance(args, dict) else args,
                },
            )
            try:
                decision: bool = await asyncio.wait_for(future, timeout=600)
            except asyncio.TimeoutError:
                decision = False
            finally:
                self._pending_approvals.pop(approval_id, None)
            await sink(
                0,
                "approval_resolved",
                {
                    "run_id": run_context.run_id,
                    "thread_id": thread.thread_id,
                    "approval_id": approval_id,
                    "tool": name,
                    "approved": decision,
                },
            )
            return decision

        try:
            # 组装记忆(Skill)增强、受 token 预算约束的 system prompt
            system_prompt, context_telemetry = await self._build_context(
                user_id=run_context.user_id,
                user_message=user_message,
                base_system_prompt=SYSTEM_PROMPT,
                manuscript_text=manuscript_text,
            )
            await sink(0, "context", {"run_id": run_context.run_id, **context_telemetry})

            engine = AgentEngine(
                llm_wrapper=self.llm_wrapper,
                executor=self.executor,
                registry=self.registry,
                tool_ctx=tool_ctx,
                adapter=self.adapter,
                run_context=run_context,
                event_sink=sink,
                max_rounds=self.max_rounds,
                approval_gate=approval_gate if self.hitl_enabled else None,
                hitl_enabled=self.hitl_enabled,
                hitl_min_side_effect=self.hitl_min_side_effect,
                history_tokens=self.history_tokens,
            )
            live.engine = engine
            if resumed:
                await sink(0, "resumed", {"run_id": run_context.run_id, "thread_id": thread.thread_id})
            result = await engine.run(
                system_prompt=system_prompt,
                history=history,
                user_message=user_message,
                initial_messages=resume_snapshot,
            )
            interrupted = bool(result.get("interrupted"))
            if interrupted:
                # 用户中断：把现场快照落库，供下一条消息"继续"续跑；不追加空 assistant 消息
                snap = result.get("snapshot")
                if snap:
                    await self._save_snapshot(thread.thread_id, run_context.user_id, snap)
                await sink(0, "done", {"run_id": run_context.run_id, "status": "interrupted"})
            else:
                # 正常完成：仅当本次确为"继续"恢复（resumed 续跑）时才清理该断点快照；
                # 普通新回合【不清除】遗留断点，保留待命以便用户后续仍可"继续"恢复。
                if resume_snapshot:
                    await self._clear_snapshot(thread.thread_id)
                thread.messages.append(
                    ThreadMessage(
                        role="assistant",
                        content=result.get("final_text", ""),
                        draft=result.get("draft"),
                        thinking=result.get("trace"),
                    )
                )
                await self._record_generation_feedback(
                    user_id=run_context.user_id,
                    run_id=run_context.run_id,
                    thread_id=thread.thread_id,
                    tools_used=result.get("tools_used") or [],
                    final_ok=result.get("status") in ("completed", "max_rounds"),
                    context_telemetry=context_telemetry,
                )
            thread.status = "idle"
            thread.updated_at = _now()
            await self._persist(thread)
        except Exception:
            logger.exception("[chat] generation failed: thread=%s", thread.thread_id)
            live.events.append(
                ChatEvent(
                    sequence=_seq(),
                    event_type="error",
                    run_id=run_context.run_id,
                    thread_id=thread.thread_id,
                    timestamp=_now(),
                    payload={"error": "生成过程发生异常，请重试。"},
                )
            )
            thread.status = "idle"
            await self._persist(thread)
        finally:
            live.engine = None
            live.running = False

    # ── 中断协作 ─────────────────────────────────────────
    def stop_generation(self, thread_id: str) -> bool:
        """请求中断指定会话正在进行的生成（协作式）。

        Returns True 若该会话确实在生成中并已发出中断请求。
        """
        live = self._live.get(thread_id)
        if live is None or not live.running:
            return False
        if live.engine is not None:
            live.engine.stop()
        return True

    # ── 事件读取（供 SSE 轮询） ────────────────────────────
    def events(self, thread_id: str, user_id: str | None = None, last_sequence: int = 0) -> list[ChatEvent]:
        live = self._live.get(thread_id)
        if live is None:
            return []
        return [e for e in live.events if e.sequence > last_sequence]

    # ── HITL 审批确认 ────────────────────────────────────
    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        """用户对某个待审批工具调用做出决定，恢复被暂停的生成任务。

        Returns False 若 approval_id 不存在或已处理。
        """
        future = self._pending_approvals.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(bool(approved))
        return True

    # ── 持久化 ─────────────────────────────────────────────
    async def _persist(self, thread: ChatThread) -> None:
        self._memory[thread.thread_id] = thread
        if self.db is None:
            return
        try:
            doc = thread.model_dump(mode="json")
            doc["_id"] = thread.thread_id
            await self.db["chat_threads"].replace_one(
                {"thread_id": thread.thread_id}, doc, upsert=True
            )
        except Exception:
            logger.exception("[chat] persist thread failed")

    # ── 断点快照（"继续"续跑） ────────────────────────────
    async def _save_snapshot(self, thread_id: str, user_id: str, snapshot: list[dict]) -> None:
        if self.db is None:
            return
        try:
            await self.db["chat_resume_snapshots"].replace_one(
                {"thread_id": thread_id},
                {
                    "_id": thread_id,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "messages": snapshot,
                    "created_at": _now(),
                },
                upsert=True,
            )
        except Exception:
            logger.exception("[chat] save resume snapshot failed")

    async def _load_snapshot(self, thread_id: str) -> list[dict] | None:
        if self.db is None:
            return None
        try:
            doc = await self.db["chat_resume_snapshots"].find_one({"thread_id": thread_id})
            if doc and doc.get("messages"):
                return list(doc["messages"])
        except Exception:
            logger.exception("[chat] load resume snapshot failed")
        return None

    async def _clear_snapshot(self, thread_id: str) -> None:
        if self.db is None:
            return
        try:
            await self.db["chat_resume_snapshots"].delete_one({"thread_id": thread_id})
        except Exception:
            logger.exception("[chat] clear resume snapshot failed")

    def _thread_memory(self, thread_id: str) -> ChatThread | None:
        return self._memory.get(thread_id)


def _thread_from_db(d: dict[str, Any]) -> ChatThread:
    d2 = dict(d)
    d2.pop("_id", None)
    d2["messages"] = [ThreadMessage(**m) for m in (d.get("messages") or [])]
    return ChatThread(**d2)