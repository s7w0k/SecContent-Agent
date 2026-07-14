"""Task 9.6: structured LLM output and observability tests."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../services/backend"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from agent.llm_wrapper import LLMWrapper  # noqa: E402
from agent.schemas import ClassifyResultSchema, ScoreResultSchema  # noqa: E402
from api.pipeline import list_llm_logs  # noqa: E402


class FakeCollection:
    def __init__(self, documents: list[dict] | None = None):
        self.documents = documents or []
        self.insert_one = AsyncMock()
        self.last_query: dict | None = None

    async def count_documents(self, query: dict) -> int:
        self.last_query = query
        return len(self.documents)

    def find(self, query: dict) -> FakeCollection:
        self.last_query = query
        return self

    def sort(self, *_args) -> FakeCollection:
        return self

    def skip(self, *_args) -> FakeCollection:
        return self

    def limit(self, *_args) -> FakeCollection:
        return self

    async def to_list(self, *, length: int) -> list[dict]:
        return self.documents[:length]


class StructuredRunnable:
    def __init__(self, result):
        self.result = result

    async def ainvoke(self, _messages):
        return self.result


class NativeStructuredLLM:
    model_name = "deepseek-chat"

    def __init__(self, result):
        self.result = result
        self.ainvoke = AsyncMock()

    def with_structured_output(self, _schema):
        return StructuredRunnable(self.result)


class FallbackLLM:
    model_name = "deepseek-chat"

    def __init__(self, content: str):
        self.ainvoke = AsyncMock(
            return_value=AIMessage(
                content=content,
                usage_metadata={"input_tokens": 25, "output_tokens": 10, "total_tokens": 35},
            )
        )

    def with_structured_output(self, _schema):
        raise NotImplementedError("provider does not support tool calling")


def test_schemas_normalize_and_validate_model_output() -> None:
    classified = ClassifyResultSchema.model_validate(
        {"category": "自创类别", "confidence": 120, "reason": "原因"}
    )
    scored = ScoreResultSchema.model_validate(
        {
            "product_relevance": -10,
            "event_impact": 140,
            "reason": "评分理由",
            "tags": ["a", "b", "c", "d", "e", "f"],
        }
    )

    assert classified.category == "不相关"
    assert classified.confidence == 100
    assert scored.product_relevance == 0
    assert scored.event_impact == 100
    assert scored.tags == ["a", "b", "c", "d", "e"]


@pytest.mark.asyncio
async def test_native_structured_output_persists_complete_metadata() -> None:
    collection = FakeCollection()
    db = {"llm_call_logs": collection}
    result = ClassifyResultSchema(
        category="爆点事件",
        confidence=95,
        reason="重大漏洞",
        is_pr_eligible=True,
    )
    wrapper = LLMWrapper(NativeStructuredLLM(result), db)

    actual = await wrapper.invoke_structured(
        "system prompt",
        "user prompt",
        ClassifyResultSchema,
        "classifier_v2",
        user_id="user-a",
        trace_id="trace-a",
        task_id="task-a",
    )

    assert actual == result
    document = collection.insert_one.await_args.args[0]
    assert document["user_id"] == "user-a"
    assert document["trace_id"] == "trace-a"
    assert document["task_id"] == "task-a"
    assert document["structured_output"] is True
    assert document["degraded"] is False
    assert document["schema_name"] == "ClassifyResultSchema"
    assert document["system_prompt_hash"].startswith("sha256:")
    assert "system prompt" not in document.values()
    assert document["total_tokens"] == document["input_tokens"] + document["output_tokens"]


@pytest.mark.asyncio
async def test_provider_incompatibility_falls_back_and_records_reason() -> None:
    collection = FakeCollection()
    wrapper = LLMWrapper(
        FallbackLLM(
            '{"product_relevance": 88, "event_impact": 77, "reason": "高度相关", "tags": ["MCP"]}'
        ),
        {"llm_call_logs": collection},
    )

    result = await wrapper.invoke_structured(
        "system",
        "user",
        ScoreResultSchema,
        "scorer_v2",
    )

    assert result.product_relevance == 88
    document = collection.insert_one.await_args.args[0]
    assert document["degraded"] is True
    assert document["structured_output"] is False
    assert "provider does not support" in document["degrade_reason"]
    assert document["retry_count"] == 1
    assert document["input_tokens"] == 25
    assert document["output_tokens"] == 10


@pytest.mark.asyncio
async def test_llm_log_api_enforces_current_user_filter() -> None:
    collection = FakeCollection(
        [
            {
                "_id": "mongo-id",
                "call_id": "llm-1",
                "user_id": "user-a",
                "agent_type": "scorer_v2",
                "created_at": datetime(2026, 7, 14, tzinfo=UTC),
            }
        ]
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db={"llm_call_logs": collection}))
    )

    response = await list_llm_logs(
        request,
        page=1,
        page_size=20,
        agent_type="scorer_v2",
        task_id="task-a",
        user_id="user-a",
    )

    assert collection.last_query == {
        "user_id": "user-a",
        "agent_type": "scorer_v2",
        "task_id": "task-a",
    }
    assert response["data"]["items"][0]["created_at"] == "2026-07-14T00:00:00+00:00"
    assert "_id" not in response["data"]["items"][0]
