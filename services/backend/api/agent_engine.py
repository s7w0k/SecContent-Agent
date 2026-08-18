"""聊天式 Agent 引擎 API — 真正 LLM tool-loop + 聊天工作台接口。

端点:
  POST /api/agent-engine/threads               新建会话（会话=n条消息）
  GET  /api/agent-engine/threads               会话列表
  GET  /api/agent-engine/threads/{id}          会话详情（含消息）
  POST /api/agent-engine/threads/{id}/messages 发送消息（触发 LLM 循环）
  GET  /api/agent-engine/threads/{id}/events   SSE 事件流（实时渲染工具调用）

前端体验对齐主流 Agent：
  用户发消息 -> 流式看到"Agent 的思考/计划(agent_message)"、
  "它正在调用什么工具(tool_call)"、"工具结果(tool_result)"、"最终交付(final)"。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agent.chat_agent_service import ChatAgentService
from auth.deps import get_current_tenant, get_current_user
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.api.agent_engine")

router = APIRouter(prefix="/api/agent-engine", tags=["AgentEngine"])


def _service(request: Request) -> ChatAgentService:
    service = getattr(request.app.state, "chat_agent_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Agent engine chat is not initialized")
    return service


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)
    manuscript_id: str | None = Field(default=None, max_length=64)


class ResolveApprovalRequest(BaseModel):
    approval_id: str = Field(..., min_length=1)
    approved: bool


class ThreadSummary(BaseModel):
    thread_id: str
    title: str = ""
    status: str = "idle"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


def _summarize(thread) -> dict[str, Any]:
    return ThreadSummary(
        thread_id=thread.thread_id,
        title=thread.title,
        status=thread.status,
        messages=[m.model_dump(mode="json") for m in thread.messages],
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    ).model_dump(mode="json")


@router.post("/threads", summary="新建会话")
async def create_thread(
    request: Request,
    user_id: str = Depends(get_current_user),
    tenant_id: str = Depends(get_current_tenant),
) -> dict[str, Any]:
    service = _service(request)
    thread = await service.create_thread(user_id, tenant_id)
    return _summarize(thread)


@router.get("/threads", summary="会话列表")
async def list_threads(
    request: Request,
    limit: int = 30,
    user_id: str = Depends(get_current_user),
) -> list[dict[str, Any]]:
    service = _service(request)
    threads = await service.list_threads(user_id, limit=max(1, min(limit, 100)))
    return [_summarize(t) for t in threads]


@router.get("/threads/{thread_id}", summary="会话详情")
async def get_thread(
    thread_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service(request)
    thread = await service.get_thread(thread_id, user_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return _summarize(thread)


@router.post("/approvals/resolve", summary="处理 HITL 工具审批（批准/拒绝）")
async def resolve_approval(
    body: ResolveApprovalRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service(request)
    ok = service.resolve_approval(body.approval_id, body.approved)
    if not ok:
        raise HTTPException(status_code=404, detail="approval not found or already resolved")
    return {"ok": True, "approval_id": body.approval_id, "approved": body.approved}


@router.post("/threads/{thread_id}/messages", summary="发送消息并触发 Agent 循环")
async def send_message(
    thread_id: str,
    body: SendMessageRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service(request)
    try:
        thread = await service.send_message(
            thread_id, user_id, body.content, manuscript_id=body.manuscript_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="thread not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summarize(thread)


@router.post("/threads/{thread_id}/stop", summary="中断当前正在进行的生成（保留断点，可\"继续\"续跑）")
async def stop_generation(
    thread_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service(request)
    thread = await service.get_thread(thread_id, user_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    stopped = service.stop_generation(thread_id)
    return {"ok": True, "stopped": stopped, "thread_id": thread_id}


@router.get("/threads/{thread_id}/events", summary="SSE 事件流")
async def thread_events(
    thread_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: str = Depends(get_current_user),
):
    service = _service(request)
    try:
        last_seq = int(last_event_id or 0)
    except (TypeError, ValueError):
        last_seq = 0

    # 校验归属
    thread = await service.get_thread(thread_id, user_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")

    async def event_stream():
        sent = last_seq
        seen_done = False
        idle_ticks = 0
        while True:
            events = service.events(thread_id, last_sequence=sent)
            for ev in events:
                sent = ev.sequence
                payload = {
                    "sequence": ev.sequence,
                    "event_type": ev.event_type,
                    "run_id": ev.run_id,
                    "thread_id": ev.thread_id,
                    "timestamp": ev.timestamp,
                    "data": ev.payload,
                }
                yield (
                    f"id: {ev.sequence}\n"
                    f"event: {ev.event_type}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
                if ev.event_type == "done":
                    seen_done = True
            if seen_done:
                # 让生成侧有机会写完持久化后返回；再给一小跳即断开
                idle_ticks += 1
                if idle_ticks > 2:
                    break
                await asyncio.sleep(0.4)
                continue
            current = service._live.get(thread_id)
            if current is None or (not current.running and not events):
                # 未在生成、没有新事件 —— 会话空闲，直接结束
                break
            await asyncio.sleep(0.2)
        yield f"event: done\ndata: {json.dumps({'thread_id': thread_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Agent-Engine-Schema-Version": "1.0",
        },
    )