"""自主模式 API — 阶段四 4A Step 4A-9。

端点：
  POST /api/autonomous/runs                         创建并启动自主运行
  GET  /api/autonomous/runs                         运行列表（多租户隔离）
  GET  /api/autonomous/runs/{run_id}                运行详情（脱敏）
  GET  /api/autonomous/runs/{run_id}/events         SSE 事件流（Last-Event-ID 续传）
  POST /api/autonomous/runs/{run_id}/cancel         取消（安全点停止）
  POST /api/autonomous/runs/{run_id}/resume         恢复（WAITING_APPROVAL）
  POST /api/autonomous/approvals/{approval_id}/approve   审批通过
  POST /api/autonomous/approvals/{approval_id}/reject    审批拒绝

安全约束：
  - 全部写入为脱敏数据；SSE/详情不返回参数原文、提示词与私有推理；
  - 多租户：所有读取按 user_id 过滤，跨用户访问返回 404。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agent.autonomous_service import AutonomousRunService
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.api.autonomous")

router = APIRouter(prefix="/api/autonomous", tags=["Autonomous"])


# ═══════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════


class CreateRunRequest(BaseModel):
    goal: str = Field(..., min_length=3, max_length=2000)
    acceptance_criteria: list[str] = Field(..., min_length=1, max_length=20)
    thread_id: str = Field(default="", max_length=100)
    tool_chain: list[str] | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)


class RunSummary(BaseModel):
    run_id: str
    status: str
    current_step: str
    goal: str
    completed_steps: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    evidence_count: int = 0
    decision_count: int = 0
    budget_usage: dict[str, Any] = Field(default_factory=dict)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    decision_summaries: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    checkpoint_version: int = 1


def _summarize(state) -> RunSummary:
    """脱敏摘要：不返回参数/提示词/推理链，仅返回决策摘要元数据。"""
    return RunSummary(
        run_id=state.run_id,
        status=state.status.value,
        current_step=state.current_step,
        goal=state.goal[:2000],
        completed_steps=list(state.completed_steps),
        failed_steps=list(state.failed_steps),
        pending_steps=list(state.pending_steps),
        evidence_count=len(state.evidence),
        decision_count=len(state.decision_summaries),
        budget_usage={
            "steps": state.usage.steps,
            "input_tokens": state.usage.input_tokens,
            "output_tokens": state.usage.output_tokens,
            "tool_calls": state.usage.tool_calls,
            "retries": state.usage.retries,
            "cost_usd": state.usage.cost_usd,
            "consecutive_failures": state.usage.consecutive_failures,
        },
        pending_approvals=[
            {
                "approval_id": a.approval_id,
                "action": a.action,
                "risk_level": a.risk_level,
                "params_summary": a.params_summary[:300],
                "status": a.status,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            }
            for a in state.approval_state.pending_approvals
        ],
        # 决策摘要（脱敏：不含参数原文/提示词/私有推理链），只取最近 20 条
        decision_summaries=[
            {
                "step_id": d.step_id,
                "phase": d.phase,
                "action": d.action[:80],
                "tool_name": d.tool_name,
                "outcome": d.outcome,
                "reason": d.reason[:120],
            }
            for d in state.decision_summaries[-20:]
        ],
        evidence=[
            {
                "evidence_id": e.evidence_id,
                "step_id": e.step_id,
                "acceptance_index": e.acceptance_index,
                "kind": e.kind,
                "hash": e.hash,
                "note": e.note[:120],
            }
            for e in state.evidence[-20:]
        ],
        created_at=state.created_at.isoformat(),
        updated_at=state.updated_at.isoformat(),
        checkpoint_version=state.checkpoint_version,
    )


def _get_service(request: Request) -> AutonomousRunService:
    service = getattr(request.app.state, "autonomous_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Autonomous mode not initialized")
    return service


# ═══════════════════════════════════════════════════════════════
# REST
# ═══════════════════════════════════════════════════════════════


@router.post("/runs", summary="创建并启动自主运行")
async def create_run(
    body: CreateRunRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> RunSummary:
    service = _get_service(request)
    try:
        state = await service.create_run(
            user_id=user_id,
            goal=body.goal,
            acceptance_criteria=body.acceptance_criteria,
            thread_id=body.thread_id,
            tool_chain=body.tool_chain,
            max_steps=body.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    started = await service.start_run(state.run_id, user_id)
    if not started:
        raise HTTPException(status_code=409, detail="run cannot start")
    return _summarize(state)


@router.get("/runs", summary="运行列表")
async def list_runs(
    request: Request,
    status: str = "",
    limit: int = 50,
    user_id: str = Depends(get_current_user),
) -> list[RunSummary]:
    service = _get_service(request)
    states = await service.list_runs(user_id, status=status, limit=min(limit, 200))
    return [_summarize(s) for s in states]


@router.get("/runs/{run_id}", summary="运行详情（脱敏）")
async def get_run(
    run_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> RunSummary:
    service = _get_service(request)
    state = await service.get_run(run_id, user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _summarize(state)


@router.post("/runs/{run_id}/cancel", summary="取消运行")
async def cancel_run(
    run_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _get_service(request)
    if not await service.cancel_run(run_id, user_id):
        raise HTTPException(status_code=409, detail="run not cancelable")
    return {"run_id": run_id, "status": "cancel_requested"}


@router.post("/runs/{run_id}/resume", summary="恢复运行（审批后）")
async def resume_run(
    run_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> RunSummary:
    service = _get_service(request)
    if not await service.resume_run(run_id, user_id):
        raise HTTPException(status_code=409, detail="run not resumable")
    state = await service.get_run(run_id, user_id)
    return _summarize(state)


@router.post("/approvals/{approval_id}/approve", summary="审批通过")
async def approve_approval(
    approval_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _get_service(request)
    state = await service.approve(approval_id, user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="approval not found or already decided")
    return {"approval_id": approval_id, "status": "approved", "run_id": state.run_id}


@router.post("/approvals/{approval_id}/reject", summary="审批拒绝")
async def reject_approval(
    approval_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    service = _get_service(request)
    state = await service.reject(approval_id, user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="approval not found or already decided")
    return {"approval_id": approval_id, "status": "rejected", "run_id": state.run_id}


# ═══════════════════════════════════════════════════════════════
# SSE 事件流
# ═══════════════════════════════════════════════════════════════


@router.get("/runs/{run_id}/events", summary="SSE 事件流（Last-Event-ID 续传）")
async def run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: str = Depends(get_current_user),
):
    service = _get_service(request)
    state = await service.get_run(run_id, user_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Last-Event-ID：断线续传锚点（sequence）
    last_seq = 0
    if last_event_id:
        try:
            last_seq = int(last_event_id)
        except (TypeError, ValueError):
            last_seq = 0
    sse_schema_version = "1.0"

    async def event_stream():
        sent = last_seq
        # 防呆循环：先补齐已有事件，再轮询新事件直到终态
        while True:
            events = await service.events(run_id, user_id, last_sequence=sent)
            for ev in events:
                sent = ev.sequence
                payload = {
                    "schema_version": sse_schema_version,
                    "event_id": ev.event_id,
                    "sequence": ev.sequence,
                    "run_id": ev.run_id,
                    "event_type": ev.event_type,
                    "status": ev.status,
                    "timestamp": ev.timestamp.isoformat(),
                    "payload": ev.payload,
                }
                yield (
                    f"id: {ev.sequence}\n"
                    f"event: {ev.event_type}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
            current = await service.get_run(run_id, user_id)
            if current is None or current.is_terminal:
                yield f"event: done\ndata: {json.dumps({'run_id': run_id, 'status': current.status.value if current else 'gone'}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Autonomous-Schema-Version": sse_schema_version,
        },
    )
