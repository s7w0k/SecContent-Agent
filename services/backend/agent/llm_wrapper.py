"""Structured LLM invocation with persistent, privacy-safe call metadata."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import uuid4

from agent.agent_contracts import RunContext
from agent.pricing_catalog import compute_cost
from agent.retry import RetryPolicy, RetryState, with_retry
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

logger = logging.getLogger("backend.agent.llm_wrapper")

T = TypeVar("T", bound=BaseModel)

PROMPT_VERSIONS = {
    "classifier_v2": "v2.1",
    "scorer_v2": "v2.1",
    "draft_generator": "v1.0",
    "draft_chat": "v1.0",
    "memory_learner": "v1.0",
    "memory_compiler": "v1.0",
}


class UnsupportedToolCallError(RuntimeError):
    """真实模型返回了绑定工具列表之外的函数名（幻觉/近似名）。

    由 AgentLoop 捕获并引导模型重规划，而非直接终止运行。
    """


class LLMWrapper:
    """Run schema-validated calls and persist their operational metadata."""

    def __init__(self, llm: BaseChatModel, db: Any = None):
        self.llm = llm
        self.db = db

    async def invoke_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        output_schema: type[T],
        agent_type: str,
        user_id: str = "",
        trace_id: str = "",
        task_id: str = "",
        context_meta: dict | None = None,
    ) -> T:
        """Prefer native structured output and transparently record fallback.

        context_meta: 阶段二上下文 telemetry（context_plan_hash/skill_versions/
            knowledge_snapshot/source_ids），随 LLM 调用日志持久化，不含知识全文。
        """
        started = time.perf_counter()
        degraded = False
        degrade_reason = ""
        retry_count = 0
        structured_output = True
        raw_response: Any = None
        result: T | None = None

        try:
            structured_llm = self.llm.with_structured_output(output_schema, method="json_mode")
            raw_result = await structured_llm.ainvoke(self._messages(system_prompt, user_prompt))
            result = self._validate_result(raw_result, output_schema)
        except Exception as structured_exc:
            degraded = True
            structured_output = False
            retry_count = 1
            degrade_reason = f"structured output failed: {structured_exc}"[:200]
            try:
                result, raw_response = await self._fallback_invoke(
                    system_prompt,
                    user_prompt,
                    output_schema,
                )
            except Exception as fallback_exc:
                degrade_reason = (f"{degrade_reason}; fallback failed: {fallback_exc}")[:200]
                await self._log_call(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    result=None,
                    raw_response=raw_response,
                    agent_type=agent_type,
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    degraded=True,
                    degrade_reason=degrade_reason,
                    retry_count=retry_count,
                    structured_output=False,
                    schema_name=output_schema.__name__,
                    context_meta=context_meta,
                )
                raise

        await self._log_call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            result=result,
            raw_response=raw_response,
            agent_type=agent_type,
            user_id=user_id,
            trace_id=trace_id,
            task_id=task_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            degraded=degraded,
            degrade_reason=degrade_reason,
            retry_count=retry_count,
            structured_output=structured_output,
            schema_name=output_schema.__name__,
            context_meta=context_meta,
        )
        return result

    async def invoke_agent_step(
        self,
        *,
        bound_llm: Any,
        messages: list[Any],
        run_context: RunContext,
        loop_round: int = 0,
        retry_policy: RetryPolicy | None = None,
        max_attempts: int | None = None,
        retry_state_out: RetryState | None = None,
    ) -> AIMessage:
        """执行一次 Agent Loop 决策轮 LLM 调用（非流式 ainvoke）。

        基于 Step 1 探针结论：非流式 ainvoke 的 tool_calls 稳定可靠。

        Args:
            bound_llm: 已 bind_tools 的 LLM 实例
            messages: 消息列表（含 system + 历史 + 工具观察）
            run_context: 运行上下文（身份/追踪/预算）
            loop_round: 当前轮次（0-based，日志用）
            retry_policy: 重试策略（None 时用默认）
            max_attempts: 最大尝试次数上限（含首次），来自预算 max_retries+1；
                retry_policy 提供时忽略
            retry_state_out: 可选引用传递的重试状态，调用方用于读取实际重试次数
                （接入 BudgetManager.record_retry，使重试预算生效）

        Returns:
            AIMessage：含 content 和 tool_calls

        Raises:
            最后一个异常（如果所有重试都失败）
        """
        from datetime import datetime

        started = time.perf_counter()
        # 直接复用调用方对象：重试计数（含失败路径）实时写入 retry_state_out
        retry_state = retry_state_out if retry_state_out is not None else RetryState()

        # 默认重试策略：2 次重试，共享 run_context 的 deadline
        deadline_at = None
        if run_context.deadline_at is not None:
            remaining = (run_context.deadline_at - datetime.now(UTC)).total_seconds()
            deadline_at = time.monotonic() + max(0, remaining)

        policy = retry_policy or RetryPolicy(
            max_attempts=max(1, int(max_attempts or 3)),
            base_delay=1.0,
            multiplier=2.0,
            max_delay=8.0,
            deadline_at=deadline_at,
        )

        tool_names: list[str] = []
        response: AIMessage | None = None

        try:
            response = await with_retry(
                lambda: bound_llm.ainvoke(messages),
                policy=policy,
                retry_state=retry_state,
                trace_id=run_context.trace_id,
            )
        except Exception as exc:
            # 日志仍写入（含 retry 信息），然后重新抛出
            await self._log_agent_call(
                run_context=run_context,
                loop_round=loop_round,
                messages=messages,
                response=None,
                duration_ms=int((time.perf_counter() - started) * 1000),
                retry_state=retry_state,
                error="LLM call failed",
            )
            if isinstance(exc, ValueError) and "Unsupported function" in str(exc):
                # 真实模型返回绑定工具列表之外的函数名（幻觉/近似名）：
                # 转成结构化异常，由 AgentLoop 引导重规划而非终止运行
                raise UnsupportedToolCallError(str(exc)) from exc
            raise

        # 提取 tool names 用于日志
        if isinstance(response, AIMessage):
            tool_calls = getattr(response, "tool_calls", []) or []
            tool_names = [tc.get("name", "") for tc in tool_calls if isinstance(tc, dict)]

        await self._log_agent_call(
            run_context=run_context,
            loop_round=loop_round,
            messages=messages,
            response=response,
            duration_ms=int((time.perf_counter() - started) * 1000),
            retry_state=retry_state,
            tool_names=tool_names,
        )

        return response

    async def _log_agent_call(
        self,
        *,
        run_context: RunContext,
        loop_round: int,
        messages: list[Any],
        response: AIMessage | None,
        duration_ms: int,
        retry_state: RetryState,
        tool_names: list[str] | None = None,
        error: str = "",
    ) -> None:
        """记录 Agent 步级 LLM 调用日志到 llm_call_logs。

        安全约束：
          - 不写原始 prompt、完整工具内容或私有推理
          - 只写 hash、token 计数、工具名、重试信息
        """
        if self.db is None:
            return

        # 计算 token usage（优先用 provider 返回的 usage_metadata；缺失时保守估算）
        input_tokens, output_tokens, cached_input_tokens, usage_estimated = self._resolve_usage(
            messages, response
        )
        model_name = self._model_name()
        cost = compute_cost(
            model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            input_tokens_estimated=usage_estimated,
            output_tokens_estimated=usage_estimated,
        )
        now = datetime.now(UTC)

        # 计算 messages 的 hash（不含完整内容）
        messages_hash = self._messages_hash(messages)

        document = {
            "call_id": f"llm-{uuid4().hex[:12]}",
            "model_name": model_name,
            "prompt_version": PROMPT_VERSIONS.get("draft_chat", "v1.0"),
            "system_prompt_hash": messages_hash,
            "user_prompt_hash": "",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost["cost_usd"],
            "currency": cost["currency"],
            "pricing_version": cost["pricing_version"],
            "pricing_source": cost["pricing_source"],
            "pricing_estimated": cost["pricing_estimated"],
            "usage_estimated": cost["usage_estimated"],
            "created_at": now,
            "expires_at": now + timedelta(days=30),
            "agent_type": "draft_chat",
            "user_id": run_context.user_id,
            "trace_id": run_context.trace_id,
            "task_id": "",
            "run_id": run_context.run_id,
            "loop_round": loop_round,
            "tool_names": tool_names or [],
            "retry": list(retry_state.attempts),
            "degraded": bool(error),
            "degrade_reason": error[:200] if error else "",
            "duration_ms": duration_ms,
            "structured_output": False,
            "schema_name": "agent_step",
        }

        try:
            await self.db["llm_call_logs"].insert_one(document)
        except Exception:
            logger.exception("Failed to persist agent LLM call metadata")

    @staticmethod
    def _messages_hash(messages: list[Any]) -> str:
        """计算消息列表的联合 hash（不含完整内容）。"""
        parts: list[str] = []
        for msg in messages:
            msg_type = type(msg).__name__
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                parts.append(f"{msg_type}:{len(content)}")
            else:
                parts.append(f"{msg_type}:{type(content).__name__}")
            # 工具调用也只记 hash
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        parts.append(f"tc:{tc.get('name', '')}:{tc.get('id', '')}")
        combined = "|".join(parts)
        return f"sha256:{hashlib.sha256(combined.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _resolve_usage(
        messages: list[Any],
        response: AIMessage | None,
    ) -> tuple[int, int, int, bool]:
        """从 LLM 响应中提取 token 用量。

        返回 (input_tokens, output_tokens, cached_input_tokens, estimated)。
        - 优先 provider 返回值（usage_metadata / response_metadata.token_usage），
          并读取缓存命中输入 token（input_token_details.cache_read 或
          prompt_cache_hit_tokens）；estimated=False。
        - 无数据时按字符数 /4 保守估算；estimated=True。

        Step 1 探针确认：DeepSeek 返回 usage_metadata 可直接读取。
        """
        if response is not None:
            usage = getattr(response, "usage_metadata", None) or {}
            if not usage:
                metadata = getattr(response, "response_metadata", None) or {}
                usage = metadata.get("token_usage", {})
            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
            output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
            if input_tokens is not None and output_tokens is not None:
                cached = 0
                details = usage.get("input_token_details") or {}
                if isinstance(details, dict):
                    cached = details.get("cache_read", 0)
                if not cached:
                    cached = usage.get("prompt_cache_hit_tokens", 0)
                return int(input_tokens), int(output_tokens), int(cached or 0), False

        # 估算：按字符数 / 4
        total_chars = 0
        for msg in messages:
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                total_chars += len(content)
        input_tokens = max(1, total_chars // 4)
        output_tokens = 0
        if response is not None:
            content = getattr(response, "content", "")
            if isinstance(content, str):
                output_tokens = max(1, len(content) // 4) if content else 0
        return input_tokens, output_tokens, 0, True

    @classmethod
    def _agent_token_usage(
        cls,
        messages: list[Any],
        response: AIMessage | None,
    ) -> tuple[int, int]:
        """兼容接口：返回 (input_tokens, output_tokens)。"""
        input_tokens, output_tokens, _, _ = cls._resolve_usage(messages, response)
        return input_tokens, output_tokens

    @staticmethod
    def _messages(system_prompt: str, user_prompt: str) -> list[Any]:
        return [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

    @staticmethod
    def _validate_result(result: Any, schema: type[T]) -> T:
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)

    async def _fallback_invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> tuple[T, Any]:
        """Fallback to a normal call, then validate extracted JSON with Pydantic."""
        response = await self.llm.ainvoke(self._messages(system_prompt, user_prompt))
        raw = response.content if hasattr(response, "content") else str(response)
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        text = code_block.group(1).strip() if code_block else raw.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            first_brace = text.find("{")
            last_brace = text.rfind("}")
            if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
                raise ValueError(f"Cannot extract JSON from response: {text[:200]}") from None
            data = json.loads(text[first_brace : last_brace + 1])
        return schema.model_validate(data), response

    async def _log_call(self, **kwargs: Any) -> None:
        if self.db is None:
            return

        system_prompt = kwargs.pop("system_prompt", "")
        user_prompt = kwargs.pop("user_prompt", "")
        result = kwargs.pop("result", None)
        raw_response = kwargs.pop("raw_response", None)
        model_name = self._model_name()
        input_tokens, output_tokens, usage_estimated = self._token_usage(
            raw_response,
            system_prompt,
            user_prompt,
            result,
        )
        cost = compute_cost(
            model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_estimated=usage_estimated,
            output_tokens_estimated=usage_estimated,
        )
        now = datetime.now(UTC)
        document = {
            "call_id": f"llm-{uuid4().hex[:12]}",
            "model_name": model_name,
            "prompt_version": PROMPT_VERSIONS.get(kwargs.get("agent_type", ""), "unknown"),
            "system_prompt_hash": self._prompt_hash(system_prompt),
            "user_prompt_hash": self._prompt_hash(user_prompt),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost["cost_usd"],
            "currency": cost["currency"],
            "pricing_version": cost["pricing_version"],
            "pricing_source": cost["pricing_source"],
            "pricing_estimated": cost["pricing_estimated"],
            "usage_estimated": cost["usage_estimated"],
            "created_at": now,
            "expires_at": now + timedelta(days=30),
            **kwargs,
        }
        try:
            await self.db["llm_call_logs"].insert_one(document)
        except Exception:
            logger.exception("Failed to persist LLM call metadata")

    def _model_name(self) -> str:
        return str(
            getattr(self.llm, "model_name", None) or getattr(self.llm, "model", None) or "unknown"
        )

    @staticmethod
    def _prompt_hash(prompt: str) -> str:
        return f"sha256:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _token_usage(
        response: Any,
        system_prompt: str,
        user_prompt: str,
        result: BaseModel | None,
    ) -> tuple[int, int, bool]:
        usage = getattr(response, "usage_metadata", None) or {}
        if not usage and response is not None:
            metadata = getattr(response, "response_metadata", None) or {}
            usage = metadata.get("token_usage", {})
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        estimated = False
        if input_tokens is None:
            input_tokens = max(1, (len(system_prompt) + len(user_prompt)) // 4)
            estimated = True
        if output_tokens is None:
            rendered = result.model_dump_json() if result is not None else ""
            output_tokens = max(1, len(rendered) // 4) if rendered else 0
            estimated = True
        return int(input_tokens), int(output_tokens), estimated
