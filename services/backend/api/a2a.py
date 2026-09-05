"""A2A 1.0 REST 路由 — 阶段四 4B Step 4B-2。

端点（HTTP + JSON/REST，A2A-Version 头校验）：
  GET  /.well-known/agent-card.json              Agent Card（只发布真实开放能力）
  POST /a2a/message/send                          消息提交（TaskSendResult）
  POST /a2a/message/stream                        消息提交 + 事件流（SSE）
  POST /a2a/tasks/query                           任务查询（协议标准）
  GET  /a2a/tasks                                 任务列表（等价查询，站内便捷）
  GET  /a2a/tasks/{task_id}                       任务详情（多租户隔离）
  POST /a2a/tasks/{task_id}/cancel                取消任务
  POST /a2a/tasks/{task_id}/resubscribe           订阅任务事件流（Last-Event-ID 续传）

请求链路：认证 -> 协议校验 -> 输入净化 -> PolicyEngine -> 创建/关联内部运行
          -> Runtime 执行 -> 状态/Artifact 映射 -> 响应或事件流

错误语义：
  - A2A-Version 头缺失/不匹配         -> 400（协议错误）
  - 不可信输入被拒绝（净化失败）       -> 400
  - 未实现能力（Skill/方法未开放）     -> 501（明确协议错误，不静默伪装成功）
  - 多租户隔离 / 归属冲突             -> 404 / 409
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.a2a.models import (
    AGENT_CARD_PATH,
    PROTOCOL_VERSION,
    VERSION_HEADER,
    A2AError,
    AgentCard,
    InvalidInputError,
    Message,
    MethodNotImplementedError,
    ProtocolError,
    Task,
    TaskSendResult,
)
from agent.a2a.server import A2AServer
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.api.a2a")

router = APIRouter(tags=["A2A"])


# ═══════════════════════════════════════════════════════════════
# 依赖与辅助
# ═══════════════════════════════════════════════════════════════


async def _check_protocol_version(
    a2a_version: str | None = Header(default=None, alias="A2A-Version"),
) -> None:
    if a2a_version != PROTOCOL_VERSION:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "version_error",
                    "message": f"{VERSION_HEADER} must be {PROTOCOL_VERSION}",
                }
            },
        )


def _get_server(request: Request) -> A2AServer:
    server = getattr(request.app.state, "a2a_server", None)
    if server is None:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "not_initialized", "message": "A2A service not initialized"}},
        )
    return server


def _translate(exc: A2AError) -> HTTPException:
    if isinstance(exc, MethodNotImplementedError):
        return HTTPException(
            status_code=501,
            detail={"error": {"code": "method_not_implemented", "message": str(exc)}},
        )
    if isinstance(exc, ProtocolError):
        return HTTPException(
            status_code=400, detail={"error": {"code": "protocol_error", "message": str(exc)}}
        )
    if isinstance(exc, InvalidInputError):
        return HTTPException(
            status_code=400, detail={"error": {"code": "invalid_input", "message": str(exc)}}
        )
    return HTTPException(
        status_code=409, detail={"error": {"code": "conflict", "message": str(exc)}}
    )


def _sse_events(events) -> Any:
    """TaskStatusUpdateEvent 流 -> SSE 帧（event_id 用运行事件 sequence 做游标）。"""

    async def stream():
        async for ev in events:
            seq = int(ev.metadata.get("sequence", 0))
            yield (
                f"id: {seq}\n"
                f"event: task_status_update\n"
                f"data: {json.dumps(ev.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            )
        yield f"event: done\ndata: {json.dumps({'status': 'completed'}, ensure_ascii=False)}\n\n"

    return stream()


# ═══════════════════════════════════════════════════════════════
# Agent Card
# ═══════════════════════════════════════════════════════════════


@router.get(AGENT_CARD_PATH, summary="Agent Card（能力声明）")
async def agent_card(
    request: Request,
    _user_id: str = Depends(get_current_user),
) -> AgentCard:
    return _get_server(request).agent_card


# ═══════════════════════════════════════════════════════════════
# Message Send / Stream
# ═══════════════════════════════════════════════════════════════


@router.post("/a2a/message/send", summary="Message Send")
async def message_send(
    body: Message,
    request: Request,
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
) -> TaskSendResult:
    server = _get_server(request)
    try:
        return await server.send(body, principal=server.principal(user_id))
    except A2AError as exc:
        raise _translate(exc) from exc


@router.post("/a2a/message/stream", summary="Message Stream（SSE）")
async def message_stream(
    body: Message,
    request: Request,
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
):
    server = _get_server(request)
    principal = server.principal(user_id)
    try:
        result = await server.send(body, principal=principal)
    except A2AError as exc:
        raise _translate(exc) from exc
    task_id = result.task.id if result.task else body.task_id
    events = server.subscribe(task_id, principal=principal, last_event_id="")
    return StreamingResponse(
        _sse_events(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            f"{VERSION_HEADER}": PROTOCOL_VERSION,
        },
    )


# ═══════════════════════════════════════════════════════════════
# Tasks Get / List / Query / Cancel / Resubscribe
# ═══════════════════════════════════════════════════════════════


class TaskQueryRequest(BaseModel):
    status: str = Field(default="", max_length=32)
    history_length: int = Field(default=0, ge=0, le=100)


class TaskQueryResponse(BaseModel):
    tasks: list[Task] = Field(default_factory=list)
    next_cursor: str | None = None


@router.post("/a2a/tasks/query", summary="Tasks/Query")
async def tasks_query(
    body: TaskQueryRequest,
    request: Request,
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
) -> TaskQueryResponse:
    server = _get_server(request)
    tasks = await server.list_tasks(
        principal=server.principal(user_id), status=body.status, limit=50
    )
    return TaskQueryResponse(tasks=tasks)


@router.get("/a2a/tasks", summary="Tasks/List（站内便捷）")
async def tasks_list(
    request: Request,
    status: str = "",
    limit: int = 50,
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
) -> TaskQueryResponse:
    server = _get_server(request)
    tasks = await server.list_tasks(
        principal=server.principal(user_id), status=status, limit=min(limit, 200)
    )
    return TaskQueryResponse(tasks=tasks)


@router.get("/a2a/tasks/{task_id}", summary="Tasks/Get")
async def tasks_get(
    task_id: str,
    request: Request,
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
) -> Task:
    server = _get_server(request)
    task = await server.get_task(task_id, principal=server.principal(user_id))
    if task is None:
        raise HTTPException(
            status_code=404, detail={"error": {"code": "not_found", "message": "task not found"}}
        )
    return task


@router.post("/a2a/tasks/{task_id}/cancel", summary="Tasks/Cancel")
async def tasks_cancel(
    task_id: str,
    request: Request,
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
) -> Task:
    server = _get_server(request)
    task = await server.cancel(task_id, principal=server.principal(user_id))
    if task is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found", "message": "task not found or not cancelable"}},
        )
    return task


@router.post("/a2a/tasks/{task_id}/resubscribe", summary="Tasks/Resubscribe（SSE 游标续传）")
async def tasks_resubscribe(
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _version: None = Depends(_check_protocol_version),
    user_id: str = Depends(get_current_user),
):
    server = _get_server(request)
    principal = server.principal(user_id)
    # 预检（subscribe 是懒生成器，未知任务需在此显式 404，避免流式阶段才抛错）
    task = await server.get_task(task_id, principal=principal)
    if task is None or not task.internal_run_id:
        raise HTTPException(
            status_code=404, detail={"error": {"code": "not_found", "message": "task not found"}}
        )
    try:
        events = server.subscribe(task_id, principal=principal, last_event_id=last_event_id or "")
    except A2AError as exc:
        raise _translate(exc) from exc
    return StreamingResponse(
        _sse_events(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            f"{VERSION_HEADER}": PROTOCOL_VERSION,
        },
    )
