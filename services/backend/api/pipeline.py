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

import logging
import httpx
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


@router.post("/import-wewe", summary="导入 WeWe RSS 全部文章")
async def import_wewe_articles(request: Request):
    """从 WeWe RSS 获取全部文章并入库（含公众号来源和中文日期）。"""
    import hashlib
    import json
    from urllib.request import Request, urlopen
    from urllib.parse import quote
    from datetime import datetime, timezone, timedelta

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    log = logging.getLogger("backend.api.pipeline")
    log.info("[import-wewe] Starting WeWe article import...")

    try:
        # 1. 获取 Atom feed（含 <author><name> 公众号名称）
        import xml.etree.ElementTree as ET
        atom_url = "http://49.232.145.182:4001/feeds/all.atom"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(atom_url)
            xml_text = resp.text

        NS = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", NS)
        log.info(f"[import-wewe] Atom feed has {len(entries)} articles")

        # 2. 入库
        saved = 0
        tz = timezone(timedelta(hours=8))
        for entry in entries:
            title_el = entry.find("atom:title", NS)
            link_el = entry.find("atom:link", NS)
            author_el = entry.find("atom:author/atom:name", NS)
            updated_el = entry.find("atom:updated", NS)

            title = title_el.text if title_el is not None else ""
            url = link_el.get("href", "") if link_el is not None else ""
            source_name = author_el.text if author_el is not None else "微信公众号"
            pub_date_raw = updated_el.text if updated_el is not None else ""

            if not url:
                continue

            # 日期格式转换: "2026-06-29T01:02:20.000Z" → "2026年6月29日"
            pub_date = pub_date_raw[:10].replace("-", "年", 1).replace("-", "月") + "日" if pub_date_raw else ""

            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                continue

            doc = {
                "url_hash": url_hash,
                "title": title,
                "url": url,
                "source": source_name,
                "source_type": "wechat_mp",
                "published_at": pub_date,
                "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "summary": "",
                "summary_cn": "",
                "content_md": "",
                "is_ai_security": False,
                "is_agent_security": False,
                "category": "",
                "ai_relevance_score": 0,
                "reportability_score": 0,
                "score_reason": "",
                "has_report": False,
                "report_id": None,
                "pipeline_status": "crawled",
            }
            await db["articles"].insert_one(doc)
            saved += 1

        log.info(f"[import-wewe] Saved {saved} new ({len(entries) - saved} dupes)")
        return {"ok": True, "total": len(entries), "saved": saved}

    except Exception as e:
        log.error(f"[import-wewe] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
