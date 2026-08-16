from __future__ import annotations

from agent.business_tools import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
    build_business_tool_registry,
)
from agent.conversational_service import ConversationalAgentService
from api.agent import router
from auth.deps import get_current_tenant, get_current_user
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _app() -> tuple[FastAPI, dict[str, str]]:
    app = FastAPI()
    identity = {"tenant_id": "tenant-a"}
    registry = build_business_tool_registry()
    app.state.conversational_agent_service = ConversationalAgentService(
        tool_executor=BusinessToolExecutor(
            registry, {BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter()}
        )
    )
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: "user-a"
    app.dependency_overrides[get_current_tenant] = lambda: identity["tenant_id"]
    return app, identity


async def test_agent_api_idempotency_tenant_isolation_and_sse_resume():
    app, identity = _app()
    transport = ASGITransport(app=app)
    body = {
        "content": "搜索 AI 安全新闻",
        "turn_id": "turn-api-1",
        "thread_id": "thread-api-1",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/agent/turns", json=body)
        assert first.status_code == 200
        payload = first.json()
        run_id = payload["run"]["run_id"]

        duplicate = await client.post("/api/agent/turns", json=body)
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["run"]["run_id"] == run_id

        conflict = await client.post(
            "/api/agent/turns",
            json={**body, "content": "不同的请求正文"},
        )
        assert conflict.status_code == 409

        detail = await client.get(f"/api/agent/runs/{run_id}")
        assert detail.status_code == 200
        identity["tenant_id"] = "tenant-b"
        denied = await client.get(f"/api/agent/runs/{run_id}")
        assert denied.status_code == 404
        identity["tenant_id"] = "tenant-a"

        events = await client.get(f"/api/agent/runs/{run_id}/events")
        assert events.status_code == 200
        assert "event: turn.persisted" in events.text
        resumed = await client.get(
            f"/api/agent/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )
        assert "event: turn.persisted" not in resumed.text
        assert "event: understanding.completed" in resumed.text


async def test_agent_api_cancel_and_approve_are_scoped_and_idempotent():
    app, _identity = _app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/agent/turns",
            json={"content": "写稿", "turn_id": "turn-wait"},
        )
        run_id = response.json()["run"]["run_id"]
        canceled = await client.post(f"/api/agent/runs/{run_id}/cancel")
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"
        repeated = await client.post(f"/api/agent/runs/{run_id}/cancel")
        assert repeated.status_code == 200
        approved = await client.post(f"/api/agent/runs/{run_id}/approve")
        assert approved.status_code == 200
        assert approved.json()["status"] == "canceled"
