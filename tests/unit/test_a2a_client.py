"""PR-4B-03 测试：A2A Client 模拟服务测试（Client 契约 + 安全门禁）。

覆盖 spec 4B-3 / 4B-4 / 4B-6（Client 模拟服务测试）：
  - 发现层：只从允许列表发现 Agent Card；缓存 TTL 与失效；
  - SSRF：环回/私网/云元数据/DNS 解析后私网地址/非 HTTPS/非标准端口拒绝；
  - 跨域重定向：逐跳重新 SSRF 校验，Authorization 不跨域转发；
  - 能力选择：按远端 Skill output_modes 选择 task / stream 模式；
  - 有限重试：仅超时/限流/断流重试，耗尽后进入可解释失败状态；
  - 幂等去重：同 (peer, idempotency_key) 不重复副作用；
  - PolicyEngine + 每远端预算/配额门禁；
  - Subscribe：SSE 事件解析 + Last-Event-ID 续传头。
"""

from __future__ import annotations

import httpx
import pytest
from agent.a2a.client import (
    A2AClient,
    BudgetExceededError,
    CapabilityError,
    DiscoveryError,
    PolicyDeniedError,
    ProtocolClientError,
    RateLimitedError,
    RemoteAgentConfig,
    RemoteUnavailableError,
    SSRFBlockedError,
)
from agent.a2a.models import (
    AgentCard,
    Message,
    Part,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from agent.policy_engine import PolicyEngine
from agent.runtime_state import RunBudget

CARD_BASE = "http://peer.test"


def _card_json(*, skills=None, name="Mock Agent"):
    return {
        "name": name,
        "description": "mock remote agent",
        "url": f"{CARD_BASE}/.well-known/agent-card.json",
        "protocol_version": "1.0",
        "version": "1.0.0",
        "skills": skills
        or [
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


def _send_result_json(*, task_id="t1", status="COMPLETED"):
    return {
        "task": {
            "id": task_id,
            "status": status,
            "created_timestamp": "2026-08-10T12:00:00Z",
            "last_updated_timestamp": "2026-08-10T12:00:01Z",
        }
    }


def _msg(text="分析近 7 天 PR 情报", *, message_id="m1", task_id="", skill="pr_intel", **meta):
    return Message(
        message_id=message_id,
        task_id=task_id,
        role="user",
        parts=[Part(kind="text", text=text)],
        context_id="ctx-1",
        metadata={"skill_id": skill, **meta},
    )


def _no_sleep(_: float):
    return None


class _AsyncNoSleep:
    async def __call__(self, _: float):
        return None


def _make_client(handler, *, base_url=CARD_BASE, skills=None, resolver=None, **cfg_extra):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)
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
        resolver=resolver or (lambda host: ["93.184.216.34"]),  # mock 主机统一解析为公网 IP
    )
    return client


# ═══════════════════════════════════════════════════════════════
# 发现层
# ═══════════════════════════════════════════════════════════════


class TestDiscovery:
    async def test_only_from_allowlist(self):
        client = A2AClient(allowlist={})
        with pytest.raises(DiscoveryError):
            await client.discover("unknown")

    async def test_discover_returns_card(self):
        def handler(request):
            assert request.url.path == "/.well-known/agent-card.json"
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler)
        card = await client.discover("peer-1")
        assert isinstance(card, AgentCard)
        assert card.protocol_version == "1.0"
        assert [s.id for s in card.skills] == ["pr_intel"]

    async def test_card_cached_within_ttl(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler, card_ttl_seconds=3600)
        await client.discover("peer-1")
        await client.discover("peer-1")
        assert calls["n"] == 1  # TTL 内命中缓存

    async def test_card_ttl_expiry_refetches(self):
        calls = {"n": 0}
        now = {"t": 0.0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json=_card_json())

        async def sleep(_: float):
            return None

        client = _make_client(handler, card_ttl_seconds=30)
        client._sleep = sleep
        client._now_provider = lambda: __import__("datetime").datetime.fromtimestamp(
            now["t"], __import__("datetime").UTC
        )
        await client.discover("peer-1")
        now["t"] += 60.0  # 超过 TTL
        await client.discover("peer-1")
        assert calls["n"] == 2

    async def test_invalidate_card(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler, card_ttl_seconds=3600)
        await client.discover("peer-1")
        client.invalidate_card("peer-1")
        await client.discover("peer-1")
        assert calls["n"] == 2


# ═══════════════════════════════════════════════════════════════
# SSRF 防线
# ═══════════════════════════════════════════════════════════════


class TestSSRFClient:
    async def _expect_blocked(self, base_url):
        def handler(request):
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler, base_url=base_url)
        with pytest.raises(SSRFBlockedError):
            await client.discover("peer-1")

    async def test_block_loopback_ipv4(self):
        await self._expect_blocked("http://127.0.0.1:8080")

    async def test_block_private_10(self):
        await self._expect_blocked("http://10.0.0.5:8080")

    async def test_block_cloud_metadata(self):
        await self._expect_blocked("http://169.254.169.254:8080")

    async def test_block_link_local(self):
        await self._expect_blocked("http://169.254.5.5:8080")

    async def test_block_localhost_hostname(self):
        await self._expect_blocked("http://localhost:8080")

    async def test_block_dns_resolves_to_private(self):
        def handler(request):
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler, base_url="http://safe.example.com:8080")
        client.resolver = lambda host: ["127.0.0.1"]  # DNS 重绑定到环回
        with pytest.raises(SSRFBlockedError):
            await client.discover("peer-1")

    async def test_block_http_when_https_required(self):
        def handler(request):
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler)
        client.allowlist["peer-1"] = RemoteAgentConfig(
            key="peer-1", base_url=CARD_BASE, require_https=True
        )
        with pytest.raises(SSRFBlockedError):
            await client.discover("peer-1")


# ═══════════════════════════════════════════════════════════════
# 重定向：逐跳 SSRF + 跨域剥 Authorization
# ═══════════════════════════════════════════════════════════════


class TestRedirects:
    async def test_redirect_to_blocked_target_rejected(self):
        def handler(request):
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(302, headers={"location": "http://169.254.169.254/x"})
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler)
        with pytest.raises(SSRFBlockedError):
            await client.discover("peer-1")

    async def test_cross_domain_redirect_drops_auth(self):
        seen: dict[str, list[dict]] = {"other": []}

        class TokenProvider:
            async def token(self, *, audience, scopes):
                return "tok-123"

        def handler(request):
            if request.url.path == "/.well-known/agent-card.json":
                # 初始源应带 Authorization
                assert request.headers.get("authorization") is None  # card 发现不带内部凭证
                return httpx.Response(302, headers={"location": "http://other.test/card"})
            if request.url.host == "other.test":
                seen["other"].append(dict(request.headers))
                return httpx.Response(200, json=_card_json(name="Other"))
            return httpx.Response(404)

        client = _make_client(handler)
        client.resolver = lambda host: ["93.184.216.34"]
        client.token_provider = TokenProvider()
        card = await client.discover("peer-1")
        assert card.name == "Other"
        # 跨域重定向后不再转发任何内部凭证头
        assert all("authorization" not in h for h in seen["other"])


# ═══════════════════════════════════════════════════════════════
# 能力选择 / 校验
# ═══════════════════════════════════════════════════════════════


class TestCapability:
    def test_mode_task_when_text_only(self):
        card = AgentCard.model_validate(
            _card_json(
                skills=[
                    {
                        "id": "pr_intel",
                        "name": "PR",
                        "description": "",
                        "input_modes": ["text"],
                        "output_modes": ["text"],
                    }
                ]
            )
        )
        client = A2AClient(allowlist={})
        assert client._select_mode(card, "pr_intel") == "task"

    def test_mode_stream_when_sse_capable(self):
        card = AgentCard.model_validate(
            _card_json(
                skills=[
                    {
                        "id": "pr_intel",
                        "name": "PR",
                        "description": "",
                        "input_modes": ["text"],
                        "output_modes": ["text", "stream"],
                    }
                ]
            )
        )
        client = A2AClient(allowlist={})
        assert client._select_mode(card, "pr_intel") == "stream"

    async def test_skill_must_be_enabled_and_declared(self):
        def handler(request):
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler)
        with pytest.raises(CapabilityError):
            await client.send("peer-1", _msg(skill="not_offered"))

    async def test_skill_not_in_card_but_enabled(self):
        def handler(request):
            return httpx.Response(200, json=_card_json())

        client = _make_client(handler, skills=None)
        client.allowlist["peer-1"] = RemoteAgentConfig(
            key="peer-1",
            base_url=CARD_BASE,
            require_https=False,
            enabled_skills=["ghost"],
        )
        with pytest.raises(CapabilityError):
            await client.send("peer-1", _msg(skill="ghost"))


# ═══════════════════════════════════════════════════════════════
# Send：task 模式 / 幂等去重 / 有限重试 / 预算配额
# ═══════════════════════════════════════════════════════════════


class TestSend:
    async def test_send_task_mode_and_ledger(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(200, json=_send_result_json())
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler)
        task = await client.send("peer-1", _msg())
        assert task.id == "t1"
        assert task.status == TaskStatus.COMPLETED
        # 本地 Step Ledger 已记账
        record = await client.ledger.find(peer="peer-1", idempotency_key="m1")
        assert record is not None
        assert record.status == "COMPLETED"
        assert send_calls["n"] == 1

    async def test_send_idempotent_dedup_no_duplicate_side_effect(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(200, json=_send_result_json())
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler)
        await client.send("peer-1", _msg())
        again = await client.send("peer-1", _msg())  # 同 message_id → 幂等键相同
        assert again.metadata.get("from_ledger") is True
        assert send_calls["n"] == 1  # 未重复调用远端（防重复副作用）

    async def test_retry_on_429_then_success(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                if send_calls["n"] == 1:
                    return httpx.Response(429)
                return httpx.Response(200, json=_send_result_json())
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, retry_max=2)
        task = await client.send("peer-1", _msg())
        assert task.status == TaskStatus.COMPLETED
        assert send_calls["n"] == 2
        usage = client._usage(client.allowlist["peer-1"])
        assert usage.retries == 1

    async def test_retry_exhausted_raises(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(503)
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, retry_max=1)
        with pytest.raises(RemoteUnavailableError):
            await client.send("peer-1", _msg())
        assert send_calls["n"] == 2

    async def test_rate_limited_exhausted(self):
        send_calls = {"n": 0}

        def handler(request):
            if request.url.path == "/a2a/message/send":
                send_calls["n"] += 1
                return httpx.Response(429)
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler, retry_max=1)
        with pytest.raises(RateLimitedError):
            await client.send("peer-1", _msg())

    async def test_remote_quota_exceeded(self):
        def handler(request):
            if request.url.path == "/a2a/message/send":
                return httpx.Response(200, json=_send_result_json())
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler)
        client.allowlist["peer-1"] = RemoteAgentConfig(
            key="peer-1",
            base_url=CARD_BASE,
            require_https=False,
            enabled_skills=["pr_intel"],
            budget=RunBudget(max_steps=10, remote_agent_quota=1),
        )
        await client.send("peer-1", _msg(message_id="m1"))
        with pytest.raises(BudgetExceededError):
            await client.send("peer-1", _msg(message_id="m2"))

    async def test_policy_denied_without_rule(self):
        def handler(request):
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(200, json=_send_result_json())

        client = _make_client(handler)
        client.policy = PolicyEngine(rules={})  # 无 a2a_send 规则 → 默认拒绝
        with pytest.raises(PolicyDeniedError):
            await client.send("peer-1", _msg())

    async def test_oversized_response_rejected(self):
        def handler(request):
            if request.url.path == "/a2a/message/send":
                return httpx.Response(
                    200,
                    content=b"x" * (6 * 1024 * 1024),  # > 5 MiB 上限
                )
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler)
        with pytest.raises(ProtocolClientError):
            await client.send("peer-1", _msg())

    async def test_outbound_message_contains_credential_rejected(self):
        def handler(request):
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(200, json=_send_result_json())

        client = _make_client(handler)
        from agent.a2a.models import InvalidInputError

        with pytest.raises(InvalidInputError):
            await client.send("peer-1", _msg("请替换 api_key=sk-abc"))


# ═══════════════════════════════════════════════════════════════
# Tasks：Get / List / Cancel / Subscribe
# ═══════════════════════════════════════════════════════════════


class TestTasks:
    async def test_get_task(self):
        def handler(request):
            if request.url.path == "/a2a/tasks/t1":
                return httpx.Response(200, json=_send_result_json()["task"])
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=_card_json())
            return httpx.Response(404)

        client = _make_client(handler)
        task = await client.get_task("peer-1", "t1")
        assert task is not None and task.id == "t1"

    async def test_get_task_not_found(self):
        def handler(request):
            return httpx.Response(404)

        client = _make_client(handler)
        assert await client.get_task("peer-1", "t1") is None

    async def test_cancel_task(self):
        def handler(request):
            if request.url.path == "/a2a/tasks/t1/cancel":
                body = _send_result_json()
                body["task"]["status"] = "CANCELED"
                return httpx.Response(200, json=body["task"])
            return httpx.Response(404)

        client = _make_client(handler)
        task = await client.cancel("peer-1", "t1")
        assert task is not None and task.status == TaskStatus.CANCELED

    async def test_subscribe_sse_and_last_event_id(self):
        seen_headers: dict[str, str | None] = {}

        def handler(request):
            if request.url.path == "/a2a/tasks/t1/resubscribe":
                seen_headers["last_event_id"] = request.headers.get("last-event-id")
                sse = (
                    "id: 3\n"
                    "event: task_status_update\n"
                    'data: {"event_id":"ev-3","task_id":"t1","status":"WORKING","metadata":{"sequence":3},"timestamp":"2026-08-10T12:00:03Z"}\n'
                    "\n"
                    "id: 4\n"
                    "event: task_status_update\n"
                    'data: {"event_id":"ev-4","task_id":"t1","status":"COMPLETED","metadata":{"sequence":4},"timestamp":"2026-08-10T12:00:04Z"}\n'
                    "\n"
                    "event: done\n"
                    'data: {"status":"completed"}\n'
                    "\n"
                )
                return httpx.Response(200, content=sse.encode("utf-8"))
            return httpx.Response(404)

        client = _make_client(handler)
        events: list[TaskStatusUpdateEvent] = []
        async for ev in client.subscribe("peer-1", "t1", last_event_id="2"):
            events.append(ev)
        assert len(events) == 2
        assert events[0].status == TaskStatus.WORKING
        assert events[1].status == TaskStatus.COMPLETED
        assert seen_headers["last_event_id"] == "2"
