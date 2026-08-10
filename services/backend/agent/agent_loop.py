"""Agent Loop 运行时 -- 阶段一 Step 5。

有界 Think->Act->Observe 循环：
  1. 检查 cancel/deadline/token/cost/tool count
  2. 调用 invoke_agent_step（非流式 ainvoke）
  3. 无 tool call -> 校验并结束
  4. 校验 tool name/args/policy
  5. 检测重复动作
  6. 限并发执行只读工具
  7. 按原 tool call 顺序追加 ToolMessage
  8. 更新预算、事件和 messages

基于 Step 1 探针结论：非流式 ainvoke 的 tool_calls 稳定可靠。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import Counter
from datetime import datetime, timezone

from agent.agent_contracts import (
    AgentEvent,
    BudgetUsage,
    EventType,
    LoopBudget,
    LoopResult,
    LoopStatus,
    RunContext,
    ToolPolicy,
    TypedToolResult,
)
from agent.llm_wrapper import LLMWrapper
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger("backend.agent.agent_loop")


class AgentLoop:
    """有界 Agent Loop 运行时。

    每次 run() 执行一轮 Think->Act->Observe 循环，直到：
      - 模型不再请求工具调用（正常结束）
      - 达到轮次/预算/deadline 上限（降级结束）
      - 被取消
    """

    def __init__(
        self,
        *,
        llm_wrapper: LLMWrapper,
        tools: list,
        budget: LoopBudget,
        run_context: RunContext,
        tool_policies: dict[str, ToolPolicy] | None = None,
        max_repeated_actions: int = 2,
    ):
        self.llm_wrapper = llm_wrapper
        self.tools_by_name: dict[str, any] = {}
        for t in tools:
            self.tools_by_name[t.name] = t
        self.budget = budget
        self.run_context = run_context
        self.tool_policies = tool_policies or {}
        self.max_repeated_actions = max_repeated_actions
        self._semaphore = asyncio.Semaphore(budget.max_parallel_tools)

    async def run(
        self,
        *,
        system_prompt: str,
        user_message: str,
        initial_context: str = "",
    ) -> LoopResult:
        """执行 Agent Loop。

        Args:
            system_prompt: 系统提示词
            user_message: 用户消息
            initial_context: 初始上下文（文章/草稿摘要，可选）

        Returns:
            LoopResult
        """
        usage = BudgetUsage()
        events: list[AgentEvent] = []
        seq = 0
        tool_names_used: list[str] = []
        references: list[str] = []
        action_history: list[tuple[str, str]] = []  # (tool_name, args_hash)

        # 构建消息
        messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
        if initial_context:
            messages.append(HumanMessage(content=f"上下文:\n{initial_context}\n\n用户问题: {user_message}"))
        else:
            messages.append(HumanMessage(content=user_message))

        # 绑定工具
        bound_llm = self.llm_wrapper.llm.bind_tools(list(self.tools_by_name.values()))

        # LOOP_START 事件
        events.append(AgentEvent(
            type=EventType.LOOP_START, sequence=seq, run_id=self.run_context.run_id,
            trace_id=self.run_context.trace_id, round_no=0,
        ))
        seq += 1

        cancelled = False

        while usage.can_continue(self.budget):
            round_no = usage.rounds
            usage.rounds += 1

            # ROUND_START
            events.append(AgentEvent(
                type=EventType.ROUND_START, sequence=seq, run_id=self.run_context.run_id,
                trace_id=self.run_context.trace_id, round_no=round_no,
            ))
            seq += 1

            # 检查取消
            if self.run_context.is_expired():
                events.append(AgentEvent(
                    type=EventType.CANCEL, sequence=seq, run_id=self.run_context.run_id,
                    trace_id=self.run_context.trace_id, round_no=round_no,
                    error_code="deadline_expired",
                ))
                cancelled = True
                break

            # Think: 调用 LLM（非流式）
            try:
                response = await self.llm_wrapper.invoke_agent_step(
                    bound_llm=bound_llm,
                    messages=messages,
                    run_context=self.run_context,
                    loop_round=round_no,
                )
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as e:
                logger.exception("[%s] LLM step failed on round %d", self.run_context.trace_id, round_no)
                events.append(AgentEvent(
                    type=EventType.TOOL_FAILED, sequence=seq, run_id=self.run_context.run_id,
                    trace_id=self.run_context.trace_id, round_no=round_no,
                    error_code=type(e).__name__,
                ))
                seq += 1
                # 降级：尝试 finalization
                return await self._finalize(
                    messages=messages, usage=usage, events=events, seq=seq,
                    tool_names_used=tool_names_used, references=references,
                    status=LoopStatus.DEGRADED, degraded=True,
                    degrade_reason=f"LLM failed on round {round_no}: {type(e).__name__}",
                    bound_llm=bound_llm,
                )

            # 累加 token
            input_t, output_t = self.llm_wrapper._agent_token_usage(messages, response)
            usage.input_tokens += input_t
            usage.output_tokens += output_t

            # 检查是否有工具调用
            tool_calls = getattr(response, "tool_calls", []) or []

            if not tool_calls:
                # 无工具调用 -> 最终回答
                answer = response.content if isinstance(response.content, str) else str(response.content)
                answer = answer.strip()

                events.append(AgentEvent(
                    type=EventType.ROUND_END, sequence=seq, run_id=self.run_context.run_id,
                    trace_id=self.run_context.trace_id, round_no=round_no,
                ))
                seq += 1
                events.append(AgentEvent(
                    type=EventType.LOOP_END, sequence=seq, run_id=self.run_context.run_id,
                    trace_id=self.run_context.trace_id, round_no=round_no,
                ))

                return LoopResult(
                    status=LoopStatus.COMPLETED,
                    answer=answer,
                    rounds=usage.rounds,
                    usage=usage,
                    references=references,
                    events=events,
                    tool_names_used=tool_names_used,
                )

            # Act: 执行工具调用
            messages.append(response)  # 追加 AIMessage（含 tool_calls）

            # 校验并执行工具
            tool_results: list[tuple[str, str, ToolMessage]] = []  # (tool_name, tool_call_id, message)

            for tc in tool_calls:
                tc_name = tc.get("name", "")
                tc_id = tc.get("id", "")
                tc_args = tc.get("args", {})

                # 校验工具名
                if tc_name not in self.tools_by_name:
                    msg = f"[工具不存在] {tc_name} 不在可用工具列表中"
                    tool_results.append((tc_name, tc_id, ToolMessage(content=msg, tool_call_id=tc_id)))
                    events.append(AgentEvent(
                        type=EventType.TOOL_BLOCKED, sequence=seq, run_id=self.run_context.run_id,
                        trace_id=self.run_context.trace_id, round_no=round_no,
                        tool_name=tc_name, error_code="tool_not_found",
                    ))
                    seq += 1
                    continue

                # 重复动作检测
                args_hash = self._args_hash(tc_args)
                action_key = (tc_name, args_hash)
                action_history.append(action_key)
                repeat_count = action_history.count(action_key)

                if repeat_count > self.max_repeated_actions:
                    msg = f"[重复操作] {tc_name} 已执行相同操作 {repeat_count} 次，请更换策略或直接回答"
                    tool_results.append((tc_name, tc_id, ToolMessage(content=msg, tool_call_id=tc_id)))
                    events.append(AgentEvent(
                        type=EventType.TOOL_BLOCKED, sequence=seq, run_id=self.run_context.run_id,
                        trace_id=self.run_context.trace_id, round_no=round_no,
                        tool_name=tc_name, tool_args_hash=args_hash,
                        error_code="repeated_action",
                    ))
                    seq += 1
                    continue

                # 执行工具
                started = time.perf_counter()
                try:
                    async with self._semaphore:
                        tool_fn = self.tools_by_name[tc_name]
                        raw_result = await asyncio.wait_for(
                            tool_fn.ainvoke(tc_args),
                            timeout=self.budget.tool_timeout_seconds,
                        )

                    duration_ms = int((time.perf_counter() - started) * 1000)
                    tool_names_used.append(tc_name)
                    usage.tool_calls += 1

                    # 提取 source_ids（从 TypedToolResult 格式中尝试解析）
                    result_text = str(raw_result)

                    tool_results.append((tc_name, tc_id, ToolMessage(content=result_text, tool_call_id=tc_id)))
                    events.append(AgentEvent(
                        type=EventType.TOOL_FINISHED, sequence=seq, run_id=self.run_context.run_id,
                        trace_id=self.run_context.trace_id, round_no=round_no,
                        tool_name=tc_name, tool_args_hash=args_hash,
                        duration_ms=duration_ms,
                    ))

                except asyncio.TimeoutError:
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    msg = f"[工具超时] {tc_name} 执行超过 {self.budget.tool_timeout_seconds}s"
                    tool_results.append((tc_name, tc_id, ToolMessage(content=msg, tool_call_id=tc_id)))
                    events.append(AgentEvent(
                        type=EventType.TOOL_FAILED, sequence=seq, run_id=self.run_context.run_id,
                        trace_id=self.run_context.trace_id, round_no=round_no,
                        tool_name=tc_name, error_code="timeout",
                        duration_ms=duration_ms,
                    ))
                except Exception as e:
                    duration_ms = int((time.perf_counter() - started) * 1000)
                    msg = f"[工具执行失败] {tc_name}: {type(e).__name__}: {str(e)[:100]}"
                    tool_results.append((tc_name, tc_id, ToolMessage(content=msg, tool_call_id=tc_id)))
                    events.append(AgentEvent(
                        type=EventType.TOOL_FAILED, sequence=seq, run_id=self.run_context.run_id,
                        trace_id=self.run_context.trace_id, round_no=round_no,
                        tool_name=tc_name, error_code=type(e).__name__,
                        duration_ms=duration_ms,
                    ))

                seq += 1

            # 按原 tool call 顺序追加 ToolMessage
            for _, _, tm in tool_results:
                messages.append(tm)

            # ROUND_END
            events.append(AgentEvent(
                type=EventType.ROUND_END, sequence=seq, run_id=self.run_context.run_id,
                trace_id=self.run_context.trace_id, round_no=round_no,
            ))
            seq += 1

        # 预算耗尽或取消 -> finalization（一次禁止工具的调用）
        if cancelled:
            return LoopResult(
                status=LoopStatus.CANCELLED,
                answer="",
                rounds=usage.rounds,
                usage=usage,
                events=events,
                tool_names_used=tool_names_used,
                degraded=True,
                degrade_reason="cancelled",
            )

        return await self._finalize(
            messages=messages, usage=usage, events=events, seq=seq,
            tool_names_used=tool_names_used, references=references,
            status=LoopStatus.BUDGET_EXCEEDED if not usage.can_continue(self.budget) else LoopStatus.MAX_ROUNDS_REACHED,
            degraded=True,
            degrade_reason="budget_or_rounds_exceeded",
            bound_llm=bound_llm,
        )

    async def _finalize(
        self,
        *,
        messages: list[BaseMessage],
        usage: BudgetUsage,
        events: list[AgentEvent],
        seq: int,
        tool_names_used: list[str],
        references: list[str],
        status: LoopStatus,
        degraded: bool,
        degrade_reason: str,
        bound_llm,
    ) -> LoopResult:
        """预算耗尽后的一次 finalization（禁止工具调用）。"""
        events.append(AgentEvent(
            type=EventType.FINALIZATION, sequence=seq, run_id=self.run_context.run_id,
            trace_id=self.run_context.trace_id, round_no=usage.rounds,
            extra={"reason": degrade_reason},
        ))
        seq += 1

        try:
            # tool_choice=none 强制不调用工具
            final_llm = self.llm_wrapper.llm.bind_tools([], tool_choice="none")
            response = await self.llm_wrapper.invoke_agent_step(
                bound_llm=final_llm,
                messages=messages,
                run_context=self.run_context,
                loop_round=usage.rounds,
            )
            answer = response.content if isinstance(response.content, str) else str(response.content)
            answer = answer.strip()
        except Exception:
            answer = "基于当前上下文，暂无法提供完整回答。"

        events.append(AgentEvent(
            type=EventType.LOOP_END, sequence=seq, run_id=self.run_context.run_id,
            trace_id=self.run_context.trace_id, round_no=usage.rounds,
        ))

        return LoopResult(
            status=LoopStatus.DEGRADED if degraded else status,
            answer=answer,
            rounds=usage.rounds,
            usage=usage,
            events=events,
            tool_names_used=tool_names_used,
            references=references,
            degraded=degraded,
            degrade_reason=degrade_reason,
        )

    @staticmethod
    def _args_hash(args: dict) -> str:
        """计算工具入参 hash。"""
        combined = str(sorted(args.items())) if isinstance(args, dict) else str(args)
        return f"sha256:{hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]}"
