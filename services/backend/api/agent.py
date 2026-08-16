"""Unified conversational Agent API introduced in stage 3."""

from __future__ import annotations

import json

from agent.conversational_service import (
    AgentRunRecord,
    AgentTurnInput,
    AgentTurnResult,
    ConversationalAgentService,
)
from agent.task_state_store import TaskStateConflictError
from auth.deps import get_current_tenant, get_current_user
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api/agent", tags=["Agent"])


def _service(request: Request) -> ConversationalAgentService:
    service = getattr(request.app.state, "conversational_agent_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="conversational Agent is not initialized")
    return service


@router.post("/turns", response_model=AgentTurnResult, summary="提交一个持久化对话任务轮次")
async def submit_turn(
    body: AgentTurnInput,
    request: Request,
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
) -> AgentTurnResult:
    try:
        return await _service(request).submit_turn(body, user_id=user_id, tenant_id=tenant_id)
    except TaskStateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs", response_model=list[AgentRunRecord], summary="列出当前用户的对话运行")
async def list_runs(
    request: Request,
    limit: int = 30,
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
) -> list[AgentRunRecord]:
    return await _service(request).list_runs(
        user_id=user_id, tenant_id=tenant_id, limit=max(1, min(limit, 200))
    )


@router.get("/runs/{run_id}", response_model=AgentRunRecord, summary="读取对话运行")
async def get_run(
    run_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
) -> AgentRunRecord:
    run = await _service(request).get_run(run_id, user_id=user_id, tenant_id=tenant_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/runs/{run_id}/events", summary="SSE 事件断点续传")
async def get_run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
):
    try:
        last_sequence = int(last_event_id or 0)
    except ValueError:
        last_sequence = 0
    events = await _service(request).events(
        run_id,
        user_id=user_id,
        tenant_id=tenant_id,
        last_sequence=last_sequence,
    )
    if events is None:
        raise HTTPException(status_code=404, detail="run not found")

    async def stream():
        for event in events:
            payload = event.model_dump(mode="json")
            yield (
                f"id: {event.sequence}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            )
        yield f"event: done\ndata: {json.dumps({'run_id': run_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Agent-Schema-Version": "1.0"},
    )


@router.post("/runs/{run_id}/cancel", response_model=AgentRunRecord, summary="幂等取消运行")
async def cancel_run(
    run_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
) -> AgentRunRecord:
    run = await _service(request).cancel(run_id, user_id=user_id, tenant_id=tenant_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/runs/{run_id}/approve", response_model=AgentRunRecord, summary="审批运行")
async def approve_run(
    run_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
) -> AgentRunRecord:
    run = await _service(request).approve(run_id, user_id=user_id, tenant_id=tenant_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run
