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

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

logger = logging.getLogger("backend.agent.llm_wrapper")

T = TypeVar("T", bound=BaseModel)

PROMPT_VERSIONS = {
    "classifier_v2": "v2.1",
    "scorer_v2": "v2.1",
    "draft_generator": "v1.0",
    "draft_chat": "v1.0",
}

# USD per 1,000 tokens. Values are deliberately configurable in one place and
# are estimates only; provider billing remains the source of truth.
PRICING = {
    "deepseek-chat": {"input": 0.001, "output": 0.002},
}


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
    ) -> T:
        """Prefer native structured output and transparently record fallback."""
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
        )
        return result

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
            obj_match = re.search(r"\{[^{}]*\}", text)
            if obj_match is None:
                raise ValueError(f"Cannot extract JSON from response: {text[:200]}") from None
            data = json.loads(obj_match.group(0))
        return schema.model_validate(data), response

    async def _log_call(self, **kwargs: Any) -> None:
        if self.db is None:
            return

        system_prompt = kwargs.pop("system_prompt", "")
        user_prompt = kwargs.pop("user_prompt", "")
        result = kwargs.pop("result", None)
        raw_response = kwargs.pop("raw_response", None)
        model_name = self._model_name()
        input_tokens, output_tokens = self._token_usage(
            raw_response,
            system_prompt,
            user_prompt,
            result,
        )
        pricing = PRICING.get(model_name, {"input": 0.001, "output": 0.002})
        cost_usd = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1000
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
            "cost_usd": round(cost_usd, 8),
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
    ) -> tuple[int, int]:
        usage = getattr(response, "usage_metadata", None) or {}
        if not usage and response is not None:
            metadata = getattr(response, "response_metadata", None) or {}
            usage = metadata.get("token_usage", {})
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if input_tokens is None:
            input_tokens = max(1, (len(system_prompt) + len(user_prompt)) // 4)
        if output_tokens is None:
            rendered = result.model_dump_json() if result is not None else ""
            output_tokens = max(1, len(rendered) // 4) if rendered else 0
        return int(input_tokens), int(output_tokens)
