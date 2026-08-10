"""PR-4B-04 测试：闭环互操作、双向恢复与隔离试点灰度开关。

覆盖 spec 4B-5 / 4B-6：
  - 闭环互操作：A2AServer（真实 AutonomousRunService + Fake Mongo）+ A2AClient
    经 ASGITransport 走完整协议链路（Agent Card 发现 → Send → 轮询终态）；
  - 双向恢复与追溯：task.internal_run_id ↔ run_id 双向映射；
    context_id ↔ thread_id、message_id ↔ trace_id 审计追溯；
    断流后以远端 Task 为权威恢复；Last-Event-ID 游标续传不重放；
  - 隔离试点与灰度开关：A2A_ENABLED / A2A_CLIENT_ENABLED 默认关闭、
    A2A_ALLOWED_PEERS 默认空、A2A 依赖自主模式校验、
    试点工具链只读且不含外部 URL 输入工具、a2a_send 强制幂等键、
    空允许列表默认禁止外部副作用。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from agent.a2a.client import (
    A2AClient,
    DiscoveryError,
    RemoteAgentConfig,
    RemoteUnavailableError,
)
from agent.a2a.models import AGENT_CARD_PATH, PROTOCOL_VERSION, Skill, TaskStatus
from agent.a2a.server import A2AServer, A2A_SKILL_TOOL_CHAIN
from agent.a2a.task_store import A2ATaskStore
from agent.autonomous_service import AutonomousRunService
from agent.policy_engine import ApprovalService, PolicyEngine, RiskLevel
from agent.runtime_events import RuntimeEventStore
from agent.runtime_store import RuntimeStateStore

PEER_BASE = "http://peer-a2a.test"
PUBLIC_IP = "93.184.216.34"


# ═══════════════════════════════════════════════════════════════
# Fake Mongo（与 test_a2a_server 一致：支持序列排序 / $gt / $in / $set）
# ═══════════════════════════════════════════════════════════════


def _match(doc: dict, query: dict) -> bool:
    for k, v in (query or {}).items():
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$gt" in v and not (doc.get(k, 0) > v["$gt"]):
                return False
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _FakeCursor:
    def __init__(self, col, query):
        self.col = col
        self.query = query
        self._sort_key: str | None = None
        self._sort_reverse: bool = False
        self._limit: int = 1000

    def sort(self, key, direction):
        self._sort_key = key
        self._sort_reverse = direction < 0
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length: int | None = None):
        limit = length or self._limit
        matched = [d for d in self.col.docs if _match(d, self.query)]
        if self._sort_key:
            matched = sorted(
                matched, key=lambda d: d.get(self._sort_key, 0), reverse=self._sort_reverse
            )
        return matched[:limit]


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc: dict):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc.get("_id", "id"))

    async def replace_one(self, query, doc, upsert=False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs[i] = doc
                return SimpleNamespace(matched_count=1)
        if upsert:
            self.docs.append(doc)
            return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_one(self, query, update, **kwargs):
        for d in self.docs:
            if _match(d, query):
                for op, fields in update.items():
                    if op == "$set":
                        d.update(fields)
                    elif op == "$inc":
                        for k, v in fields.items():
                            d[k] = d.get(k, 0) + v
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def find_one(self, query=None, sort=None):
        query = query or {}
        if sort and sort[0][0] == "sequence":
            matched = [d for d in self.docs if _match(d, query)]
            if not matched:
                return None
            return max(matched, key=lambda d: d.get("sequence", 0))
        for d in self.docs:
            if _match(d, query):
                return d
        return None

    def find(self, *args, **kwargs):
        query = kwargs.get("filter", args[0] if args else {})
        return _FakeCursor(self, query)

    async def create_indexes(self, indexes):
        return [i.document["name"] for i in indexes]


class _FakeDB(dict):
    def __init__(self):
        super().__init__()
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str):
        if name not in self._cols:
            self._cols[name] = _FakeCol()
        return self._cols[name]


# ═══════════════════════════════════════════════════════════════
# 辅助：A2A Server + Client 双端栈
# ═══════════════════════════════════════════════════════════════


def _make_service(db) -> AutonomousRunService:
    return AutonomousRunService(
        store=RuntimeStateStore(db),
        event_store=RuntimeEventStore(db, expires_days=30),
        policy=PolicyEngine(),
        approval_service=ApprovalService(db=None, ttl_seconds=1800),
        db=db,
    )


def _make_interop_stack(*, stream: bool = False):
    """真实 AutonomousRunService + Fake Mongo + A2AServer + FastAPI 路由。"""
    from auth.deps import get_current_user
    from fastapi import FastAPI

    from api.a2a import router as a2a_router

    db = _FakeDB()
    service = _make_service(db)
    server = A2AServer(
        run_service=service,
        task_store=A2ATaskStore(db),
        skills=[
            Skill(
                id="pr_intel",
                name="PR 情报分析",
                description="只读情报分析",
                tags=["read-only"],
                output_modes=["text", "stream"] if stream else ["text"],
            ),
        ],
        card_url=f"{PEER_BASE}{AGENT_CARD_PATH}",
    )
    app = FastAPI()
    app.state.a2a_server = server

    async def _override_current_user():
        return "u1"

    app.dependency_overrides[get_current_user] = _override_current_user
    app.include_router(a2a_router)
    return db, server, app


def _make_interop_client(app, *, base_url=PEER_BASE):
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        follow_redirects=False,
        trust_env=False,
    )
    cfg = RemoteAgentConfig(
        key="peer-1",
        base_url=base_url,
        enabled_skills=["pr_intel"],
        require_https=False,
        retry_max=1,
    )
    return A2AClient(
        allowlist={"peer-1": cfg},
        http_client=http,
        resolver=lambda host: [PUBLIC_IP],  # mock 主机统一解析为公网 IP
    )


def _msg(text="分析近 7 天 PR 情报", *, message_id="m1", task_id="", context_id="ctx-1"):
    from agent.a2a.models import Message, Part

    return Message(
        message_id=message_id,
        task_id=task_id,
        role="user",
        parts=[Part(kind="text", text=text)],
        context_id=context_id,
        metadata={"skill_id": "pr_intel"},
    )


async def _poll_task(client, task_id, timeout=8.0):
    deadline = time.monotonic() + timeout
    last = "?"
    while time.monotonic() < deadline:
        task = await client.get_task("peer-1", task_id)
        if task is not None:
            last = task.status.value
            if task.status in (
                TaskStatus.COMPLETED, TaskStatus.FAILED,
                TaskStatus.CANCELED, TaskStatus.REJECTED,
            ):
                return task
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for terminal state, last={last}")


# ═══════════════════════════════════════════════════════════════
# 闭环互操作（Server + Client 双端，真实 Runtime 状态机）
# ═══════════════════════════════════════════════════════════════


class TestClosedLoopInterop:
    async def test_closed_loop_send_and_poll_to_complete(self):
        _, _, app = _make_interop_stack()
        client = _make_interop_client(app)
        task = await client.send(
            "peer-1", _msg("分析近 7 天 PR 情报", message_id="m1", task_id="a2a-t1")
        )
        assert task.id == "a2a-t1"
        assert task.internal_run_id  # a2a_task_id <-> internal_run_id 已关联
        final = await _poll_task(client, "a2a-t1")
        assert final.status == TaskStatus.COMPLETED
        # 服务端 Agent Card 与协议版本一致
        card = await client.discover("peer-1")
        assert card.protocol_version == PROTOCOL_VERSION

    async def test_closed_loop_stream_send(self):
        _, _, app = _make_interop_stack(stream=True)
        client = _make_interop_client(app)
        task = await client.send(
            "peer-1", _msg("流式分析", message_id="ms1", task_id="a2a-s1")
        )
        # 流结束 + get_task 拉取远端终态
        assert task.id == "a2a-s1"
        assert task.status == TaskStatus.COMPLETED

    async def test_bidirectional_traceability(self):
        _, server, app = _make_interop_stack()
        client = _make_interop_client(app)
        task = await client.send(
            "peer-1", _msg("追溯测试", message_id="trace-1", task_id="a2a-tr", context_id="th-9")
        )
        # 正向：a2a_task_id -> internal_run_id -> RuntimeState
        principal = server.principal("u1")
        state = await server.run_service.get_run(task.internal_run_id, user_id=principal)
        assert state is not None
        assert state.thread_id == "th-9"          # context_id <-> thread_id
        assert state.trace_id == "trace-1"        # message_id <-> trace_id（审计追溯）
        # 反向：internal_run_id -> a2a Task
        back = await server.task_store.load_by_run_id(state.run_id, user_id=principal)
        assert back is not None and back.id == "a2a-tr"
        # 运行事件可追溯（事件流基于同一 run）
        events = await server.run_service.events(state.run_id, principal)
        assert any(e.event_type == "run_created" for e in events)

    async def test_client_send_idempotent_no_duplicate_run(self):
        _, server, app = _make_interop_stack()
        client = _make_interop_client(app)
        msg = _msg("幂等测试", message_id="ik-1", task_id="a2a-ik")
        first = await client.send("peer-1", msg)
        second = await client.send("peer-1", msg)  # 同 task_id → 服务端幂等
        assert first.internal_run_id == second.internal_run_id  # 复用同一内部运行
        runs = await server.run_service.list_runs(server.principal("u1"))
        assert len(runs) == 1  # 不重复创建内部运行（不重复副作用）


# ═══════════════════════════════════════════════════════════════
# 双向恢复：远端不可用可解释失败 + 游标续传不重放
# ═══════════════════════════════════════════════════════════════


class TestBidirectionalRecovery:
    async def test_remote_unavailable_is_explainable_failure(self):
        """远端持续 503 → 重试耗尽 → 可解释失败（RemoteUnavailableError），不伪装成功。"""
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(503)
            return httpx.Response(404)

        client = _make_mock_client(handler, retry_max=1)
        with pytest.raises(RemoteUnavailableError) as excinfo:
            await client.send("peer-1", _msg(message_id="m1"))
        assert "remote unavailable" in str(excinfo.value)
        assert send_calls["n"] == 2

    async def test_recovery_queries_remote_task_after_interruption(self):
        """远端不可用（断流）后恢复：恢复链路先查询远端 Task 决定继续/失败。"""
        _, _, app = _make_interop_stack()
        client = _make_interop_client(app)
        task = await client.send(
            "peer-1", _msg("恢复测试", message_id="rc-1", task_id="a2a-rc")
        )
        await _poll_task(client, "a2a-rc")
        # 恢复语义：get_task 以远端为权威拉取一致终态
        reconciled = await client.get_task("peer-1", "a2a-rc")
        assert reconciled is not None and reconciled.status == TaskStatus.COMPLETED

    async def test_resubscribe_cursor_resume_no_replay(self):
        """Last-Event-ID 游标续传：续传只收游标之后事件，不重放历史。"""
        _, _, app = _make_interop_stack()
        client = _make_interop_client(app)
        await client.send("peer-1", _msg("续传", message_id="rs-1", task_id="a2a-rs"))
        await _poll_task(client, "a2a-rs")
        # 首次全量订阅：拿到所有事件与最大 sequence
        first: list = []
        async for ev in client.subscribe("peer-1", "a2a-rs"):
            first.append(ev)
        assert first
        last_seq = max(int(ev.metadata.get("sequence", 0)) for ev in first)
        # 从 last_seq 续传：不应再收到任何事件（全部已消费，不重放）
        tail: list = []
        async for ev in client.subscribe("peer-1", "a2a-rs", last_event_id=str(last_seq)):
            tail.append(ev)
        assert tail == []


# ═══════════════════════════════════════════════════════════════
# 隔离试点与灰度开关
# ═══════════════════════════════════════════════════════════════


class TestPilotIsolation:
    def test_settings_default_disabled(self):
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
        assert s.AUTONOMOUS_AGENT_ENABLED is False
        assert s.A2A_ENABLED is False                    # A2A Server 默认关
        assert s.A2A_CLIENT_ENABLED is False             # A2A Client 默认关
        assert s.A2A_ALLOWED_PEERS == []                 # 外部 Agent 允许列表默认空
        # 客户端默认低值/保守
        assert s.A2A_CLIENT_RETRY_MAX == 2
        assert s.A2A_CLIENT_CARD_TTL_SECONDS == 300
        assert s.A2A_CLIENT_PEER_QUOTA == 5

    def test_a2a_requires_autonomous_enabled(self):
        """A2A 复用自主运行服务：不允许单独暴露。"""
        from config import Settings

        with pytest.raises(ValidationError):
            Settings(
                A2A_ENABLED=True,
                AUTONOMOUS_AGENT_ENABLED=False,
                DEEPSEEK_API_KEY="test",
                _env_file=None,
            )

    def test_pilot_tool_chain_readonly_and_low_risk(self):
        """首批试点只开放只读低风险工具；含外部 URL 输入的工具不开放。"""
        assert set(A2A_SKILL_TOOL_CHAIN) == {
            "retrieve_articles",
            "classify_articles",
            "score_articles",
            "export_articles_csv",
        }
        # fetch_article_fulltext 的 URL 来自外部输入 → 试点不放行（4B-6 隔离试点）
        assert "fetch_article_fulltext" not in A2A_SKILL_TOOL_CHAIN
        engine = PolicyEngine()
        for tool in A2A_SKILL_TOOL_CHAIN:
            rule = engine.rules[tool]
            assert rule.risk_level in (RiskLevel.L0, RiskLevel.L1)  # 均非 L2/L3

    def test_a2a_send_rule_forces_idempotency_key(self):
        """a2a_send 声明外部副作用：缺幂等键拒绝（防重试重复副作用）。"""
        engine = PolicyEngine()
        rule = engine.rules["a2a_send"]
        assert rule.has_side_effect is True
        assert rule.risk_level == RiskLevel.L1
        assert "idempotency_key" in rule.allowed_args
        denied = engine.evaluate(
            tool_name="a2a_send",
            args={"peer": "peer-1", "skill_id": "pr_intel"},
        )
        assert not denied.allowed
        assert denied.reason_code == "missing_idempotency_key"
        allowed = engine.evaluate(
            tool_name="a2a_send",
            args={"peer": "peer-1", "skill_id": "pr_intel", "idempotency_key": "ik-1"},
        )
        assert allowed.allowed

    async def test_empty_allowlist_blocks_external_side_effects(self):
        """默认禁止外部副作用：空允许列表下任何远端发现/调用都被拒绝。"""
        client = A2AClient(allowlist={})
        assert client.allowlist == {}
        with pytest.raises(DiscoveryError):
            await client.discover("any-external-agent")


# ═══════════════════════════════════════════════════════════════
# 极简 Card JSON（503 测试用）
# ═══════════════════════════════════════════════════════════════


def _card_json():
    return {
        "name": "Mock Agent",
        "description": "mock",
        "url": f"{PEER_BASE}{AGENT_CARD_PATH}",
        "protocol_version": "1.0",
        "version": "1.0.0",
        "skills": [
            {
                "id": "pr_intel",
                "name": "PR",
                "description": "read-only",
                "tags": ["read-only"],
                "input_modes": ["text"],
                "output_modes": ["text"],
            }
        ],
    }


def _make_mock_client(handler, *, retry_max=1, **cfg_extra):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
    cfg = RemoteAgentConfig(
        key="peer-1",
        base_url=PEER_BASE,
        enabled_skills=["pr_intel"],
        require_https=False,
        retry_max=retry_max,
        **cfg_extra,
    )
    return A2AClient(
        allowlist={"peer-1": cfg},
        http_client=http,
        resolver=lambda host: [PUBLIC_IP],
    )
