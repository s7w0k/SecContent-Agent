"""
流水线 REST API — 触发与状态查询

端点:
  POST /api/pipeline/run      触发全流程
  POST /api/pipeline/crawl    仅爬取
  POST /api/pipeline/score    仅打分
  POST /api/pipeline/report   仅生成报道
  GET  /api/pipeline/status   查询运行状态
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/pipeline", tags=["Pipeline"])


# ═══════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════


class PipelineRunRequest(BaseModel):
    crawl_days: int = Field(default=1, ge=1, le=30, description="爬取天数")
    phases: list[str] = Field(
        default=["crawl", "classify", "score", "report"],
        description="要执行的阶段",
    )


class PipelinePhaseRequest(BaseModel):
    crawl_days: int = Field(default=1, ge=1, le=30, description="爬取天数")


class ScoreRequest(BaseModel):
    article_url_hashes: Optional[list[str]] = Field(
        default=None, description="指定文章 hash 列表（留空则对所有已分类文章打分）"
    )


class ReportRequest(BaseModel):
    article_url_hashes: Optional[list[str]] = Field(
        default=None, description="指定文章 hash 列表（留空则对所有高分文章生成报道）"
    )


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_manager(request: Request):
    """从 app.state 获取 PipelineManager"""
    manager = getattr(request.app.state, "pipeline_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    return manager


# ═══════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════


@router.post("/run", summary="触发完整流水线")
async def pipeline_run(body: PipelineRunRequest, request: Request):
    """执行全流程：crawl → classify → score → report"""
    manager = _get_manager(request)
    result = await manager.run_full(crawl_days=body.crawl_days)
    return result


@router.post("/crawl", summary="仅执行爬取阶段")
async def pipeline_crawl(body: PipelinePhaseRequest, request: Request):
    """仅爬取文章（crawl → classify）"""
    manager = _get_manager(request)
    result = await manager.run_phase("classify", crawl_days=body.crawl_days)
    return result


@router.post("/score", summary="仅执行打分阶段")
async def pipeline_score(body: ScoreRequest, request: Request):
    """对已分类文章重新打分"""
    manager = _get_manager(request)
    # 仅执行 score 阶段（跳过 crawl 和 classify）
    result = await manager.run_phase("score", crawl_days=0)
    return result


@router.post("/report", summary="仅生成报道")
async def pipeline_report(body: ReportRequest, request: Request):
    """对高分文章生成 PR 报道"""
    manager = _get_manager(request)
    result = await manager.run_phase("report", crawl_days=0)
    return result


@router.get("/status", summary="查询流水线状态")
async def pipeline_status(request: Request):
    """返回当前流水线的运行状态和进度"""
    manager = _get_manager(request)
    return manager.get_status()
