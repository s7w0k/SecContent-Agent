"""chat 线程用户隔离测试（替代已移除 api.chat 时代的 chat_sessions 隔离用例）。

当前对话实现为 agent-engine 的 chat_threads：按 user_id 隔离，
create/list/get 全部要求所有权（与 removed chat_sessions 语义一致，落点更新）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from agent.business_tools.contracts import build_business_tool_registry
from agent.chat_agent_service import ChatAgentService, ChatThread


class _Cursor:
    def __init__(self, docs: list[dict]):
        self.docs = list(docs)

    def sort(self, _field: str, _direction: int):
        self.docs.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
        return self

    def limit(self, count: int):
        self.docs = self.docs[:count]
        return self

    async def to_list(self, length: int):
        return self.docs[:length]


class _ThreadCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, document: dict):
        self.docs.append(document)
        return MagicMock(inserted_id=document["thread_id"])

    async def replace_one(self, query: dict, replacement: dict, upsert: bool = False):
        tid = query.get("thread_id")
        for i, d in enumerate(self.docs):
            if d.get("thread_id") == tid:
                self.docs[i] = replacement
                return MagicMock(matched_count=1, upserted_id=None)
        if upsert:
            self.docs.append(replacement)
            return MagicMock(matched_count=0, upserted_id=tid)
        return MagicMock(matched_count=0, upserted_id=None)

    async def find_one(self, query: dict):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    def find(self, query: dict):
        return _Cursor([d for d in self.docs if d.get("user_id") == query.get("user_id")])


def _service(db) -> ChatAgentService:
    return ChatAgentService(
        llm_wrapper=MagicMock(),
        executor=MagicMock(),
        registry=build_business_tool_registry(),
        adapter="fake",
        tenant_id_default="tenant-default",
        db=db,
    )


def _thread(thread_id: str, user_id: str, tenant_id: str = "tenant-a") -> dict:
    return ChatThread(thread_id=thread_id, user_id=user_id, tenant_id=tenant_id).model_dump()


@pytest.mark.asyncio
async def test_create_thread_persists_with_owner_and_is_listed_for_owner_only():
    collection = _ThreadCollection()
    db = {"chat_threads": collection}
    service = _service(db)

    a = await service.create_thread("user-a", "tenant-a")
    await service.create_thread("user-b", "tenant-b")

    # 持久化文档归属正确
    stored = collection.docs
    assert {d["user_id"] for d in stored} == {"user-a", "user-b"}
    assert a.user_id == "user-a"

    # 用户只能列出自己的线程
    list_a = await service.list_threads("user-a")
    assert {t.user_id for t in list_a} == {"user-a"}
    list_b = await service.list_threads("user-b")
    assert {t.user_id for t in list_b} == {"user-b"}


@pytest.mark.asyncio
async def test_get_thread_enforces_ownership():
    collection = _ThreadCollection()
    db = {"chat_threads": collection}
    service = _service(db)

    await service.create_thread("user-a", "tenant-a")
    thread_id = collection.docs[0]["thread_id"]

    own = await service.get_thread(thread_id, "user-a")
    assert own is not None and own.user_id == "user-a"
    other = await service.get_thread(thread_id, "user-b")
    assert other is None  # 跨用户访问被拒


@pytest.mark.asyncio
async def test_list_threads_filters_by_user_on_db_query():
    collection = _ThreadCollection()
    collection.docs = [
        _thread("t1", "user-a"),
        _thread("t2", "user-b"),
        _thread("t3", "user-a"),
    ]
    db = {"chat_threads": collection}
    service = _service(db)
    listed = await service.list_threads("user-a", limit=50)
    assert sorted(t.thread_id for t in listed) == ["t1", "t3"]
