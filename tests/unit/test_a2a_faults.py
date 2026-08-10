"""PR-4B-04 测试：故障注入（断流 / 超时 / 重复投递 / 乱序幂等 / 熔断恢复）。

覆盖 spec 4B-5（一致性 / 故障恢复）与 4B-6（断流、超时、重复投递、乱序故障注入）：
  - Subscribe 断流：读流中断 → RemoteUnavailableError（可解释失败，不静默成功）；
  - Stream 模式断流：不重放副作用，先查询远端 Task 决定继续/失败；
  - 有限重试：仅超时/断流/限流重试，重试不产生重复副作用；
  - 重复投递：Client Ledger 幂等去重 / Server 事件投递去重（record_event）；
  - 乱序事件 + Last-Event-ID：游标续传只收之后事件；
  - 熔断：连续失败阈值开启 → 拒绝后续调用（不再发网络请求）→ 冷却后半开恢复。
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.a2a.client import (
    A2AClient,
    AuthError,
    MemoryCallLedger,
    RemoteAgentConfig,
    RemoteUnavailableError,
)
from agent.a2a.models import TaskStatus, TaskStatusUpdateEvent
from agent.a2a.task_store import A2ATaskStore

CARD_BASE = "http://peer.test"


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _card_json(*, stream: bool = False):
    return {
        "name": "Mock Agent",
        "description": "mock remote agent",
        "url": f"{CARD_BASE}/.well-known/agent-card.json",
        "protocol_version": "1.0",
        "version": "1.0.0",
        "skills": [
            {
                "id": "pr_intel",
                "name": "PR",
                "description": "read-only",
                "tags": ["read-only"],
                "input_modes": ["text"],
                "output_modes": ["text", "stream"] if stream else ["text"],
            }
        ],
    }


def _task_json(*, task_id="t1", status="COMPLETED"):
    return {
        "id": task_id,
        "status": status,
        "created_timestamp": "2026-08-10T12:00:00Z",
        "last_updated_timestamp": "2026-08-10T12:00:01Z",
    }


def _msg(text="分析近 7 天 PR 情报", *, message_id="m1", task_id="", **meta):
    from agent.a2a.models import Message, Part

    return Message(
        message_id=message_id,
        task_id=task_id,
        role="user",
        parts=[Part(kind="text", text=text)],
        context_id="ctx-1",
        metadata={"skill_id": "pr_intel", **meta},
    )


class _AsyncNoSleep:
    async def __call__(self, _: float):
        return None


def _make_client(handler, *, base_url=CARD_BASE, stream=False, resolver=None, **cfg_extra):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False
    )
    cfg = RemoteAgentConfig(
        key="peer-1",
        base_url=base_url,
        enabled_skills=["pr_intel"],
        require_https=False,
        retry_max=cfg_extra.pop("retry_max", 1),
        **cfg_extra,
    )
    client = A2AClient(
        allowlist={"peer-1": cfg},
        http_client=http,
        sleep=_AsyncNoSleep(),
        resolver=resolver or (lambda host: ["93.184.216.34"]),
    )
    return client


class _BurstThenBreakStream(httpx.AsyncByteStream):
    """响应体流：先吐出若干字节，再抛 ReadError 模拟断流。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def __aiter__(self):
        for c in self._chunks:
            yield c
        raise httpx.ReadError("mid-stream break")


def _sse_frame(payload: dict) -> bytes:
    return (
        f"id: {payload.get('sequence', 0)}\n"
        "event: task_status_update\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


# ═══════════════════════════════════════════════════════════════
# Subscribe 断流
# ═══════════════════════════════════════════════════════════════


class TestSubscribeInterruption:
    async def test_stream_breaks_before_any_event_raises(self):
        def handler(request):
            raise httpx.ReadError("connection reset")

        client = _make_client(handler)
        with pytest.raises(RemoteUnavailableError):
            async for _ in client.subscribe("peer-1", "t1"):
                pass

    async def test_partial_events_then_break_raises(self):
        frames = [
            _sse_frame({"event_id": "ev-1", "task_id": "t1", "status": "WORKING", "sequence": 1}),
            _sse_frame({"event_id": "ev-2", "task_id": "t1", "status": "WORKING", "sequence": 2}),
        ]

        def handler(request):
            return httpx.Response(200, stream=_BurstThenBreakStream(frames))

        client = _make_client(handler)
        got: list[TaskStatusUpdateEvent] = []
        with pytest.raises(RemoteUnavailableError):
            async for ev in client.subscribe("peer-1", "t1"):
                got.append(ev)
        # 中断前已到达的事件正常产出；中断本身是可解释失败，不伪造完成
        assert len(got) == 2
        assert [e.event_id for e in got] == ["ev-1", "ev-2"]


# ═══════════════════════════════════════════════════════════════
# Stream 模式发送：断流后先查远端 Task 决定继续/失败
# ═══════════════════════════════════════════════════════════════


class _FakeHttp:
    """定向注入：流端点返回『吐几帧后断流』的响应；Task 端点返回受控响应。"""

    def __init__(self, stream_resp: httpx.Response, task_resp: httpx.Response):
        self.stream_resp = stream_resp
        self.task_resp = task_resp

    async def request(self, method, url, **kwargs):
        if "/a2a/tasks/" in str(url):
            return self.task_resp
        return self.stream_resp

    async def stream(self, *args, **kwargs):
        raise AssertionError("unexpected stream() call in _dispatch")

    async def aclose(self):
        return None


class TestStreamSendRecovery:
    def _client(self, stream_resp, task_resp):
        from agent.a2a.models import AgentCard

        cfg = RemoteAgentConfig(
            key="peer-1", base_url=CARD_BASE, enabled_skills=["pr_intel"],
            require_https=False, retry_max=1,
        )
        client = A2AClient(
            allowlist={"peer-1": cfg},
            http_client=_FakeHttp(stream_resp, task_resp),
            sleep=_AsyncNoSleep(),
            resolver=lambda host: ["93.184.216.34"],
        )
        card = AgentCard.model_validate(_card_json(stream=True))
        return client, card

    async def test_interrupted_stream_recovers_by_querying_remote(self):
        """流中断但远端 Task 真实存在 → 通过 get_task 恢复（远端权威，不重放副作用）。"""
        frame = _sse_frame(
            {"event_id": "ev-1", "task_id": "t1", "status": "WORKING", "sequence": 1}
        )
        stream_resp = httpx.Response(200, stream=_BurstThenBreakStream([frame]))
        task_resp = httpx.Response(200, json=_task_json())
        client, card = self._client(stream_resp, task_resp)
        cfg = client.allowlist["peer-1"]
        task = await client._dispatch(cfg, card, _msg(message_id="m1"), "pr_intel", "stream")
        # 断流后先查询远端 Task：拿到真实终态而非伪造成功
        assert task.id == "t1"
        assert task.status == TaskStatus.COMPLETED

    async def test_interrupted_stream_remote_unknown_fails_explainably(self):
        """流中断且远端 Task 查不到 → RemoteUnavailableError（不静默成功）。"""
        frame = _sse_frame(
            {"event_id": "ev-1", "task_id": "t1", "status": "WORKING", "sequence": 1}
        )
        stream_resp = httpx.Response(200, stream=_BurstThenBreakStream([frame]))
        task_resp = httpx.Response(404)  # 远端 Task 不存在 → 恢复无法继续
        client, card = self._client(stream_resp, task_resp)
        cfg = client.allowlist["peer-1"]
        with pytest.raises(RemoteUnavailableError) as excinfo:
            await client._dispatch(cfg, card, _msg(message_id="m1"), "pr_intel", "stream")
        assert "stream interrupted" in str(excinfo.value)


# ═══════════════════════════════════════════════════════════════
# 有限重试：超时 / 断流重试且不重复副作用
# ═══════════════════════════════════════════════════════════════


class TestRetryNoDuplicateSideEffect:
    async def test_timeout_then_success(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                if send_calls["n"] == 1:
                    raise httpx.TimeoutException("request timed out")
                return httpx.Response(200, json={"task": _task_json()})
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, retry_max=2)
        task = await client.send("peer-1", _msg(message_id="m1"))
        assert task.status == TaskStatus.COMPLETED
        assert send_calls["n"] == 2
        usage = client._usage(client.allowlist["peer-1"])
        assert usage.retries == 1

    async def test_timeout_exhausted_raises_explainably(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                raise httpx.TimeoutException("request timed out")
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, retry_max=2)
        with pytest.raises(RemoteUnavailableError):
            await client.send("peer-1", _msg(message_id="m1"))
        assert send_calls["n"] == 3  # retry_max+1 次尝试后耗尽

    async def test_retry_success_records_exactly_once(self):
        """重试成功只记账一次成功记录；再次发送同幂等键不再产生副作用。"""
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                if send_calls["n"] == 1:
                    raise httpx.ConnectError("connection refused")
                return httpx.Response(200, json={"task": _task_json()})
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, retry_max=1)
        await client.send("peer-1", _msg(message_id="m1"))
        again = await client.send("peer-1", _msg(message_id="m1"))  # 重复投递
        assert again.metadata.get("from_ledger") is True
        # 远端只被调用 1 次成功（第 1 次连接失败 + 1 次成功，重试不重复副作用）
        assert send_calls["n"] == 2
        records = [r for r in client.ledger._records if r.idempotency_key == "m1"]
        assert len(records) == 1
        assert records[0].status == "COMPLETED"

    async def test_duplicate_delivery_across_clients_deduped(self):
        """不同 client 实例共享同一账本时，同幂等键查账复用（不重复副作用）。"""
        ledger = MemoryCallLedger()
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(200, json={"task": _task_json()})
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)

        def _new_client():
            return A2AClient(
                allowlist={
                    "peer-1": RemoteAgentConfig(
                        key="peer-1", base_url=CARD_BASE,
                        enabled_skills=["pr_intel"], require_https=False, retry_max=1,
                    )
                },
                http_client=http,
                ledger=ledger,
                sleep=_AsyncNoSleep(),
                resolver=lambda host: ["93.184.216.34"],
            )

        c1 = _new_client()
        await c1.send("peer-1", _msg(message_id="dup-1"))
        c2 = _new_client()
        task = await c2.send("peer-1", _msg(message_id="dup-1"))
        assert task.metadata.get("from_ledger") is True
        assert send_calls["n"] == 1  # 共享账本：第二个实例未再调用远端


# ═══════════════════════════════════════════════════════════════
# 乱序事件 + Last-Event-ID 游标续传 / 投递去重
# ═══════════════════════════════════════════════════════════════


class TestOutOfOrderAndDedup:
    async def test_client_delivers_out_of_order_events_in_arrival_order(self):
        """事件 ID 乱序到达时按到达顺序产出，每帧携带自己的游标，不丢不重。"""
        frames = [
            _sse_frame({"event_id": "ev-5", "task_id": "t1", "status": "WORKING", "sequence": 5}),
            _sse_frame({"event_id": "ev-3", "task_id": "t1", "status": "WORKING", "sequence": 3}),
            _sse_frame({"event_id": "ev-7", "task_id": "t1", "status": "COMPLETED", "sequence": 7}),
            b"event: done\ndata: {\"status\":\"completed\"}\n\n",
        ]

        def handler(request):
            assert request.url.path == "/a2a/tasks/t1/resubscribe"
            return httpx.Response(200, content=b"".join(frames))

        client = _make_client(handler)
        got: list[TaskStatusUpdateEvent] = []
        async for ev in client.subscribe("peer-1", "t1", last_event_id="2"):
            got.append(ev)
        assert [e.event_id for e in got] == ["ev-5", "ev-3", "ev-7"]
        assert [e.status.value for e in got] == ["WORKING", "WORKING", "COMPLETED"]
        # Last-Event-ID 头已携带游标（4B-5：事件 ID + 游标重连）
        seen = {"header": None}
        frames2 = [b"event: done\ndata: {\"status\":\"completed\"}\n\n"]

        def handler2(request):
            seen["header"] = request.headers.get("last-event-id")
            return httpx.Response(200, content=b"".join(frames2))

        c2 = _make_client(handler2)
        async for _ in c2.subscribe("peer-1", "t1", last_event_id="42"):
            pass
        assert seen["header"] == "42"

    async def test_server_event_delivery_dedup(self):
        """同一 (task_id, event_id) 只投递一次；重复订阅/推送幂等。"""
        db = _FakeDB()
        store = A2ATaskStore(db)
        assert await store.record_event("t1", "ev-1") is True
        assert await store.record_event("t1", "ev-1") is False  # 重复投递被拒
        assert await store.record_event("t1", "ev-2") is True
        assert await store.has_event("t1", "ev-1") is True
        assert await store.has_event("t1", "ev-9") is False


# ═══════════════════════════════════════════════════════════════
# 熔断：连续失败阈值 → 拒绝后续 → 冷却后半开恢复
# ═══════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    async def test_breaker_opens_blocks_then_half_open_recovers(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(401)  # 认证失败（不可重试）→ AuthError
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(
            handler, breaker_fail_threshold=1, breaker_cooldown_seconds=10.0
        )
        cfg = client.allowlist["peer-1"]
        fake_time = {"t": 0.0}
        client._monotonic = lambda: fake_time["t"]

        with pytest.raises(AuthError):
            await client.send("peer-1", _msg(message_id="m1"))
        breaker = client._breaker(cfg)
        assert breaker.state == "open"

        calls_before = send_calls["n"]
        with pytest.raises(RemoteUnavailableError):
            await client.send("peer-1", _msg(message_id="m2"))
        assert send_calls["n"] == calls_before  # 熔断期间不再发网络请求（防拖垮 Runtime）

        # 冷却结束 → 半开放行一次探测
        fake_time["t"] += 10.0
        with pytest.raises(AuthError):  # 探测仍失败 → 重新熔断
            await client.send("peer-1", _msg(message_id="m3"))
        assert breaker.state == "open"

    async def test_success_recloses_breaker(self):
        calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                calls["n"] += 1
                return httpx.Response(200, json={"task": _task_json()})
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, breaker_fail_threshold=3)
        cfg = client.allowlist["peer-1"]
        breaker = client._breaker(cfg)
        # 预置 2 次连续失败，第 3 次成功 → 计数清零、闭合
        breaker.on_failure()
        breaker.on_failure()
        await client.send("peer-1", _msg(message_id="m1"))
        assert breaker.state == "closed"
        assert breaker.failures == 0


# ═══════════════════════════════════════════════════════════════
# 极简 Fake Mongo（仅 record_event 所需）
# ═══════════════════════════════════════════════════════════════


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc):
        self.docs.append(doc)

    async def find_one(self, query=None, sort=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in (query or {}).items()):
                return d
        return None


class _FakeDB(dict):
    def __init__(self):
        super().__init__()
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str):
        if name not in self._cols:
            self._cols[name] = _FakeCol()
        return self._cols[name]
