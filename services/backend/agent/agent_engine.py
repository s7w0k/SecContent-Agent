"""聊天式 Agent 引擎 — 真正的 LLM tool-calling loop（对齐市面主流 Agent）。

在轻量、可观测的循环里让 LLM 自主完成：
    规划(想到要做哪一步) -> 调用工具(执行) -> 观察结果 -> 验证/再规划 -> 交付终稿

每一轮：
  1. 把 {系统提示 + 历史(用户/助手) + 上一轮的工具观察} 交给绑定工具的 LLM；
  2. LLM 返回 narration（它"说"给用户的思考/计划）或 tool_calls；
  3. 无 tool_calls 且返回了文本 -> 这就是最终交付，结束；
  4. 有 tool_calls -> 引擎逐个执行真实业务工具，把结果回灌给 LLM，进入下一轮
     （LLM 根据观察继续规划下一步）。

所有中间态都以 SSE 事件实时外发（agent_message / tool_call / tool_result /
tool_error / final / done），由前端聊天页原样流式渲染——这正是主流 Agent
工作台（Claude / OpenAI）体验。

本引擎刻意不持有预算状态机 / 审批 / 认知存储等重设施，聚焦"LLM 决策 + 工具执行"
这条主线，量级可控、易于理解；需要时可再叠加 BudgetManager 等组件。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from agent.business_tools.contracts import ToolRequestContext
from agent.llm_wrapper import LLMWrapper, UnsupportedToolCallError
from agent.retry import RetryState
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool

logger = logging.getLogger("backend.agent.agent_engine")

MAX_TOOL_MESSAGE_CHARS = 8000
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_TOKENS = 6000  # 对话历史 token 预算（超出时压缩为最近 + 摘要）
MAX_MESSAGE_CHARS = 4000
DEFAULT_MAX_ROUNDS = 8

# 显式 Plan：单次计划步骤上限（与服务端白名单/预算上限一致）
_PLAN_MAX_STEPS = 6

# 副作用等级排序，用于 HITL 审批门判定（等级越高的工具越需要人工确认）
_SIDE_EFFECT_ORDER = {"L1": 1, "L2": 2, "L3": 3}

# DeepSeek 工具调用的特殊标记（模型会把工具调用以 `<｜tool_calls｜>...` 写进 content）
_DS_OPEN = r"<[｜|]tool_calls[｜|]>"
_DS_CLOSE = r"<[｜|]/tool_calls[｜|]>"
TOOL_BLOCK_RE = re.compile(_DS_OPEN + r".*?" + _DS_CLOSE, re.DOTALL)
_SINGLE_TAG_RE = re.compile(r"^\s*<[｜|](?:invoke|/?tool_calls|/?/tool_calls|/?parameter).*?>\s*$", re.MULTILINE)

# 计划性口吻标记：当模型只输出"我打算/我先/接下来要…"之类的计划叙述、
# 却没有实际调用工具产出结果时，判定为"把计划当交付"，应引导其继续执行工具而非直接收尾。
_PLAN_INTENT_RE = re.compile(
    r"(让我先|我将|我先|我打算|接下来(要|应该)?|需要先|我的计划是|准备(去|要)|考虑(一下)?(后|先)?(选择|匹配))"
)
_MAX_PLAN_NUDGE = 2


def clean_narration(text: str) -> str:
    """剥掉 LLM 参杂在正文里的工具调用特殊标记，只留真正的叙述文本。"""
    if not text:
        return ""
    cleaned = TOOL_BLOCK_RE.sub("", text)
    cleaned = _SINGLE_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def build_tool_defs(registry) -> list[StructuredTool]:
    """仅用于给 LLM bind_tools 生成 schema；执行由引擎调用 BusinessToolExecutor。"""
    defs: list[StructuredTool] = []

    async def _noop(_args: Any) -> str:  # pragma: no cover - 从不执行
        return "execute via engine"

    for name in registry.names():
        contract = registry.get(name)
        defs.append(
            StructuredTool.from_function(
                coroutine=_noop,
                name=name,
                description=contract.description,
                args_schema=contract.args_schema,
            )
        )
    return defs


def summarize_result(tool_name: str, result: Any) -> dict[str, Any]:
    """把工具返回的 pydantic 模型压成前端友好的摘要 dict。

    generate_draft / revise_draft 会携带完整正文（content），由聊天页渲染成初稿卡片。
    """
    data: dict[str, Any] = {}
    try:
        data = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    except Exception:
        data = {"raw": str(result)[:2000]}

    if tool_name in ("list_articles", "search_news"):
        items = data.get("items") or []
        data["_summary"] = {
            "kind": "list",
            "count": data.get("total", len(items)),
            "items": [
                {
                    "article_id": it.get("article_id", ""),
                    "title": it.get("title", ""),
                    "source": it.get("source", ""),
                    "published_at": it.get("published_at", ""),
                }
                for it in items[:8]
            ],
        }
    elif tool_name == "get_article":
        art = data.get("article") or {}
        data["_summary"] = {
            "kind": "article",
            "found": data.get("found", False),
            "article_id": art.get("article_id", ""),
            "title": art.get("title", ""),
            "source": art.get("source", ""),
            "content_available": bool(art.get("content") or art.get("content_available")),
        }
    elif tool_name == "classify_article":
        data["_summary"] = {
            "kind": "classify",
            "category": data.get("category", ""),
            "security_domain": data.get("security_domain", ""),
            "confidence": data.get("confidence", 0),
            "eligible": data.get("eligible", False),
            "conflict": data.get("conflict", ""),
            "reason": data.get("reason", ""),
        }
    elif tool_name == "match_products":
        data["_summary"] = {
            "kind": "match",
            "outcome": data.get("outcome", ""),
            "candidates": [
                {
                    "product_id": c.get("product_id", ""),
                    "name": c.get("name", ""),
                    "confidence": c.get("confidence", 0),
                }
                for c in (data.get("candidates") or [])
            ],
        }
    elif tool_name == "score_article":
        data["_summary"] = {
            "kind": "score",
            "total_score": data.get("total_score", 0),
            "product_relevance": data.get("product_relevance", {}).get("score", 0),
            "event_impact": data.get("event_impact", {}).get("score", 0),
            "worth_writing": data.get("worth_writing", False),
            "anomalies": data.get("anomalies") or [],
        }
    elif tool_name in ("generate_draft", "revise_draft"):
        artifact = data.get("artifact") or {}
        data["_summary"] = {
            "kind": "draft",
            "artifact_id": artifact.get("artifact_id", ""),
            "version": artifact.get("version", 0),
            "summary": data.get("summary", ""),
            "has_content": bool(data.get("content")),
        }
    elif tool_name == "review_draft":
        data["_summary"] = {
            "kind": "review",
            "passed": data.get("passed", False),
            "artifact_id": (data.get("artifact") or {}).get("artifact_id", ""),
            "issues": [
                {"severity": i.get("severity", ""), "message": i.get("message", "")}
                for i in (data.get("issues") or [])
            ],
        }
    elif tool_name == "crawl_news":
        data["_summary"] = {
            "kind": "crawl",
            "status": data.get("status", ""),
            "added": data.get("added", 0),
            "updated": data.get("updated", 0),
            "task_ref": data.get("task_ref", ""),
        }
    elif tool_name == "export_draft":
        data["_summary"] = {
            "kind": "export",
            "export_ref": data.get("export_ref", ""),
            "format": data.get("format", ""),
        }
    elif tool_name == "save_draft_version":
        data["_summary"] = {
            "kind": "save",
            "saved": data.get("saved", False),
            "kind": data.get("kind", ""),
            "duplicate": data.get("duplicate", False),
        }
    return data


def _tool_message_content(tool_name: str, result: Any) -> str:
    """给 LLM 回灌的工具观察文本（适合作为下轮上下文）。"""
    if not hasattr(result, "model_dump_json"):
        return str(result)[:MAX_TOOL_MESSAGE_CHARS]
    text = result.model_dump_json(exclude_none=True)
    if len(text) > MAX_TOOL_MESSAGE_CHARS:
        text = text[:MAX_TOOL_MESSAGE_CHARS] + "\n...(结果已截断)"
    return text


class AgentEngine:
    """对一个用户消息执行一轮真正的 LLM tool-calling 循环。

    Args:
        llm_wrapper: LLMWrapper（decision 调用 + usage/重试）
        executor: BusinessToolExecutor（真实工具执行）
        registry: BusinessToolRegistry（用于生成 tool schema）
        tool_ctx: ToolRequestContext（身份/权限/scope 注入每个工具调用）
        adapter: BusinessToolAdapterKind（默认 PRODUCTION）
        run_context: 本次生成的 RunContext（trace/turn 归属）
        event_sink: 异步事件回调 (sequence, event_type, payload) -> None
        max_rounds: 最大循环轮数（LLM 决策轮）
    """

    def __init__(
        self,
        *,
        llm_wrapper: LLMWrapper,
        executor,
        registry,
        tool_ctx: ToolRequestContext,
        adapter: str,
        run_context,
        event_sink,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        approval_gate: Callable[[str, dict, str], Awaitable[bool | None]] | None = None,
        hitl_enabled: bool = True,
        hitl_min_side_effect: str = "L2",
        history_tokens: int = MAX_HISTORY_TOKENS,
        # 显式 Plan（形态 A，改造计划 P1）：传入后每条消息执行前先产出步骤计划并推 SSE 'plan'。
        # 默认 None = 关闭，行为与旧版完全一致；initial_messages（断点续跑）不再重复出计划。
        explicit_planner: Callable[[str], Awaitable[Any]] | None = None,
    ):
        self.llm_wrapper = llm_wrapper
        self.executor = executor
        self.registry = registry
        self.tool_ctx = tool_ctx
        self.adapter = adapter
        self.run_context = run_context
        self.event_sink = event_sink
        self.max_rounds = max_rounds
        self.history_tokens = max(1024, int(history_tokens))
        # HITL 审批门：调用方注入 async 回调 (tool_name, args, call_id) -> bool|None。
        #   None 表示该工具无需审批（跳过）；True 批准执行；False 跳过该工具不执行。
        self.approval_gate = approval_gate
        self.hitl_enabled = hitl_enabled
        self.hitl_min_side_effect = hitl_min_side_effect
        self.tool_defs = build_tool_defs(registry)
        self._seq = 0
        # 中断协作：外部可通过 stop() 置位；协程检查后优雅退出并保留现场
        self._cancelled = False
        # 显式 Plan 状态
        self.explicit_planner = explicit_planner
        self._plan_order: list[str] = []
        self._plan_steps: dict[str, dict[str, Any]] = {}

    # ── 中断协作 ───────────────────────────────────────────
    def stop(self) -> None:
        """请求中断当前循环（协作式，非强制）。"""
        self._cancelled = True

    def _interruptible_point(self) -> bool:
        return self._cancelled

    # ── 事件 ─────────────────────────────────────────────
    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._seq += 1
        try:
            await self.event_sink(self._seq, event_type, payload)
        except Exception:
            logger.exception("[agent_engine] event_sink failed")

    # ── 显式 Plan（形态 A）────────────────────────────────
    async def _emit_explicit_plan(self, goal: str) -> dict[str, Any] | None:
        """执行前：调用显式 planner 产出步骤计划并推送 SSE 'plan' 事件。

        planner 输出会经 sanitize_plan 清洗：仅保留 registry 白名单内的工具、
        非空步骤、受 _PLAN_MAX_STEPS 约束 —— 上游（模型/调用方）不能注入任意步骤。
        """
        if self.explicit_planner is None:
            return None
        try:
            raw = await self.explicit_planner(goal)
        except Exception:
            logger.exception("[agent_engine] explicit_planner failed; run without plan")
            return None
        from agent.plan_explicit import sanitize_plan

        plan = sanitize_plan(
            raw,
            allowed_tools=set(self.registry.names()),
            max_steps=_PLAN_MAX_STEPS,
            run_id=self.run_context.run_id,
        )
        if plan is None:
            return None
        self._plan_order = [s.step_id for s in plan.steps]
        self._plan_steps = {
            s.step_id: {"tools": set(s.tools), "status": "pending"} for s in plan.steps
        }
        payload: dict[str, Any] = {
            "run_id": self.run_context.run_id,
            "steps": [
                {
                    "step_id": s.step_id,
                    "title": s.title,
                    "tools": s.tools,
                    "expected_output": s.expected_output,
                    "status": "pending",
                }
                for s in plan.steps
            ],
        }
        await self._emit("plan", payload)
        return payload

    async def _mark_plan_progress(self, tool_name: str) -> None:
        """某个工具成功后，把计划里第一个覆盖它的待办步骤标记为 completed。"""
        for step_id in self._plan_order:
            entry = self._plan_steps.get(step_id)
            if entry is None or entry["status"] != "pending" or tool_name not in entry["tools"]:
                continue
            entry["status"] = "completed"
            await self._emit(
                "plan_step",
                {
                    "run_id": self.run_context.run_id,
                    "step_id": step_id,
                    "status": "completed",
                    "tool": tool_name,
                },
            )
            return

    # ── 对外入口 ─────────────────────────────────────────
    async def run(
        self,
        *,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        initial_messages: list[dict] | None = None,
    ) -> dict:
        """执行循环，返回终态。

        Parameters:
            initial_messages: 若提供（来自中断快照），则从该处续跑，而不是从
                system_prompt+history 重启。续跑时当前 user_message 会追加为新的
                用户消息（相当于用户在中断后说的"继续"指令）。

        Returns:
            {"status": str, "final_text": str, "draft": dict|None, "rounds": int, "tools_used":[..],
             "interrupted": bool, "snapshot": list[dict]|None}
        """
        bound_llm = self.llm_wrapper.llm.bind_tools(self.tool_defs)
        if initial_messages:
            messages: list[BaseMessage] = [self._deserialize_message(m) for m in initial_messages]
        else:
            messages = [SystemMessage(content=system_prompt)]
            messages += self._build_history(history)
        messages.append(HumanMessage(content=user_message))

        await self._emit("run_started", {"run_id": self.run_context.run_id})

        # 显式 Plan（形态 A）：非断点续跑时才在首次模型决策前产出用户可见计划
        if not initial_messages:
            await self._emit_explicit_plan(user_message)

        final_text = ""
        status = "completed"
        tools_used: list[str] = []
        draft: dict[str, Any] | None = None
        trace: list[dict[str, str]] = []
        turn = 0
        interrupted = False
        plan_nudges = 0

        while turn < self.max_rounds:
            if self._interruptible_point():
                interrupted = True
                break
            turn += 1
            response: AIMessage = await self._invoke(bound_llm, messages, turn)
            if response is None:
                status = "error"
                break

            narration = clean_narration(self._content_of(response))
            if narration:
                await self._emit("agent_message", {"run_id": self.run_context.run_id, "content": narration})
                trace.append({"type": "text", "text": narration[:400]})

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                # 模型回复里带了工具调用标记，但未解析成结构化 tool_call：
                # 不要把原始标记当作最终答复，追加引导让模型重新发起 or 直接交付。
                if TOOL_BLOCK_RE.search(self._content_of(response)):
                    messages.append(response)
                    messages.append(
                        HumanMessage(
                            content="检测到你上一条回复带有工具调用标记但未被系统识别。"
                            "请使用系统提供的工具发起明确的工具调用，或者直接给出最终答复。"
                        )
                    )
                    if len(messages) > MAX_HISTORY_MESSAGES + 20:
                        messages = self._trim(messages)
                    continue
                # 验证：必须给出非空交付。
                # 兜底：若模型只输出"计划性叙述"（如"让我先…/我打算…"）却未实际调用工具产出结果，
                # 引导它继续执行工具，避免把"思考/计划"误当最终交付（导致没有初稿）。
                final_text = narration
                if (
                    final_text
                    and plan_nudges < _MAX_PLAN_NUDGE
                    and _PLAN_INTENT_RE.search(final_text)
                ):
                    plan_nudges += 1
                    messages.append(response)
                    messages.append(
                        HumanMessage(
                            content="你刚才只是描述了自己的计划/思路，还没有真正产出结果。请不要只停留在"
                            "叙述计划：请直接调用相应工具实际执行（例如生成初稿），只有在真正生成并给出"
                            "最终内容后才能作为最终答复。"
                        )
                    )
                    if len(messages) > MAX_HISTORY_MESSAGES + 20:
                        messages = self._trim(messages)
                    continue
                if not final_text:
                    status = "empty_answer"
                    await self._emit(
                        "error",
                        {"run_id": self.run_context.run_id, "error": "模型未返回任何内容"},
                    )
                else:
                    await self._emit("final", {"run_id": self.run_context.run_id, "content": final_text})
                break

            # 执行本轮全部工具调用
            messages.append(response)
            for tc in tool_calls:
                name = str(tc.get("name", ""))
                call_id = str(tc.get("id", ""))
                args = tc.get("args") or {}
                if name not in self.registry.names():
                    await self._emit(
                        "tool_error",
                        {
                            "run_id": self.run_context.run_id,
                            "id": call_id,
                            "name": name,
                            "error": f"未知工具：{name}",
                        },
                    )
                    messages.append(
                        ToolMessage(content=f"[工具错误] 未知工具 {name}，请使用系统提供的工具。", tool_call_id=call_id)
                    )
                    continue

                await self._emit(
                    "tool_call",
                    {"run_id": self.run_context.run_id, "id": call_id, "name": name, "args": args},
                )
                trace.append({"type": "tool", "name": name})

                # ── HITL 审批门：高风险/写操作在真正执行前先征求用户确认 ──
                if self._hitl_needed(name):
                    gate = self.approval_gate
                    if gate is not None:
                        decision = await gate(name, args, call_id)
                    else:
                        # 未注入审批回调：默认放行（退化为纯自主）
                        decision = True
                    if decision is False:
                        await self._emit(
                            "tool_error",
                            {
                                "run_id": self.run_context.run_id,
                                "id": call_id,
                                "name": name,
                                "error": "用户未批准该操作，已跳过执行",
                            },
                        )
                        messages.append(
                            ToolMessage(
                                content=f"[用户拒绝] 用户未批准执行 {name}，该步骤已跳过。",
                                tool_call_id=call_id,
                            )
                        )
                        continue

                try:
                    result = await self.executor.invoke(
                        name, args, context=self.tool_ctx, adapter=self.adapter
                    )
                    summary = summarize_result(name, result)
                    if name in ("generate_draft", "revise_draft") and summary.get("content"):
                        draft = {
                            "tool": name,
                            "heading": f"PR 初稿 #{summary.get('version', 1)}"
                            if name == "generate_draft"
                            else "修订稿",
                            "content": summary.get("content", ""),
                        }
                    await self._emit(
                        "tool_result",
                        {
                            "run_id": self.run_context.run_id,
                            "id": call_id,
                            "name": name,
                            "summary": summary.get("_summary", {}),
                        },
                    )
                    messages.append(
                        ToolMessage(content=_tool_message_content(name, result), tool_call_id=call_id)
                    )
                    if name not in tools_used:
                        tools_used.append(name)
                    await self._mark_plan_progress(name)
                except Exception as exc:
                    await self._emit(
                        "tool_error",
                        {
                            "run_id": self.run_context.run_id,
                            "id": call_id,
                            "name": name,
                            "error": f"{type(exc).__name__}: {str(exc)}"[:400],
                        },
                    )
                    messages.append(
                        ToolMessage(content=f"[工具执行失败] {type(exc).__name__}: {str(exc)}"[:1000], tool_call_id=call_id)
                    )

                if len(messages) > MAX_HISTORY_MESSAGES + 20:
                    messages = self._trim(messages)

        if interrupted:
            # 用户中断：不强制收尾，保留现场快照供"继续"续跑
            snapshot = self._serialize_messages(messages)
            status = "interrupted"
            await self._emit("interrupted", {"run_id": self.run_context.run_id, "rounds": turn})
        else:
            if turn >= self.max_rounds and not final_text:
                # 未在预算内收敛：做一次强制收尾（不再允许工具调用）
                final_text = await self._finalize(bound_llm, messages)
                status = "max_rounds" if not final_text else "completed"
                if final_text:
                    await self._emit("final", {"run_id": self.run_context.run_id, "content": final_text})

            if final_text:
                await self._emit("done", {"run_id": self.run_context.run_id, "status": status})
            snapshot = self._serialize_messages(messages) if not final_text else None

        return {
            "status": status,
            "final_text": final_text,
            "draft": draft,
            "rounds": turn,
            "tools_used": tools_used,
            "trace": trace,
            "interrupted": interrupted,
            "snapshot": snapshot,
        }

    # ── 内部 ─────────────────────────────────────────────
    def _hitl_needed(self, tool_name: str) -> bool:
        """判断某个工具是否需要人工审批（HITL）。

        依据工具契约的副作用等级 side_effect_level：
          - L1 只读：无需审批
          - L2+/…：副作用等级 >= hitl_min_side_effect 则需要审批
        未命中注册表/取不到等级时按"无需审批"放行（保守不打断）。
        """
        if not self.hitl_enabled or self.approval_gate is None:
            return False
        try:
            contract = self.registry.get(tool_name)
        except Exception:
            return False
        level = getattr(contract, "side_effect_level", None)
        try:
            lv = str(level or "L1")
            if lv not in _SIDE_EFFECT_ORDER:
                return False
            return _SIDE_EFFECT_ORDER.get(lv, 1) >= _SIDE_EFFECT_ORDER.get(
                self.hitl_min_side_effect, 2
            )
        except Exception:
            return False

    async def _invoke(self, bound_llm, messages: list[BaseMessage], turn: int) -> AIMessage | None:
        try:
            retry_state = RetryState()
            return await self.llm_wrapper.invoke_agent_step(
                bound_llm=bound_llm,
                messages=messages,
                run_context=self.run_context,
                loop_round=turn - 1,
                retry_state_out=retry_state,
            )
        except UnsupportedToolCallError as exc:
            await self._emit(
                "tool_error",
                {"run_id": self.run_context.run_id, "id": "", "name": "?", "error": f"模型幻觉工具：{str(exc)[:300]}"},
            )
            logger.warning("[agent_engine] unsupported tool call: %s", exc)
            return self._pseudo_notice(str(exc))
        except Exception as exc:
            await self._emit(
                "error",
                {"run_id": self.run_context.run_id, "error": f"决策调用失败：{type(exc).__name__}"},
            )
            logger.exception("[agent_engine] LLM decision failed")
            return None

    @staticmethod
    def _pseudo_notice(text: str) -> AIMessage:
        return AIMessage(
            content="系统提示：模型尝试调用了一个不可用的工具，请仅使用提供的工具继续。",
            additional_kwargs={"note": text[:200]},
        )

    @staticmethod
    def _content_of(response: AIMessage) -> str:
        content = getattr(response, "content", "")
        if isinstance(content, str):
            return content
        return str(content or "")

    def _build_history(self, history: list[dict]) -> list[BaseMessage]:
        """把历史压缩进 token 预算：优先保留最近消息，超预算时按 token 截断最旧。"""
        out: list[BaseMessage] = []
        budget = self.history_tokens
        used = 0
        # 从最近的开始往回累积，直到预算用尽；这样最旧消息优先被挤出
        for item in reversed(history[-MAX_HISTORY_MESSAGES * 2:]):
            role = str(item.get("role", ""))
            content = str(item.get("content", ""))[:MAX_MESSAGE_CHARS]
            msg_tokens = max(1, len(content) // 4)
            if used + msg_tokens > budget and out:
                break
            used += msg_tokens
            if role == "user":
                out.append(HumanMessage(content=content or "(用户消息)"))
            elif role == "assistant":
                out.append(AIMessage(content=content))
        out.reverse()
        return out

    @staticmethod
    def _estimate_messages_tokens(messages: list[BaseMessage]) -> int:
        total = 0
        for m in messages:
            content = getattr(m, "content", "")
            if isinstance(content, str):
                total += max(1, len(content) // 4)
            # ToolMessage 还可能带额外 metadata，但 content 已覆盖主体
        return total

    def _trim(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """把消息列表压缩进 token 预算：保留 system + 最近消息，丢弃中间最旧历史。

        工具循环里 messages 同时含 用户/助手历史 与 工具调用-观察 配对。
        ToolMessage 必须紧跟其触发的 AIMessage，因此只对【最近的工具观察之前的
        旧对话前缀】做 token 级丢弃，绝不切断未完成的工具配对。
        """
        system = next((m for m in messages if isinstance(m, SystemMessage)), None)
        non_system = [m for m in messages if not isinstance(m, SystemMessage)]

        # 定位"工具驻留段"：找到最后一轮用户消息的位置，它之后的都必须保留
        # （这部分是正在进行的循环上下文），它之前的旧对话才是可压缩前缀。
        last_user_idx = None
        for i in range(len(non_system) - 1, -1, -1):
            if isinstance(non_system[i], HumanMessage):
                last_user_idx = i
                break

        if last_user_idx is None:
            # 没有用户消息（异常），保底保留最近若干条
            tail = non_system[-MAX_HISTORY_MESSAGES:]
        else:
            tail = non_system[last_user_idx:]

        budget = self.history_tokens
        # Tail 始终全量保留（含正在进行的工具配对），只计算它占用
        prefix = non_system[: last_user_idx if last_user_idx is not None else 0]
        used = AgentEngine._estimate_messages_tokens(tail)

        kept_prefix: list[BaseMessage] = []
        for m in reversed(prefix):
            content = getattr(m, "content", "")
            tokens = max(1, len(content) // 4) if isinstance(content, str) else 0
            if used + tokens > budget:
                break
            used += tokens
            kept_prefix.append(m)
        kept_prefix.reverse()

        head = (([system] if system else []) + kept_prefix + tail)
        return head

    async def _finalize(self, bound_llm, messages: list[BaseMessage]) -> str:
        """强制收尾：重新 bind_tools([])，要求模型直接交付文本。"""
        try:
            final_llm = self.llm_wrapper.llm.bind_tools([], tool_choice="none")
            response = await self.llm_wrapper.invoke_agent_step(
                bound_llm=final_llm,
                messages=messages,
                run_context=self.run_context,
                loop_round=self.max_rounds,
            )
            return clean_narration(self._content_of(response))
        except Exception:
            logger.exception("[agent_engine] finalize failed")
            return ""

    # ── 断点快照（用于"继续"续跑） ─────────────────────
    def _serialize_messages(self, messages: list[BaseMessage]) -> list[dict]:
        """把引擎内部 messages 序列化为可持久化的 dict 列表。"""
        out: list[dict] = []
        for m in messages:
            if isinstance(m, SystemMessage):
                out.append({"role": "system", "content": m.content})
            elif isinstance(m, HumanMessage):
                out.append({"role": "user", "content": m.content})
            elif isinstance(m, AIMessage):
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content or "",
                        "tool_calls": list(getattr(m, "tool_calls", []) or []),
                    }
                )
            elif isinstance(m, ToolMessage):
                out.append(
                    {
                        "role": "tool",
                        "content": m.content or "",
                        "tool_call_id": m.tool_call_id,
                    }
                )
            else:
                out.append({"role": "assistant", "content": getattr(m, "content", "") or ""})
        return out

    def _deserialize_message(self, raw: dict) -> BaseMessage:
        """把快照 dict 还原为 BaseMessage。"""
        role = raw.get("role")
        content = raw.get("content") or ""
        if role == "system":
            return SystemMessage(content=content)
        if role == "user":
            return HumanMessage(content=content)
        if role == "tool":
            return ToolMessage(content=content, tool_call_id=str(raw.get("tool_call_id") or ""))
        # assistant：还原 tool_calls（dict 形态，langchain 接受）
        tool_calls = raw.get("tool_calls") or []
        return AIMessage(content=content, tool_calls=json.loads(json.dumps(tool_calls, default=str)))