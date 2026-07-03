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
    article_url_hashes: list[str] | None = Field(
        default=None, description="指定文章 hash 列表（留空则对所有已分类文章打分）"
    )


class ReportRequest(BaseModel):
    article_url_hashes: list[str] | None = Field(
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


@router.post("/crawl", summary="爬取+分类")
async def pipeline_crawl(body: PipelinePhaseRequest, request: Request):
    """爬取文章并分类（crawl → classify）"""
    manager = _get_manager(request)
    result = await manager.run_phase("classify", crawl_days=body.crawl_days)
    return result


@router.post("/crawl-overseas", summary="仅爬取海外安全新闻")
async def crawl_overseas_only(request: Request, days: int = 1):
    """仅调 mcp-crawl 爬取海外新闻 → 入库"""
    import hashlib
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    tz = timezone(timedelta(hours=8))
    tools = getattr(request.app.state, "pipeline_manager", None)
    if tools is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")

    try:
        result = await tools.tools["crawl_overseas_news"].ainvoke({"payload": {"days": days}})
        articles = []
        if result.get("ok") and result.get("data"):
            data = result["data"]
            articles = data.get("articles", []) if isinstance(data, dict) else data

        saved = 0
        for art in articles:
            url = art.get("url", "")
            if not url:
                continue
            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                continue
            await db["articles"].insert_one({
                "url_hash": url_hash, "title": art.get("title", ""), "url": url,
                "source": art.get("source", ""), "source_type": "overseas_news",
                "published_at": art.get("published_at", ""),
                "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "summary": art.get("summary", ""), "content_md": "",
                "pipeline_status": "crawled",
            })
            saved += 1
        return {"ok": True, "total": len(articles), "saved": saved}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/crawl-wewe", summary="仅爬取公众号文章")
async def crawl_wewe_only(request: Request):
    """仅直连 WeWe RSS Atom feed 爬取公众号文章 → 入库"""
    import hashlib
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    tz = timezone(timedelta(hours=8))
    log = logging.getLogger("backend.api.pipeline")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("http://49.232.145.182:4001/feeds/all.atom")
            xml_text = resp.text

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", ns)
        saved = 0
        for entry in entries:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            author_el = entry.find("atom:author/atom:name", ns)
            updated_el = entry.find("atom:updated", ns)
            title = title_el.text if title_el is not None else ""
            url = link_el.get("href", "") if link_el is not None else ""
            source = author_el.text if author_el is not None else "微信公众号"
            pub = updated_el.text[:10].replace("-", "年", 1).replace("-", "月") + "日" if updated_el is not None and updated_el.text else ""
            if not url:
                continue
            url_hash = hashlib.md5(url.encode()).hexdigest()
            existing = await db["articles"].find_one({"url_hash": url_hash})
            if existing:
                continue
            await db["articles"].insert_one({
                "url_hash": url_hash, "title": title, "url": url,
                "source": source, "source_type": "wechat_mp",
                "published_at": pub,
                "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "summary": "", "content_md": "", "pipeline_status": "crawled",
            })
            saved += 1
        log.info(f"[crawl-wewe] Saved {saved}/{len(entries)}")
        return {"ok": True, "total": len(entries), "saved": saved}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


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


@router.post("/crawl-api", summary="API 抓取公众号文章")
async def crawl_via_api(request: Request, days: int = 1):
    """通过 Just One API 抓取指定公众号文章 → 逐篇抓取全文 → 入库。"""
    import hashlib
    import os
    from datetime import datetime, timedelta, timezone

    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    api_url = os.getenv("JUST_ONE_API_URL", "https://api.justoneapi.com")
    api_token = os.getenv("JUST_ONE_API_TOKEN", "swgbMkTrhfvwP6Rv")
    # 从 MongoDB 读取配置的公众号列表
    cursor = db["crawl_accounts"].find().sort("name", 1)
    configs = await cursor.to_list(length=100)
    accounts = [c["name"] for c in configs] if configs else os.getenv("JUST_ONE_ACCOUNTS", "安恒信息,奇安信集团,绿盟科技").split(",")

    log = logging.getLogger("backend.api.pipeline")
    tz = timezone(timedelta(hours=8))
    all_articles = []

    try:
        # 1. 逐个公众号调 API（需要传 name 参数）
        async with httpx.AsyncClient(timeout=30) as client:
            for account in accounts:
                account = account.strip()
                if not account:
                    continue
                try:
                    resp = await client.get(
                        f"{api_url}/api/weixin/get-account-today-articles/v1"
                        f"?token={api_token}&name={account}"
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    arts = data.get("data", []) or data.get("articles", [])
                    if isinstance(arts, list):
                        for a in arts:
                            a["_source_account"] = account
                        all_articles.extend(arts)
                    log.info(f"[crawl-api] {account}: {len(arts) if isinstance(arts, list) else 0} articles")
                except Exception as e:
                    log.warning(f"[crawl-api] {account} failed: {e}")

        log.info(f"[crawl-api] Total articles from API: {len(all_articles)}")

        # 2. 逐篇抓取全文 + 入库
        saved, skipped = 0, 0
        async with httpx.AsyncClient(timeout=60) as client:
            for art in all_articles:
                url = art.get("url", "") or art.get("link", "")
                title = art.get("title", "")
                if not url:
                    continue

                url_hash = hashlib.md5(url.encode()).hexdigest()
                existing = await db["articles"].find_one({"url_hash": url_hash})
                if existing:
                    skipped += 1
                    continue

                # 抓取全文 (mcp-wewe)
                content = ""
                try:
                    fr = await client.post("http://mcp-wewe:8100/fetch-article", json={"link": url})
                    fd = fr.json()
                    content = fd.get("text", "") or ""
                except Exception:
                    pass

                source = art.get("_source_account", art.get("author_name", art.get("author", "微信公众号")))
                pub = art.get("publish_time", art.get("pub_time", art.get("created_at", "")))

                await db["articles"].insert_one({
                    "url_hash": url_hash,
                    "title": title,
                    "url": url,
                    "source": source,
                    "source_type": "wechat_mp",
                    "published_at": pub,
                    "added_at": datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    "summary": art.get("digest", art.get("summary", art.get("description", ""))),
                    "content_md": content[:50000],
                    "pipeline_status": "crawled",
                })
                saved += 1

        log.info(f"[crawl-api] Saved {saved}, skipped {skipped}")
        return {"ok": True, "total": len(all_articles), "saved": saved, "skipped": skipped}

    except Exception as e:
        log.error(f"[crawl-api] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/status", summary="查询流水线状态")
async def pipeline_status(request: Request):
    """返回当前流水线的运行状态和进度"""
    manager = _get_manager(request)
    return manager.get_status()


@router.post("/import-wewe", summary="导入 WeWe RSS 全部文章")
async def import_wewe_articles(request: Request):
    """从 WeWe RSS 获取全部文章并入库（含公众号来源和中文日期）。"""
    import hashlib
    from datetime import datetime, timedelta, timezone

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

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        entries = root.findall("atom:entry", ns)
        log.info(f"[import-wewe] Atom feed has {len(entries)} articles")

        # 2. 入库
        saved = 0
        tz = timezone(timedelta(hours=8))
        for entry in entries:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            author_el = entry.find("atom:author/atom:name", ns)
            updated_el = entry.find("atom:updated", ns)

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
        raise HTTPException(status_code=502, detail=str(e)) from e


# ═══════════════════════════════════════════════════════════════
# V2 6分类端点
# ═══════════════════════════════════════════════════════════════


class ClassifyV2Request(BaseModel):
    url_hashes: list[str] | None = Field(
        default=None, description="指定文章 hash 列表（留空则对所有 crawled 或 classified 文章分类）"
    )
    force: bool = Field(
        default=False, description="强制重新分类（忽略已有 category_v2）"
    )


@router.post("/classify-v2", summary="V2 6分类")
async def classify_v2(body: ClassifyV2Request, request: Request):
    """对文章执行6分类（爆点事件/法律法规/AI进展/竞品/行业/学术）。

    读取 pipeline_status 为 crawled 或 classified 的文章，
    调用 LLM 进行6类别归类，更新 category_v2 字段。
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")

    classifier = getattr(request.app.state, "classifier_v2", None)
    if classifier is None:
        raise HTTPException(status_code=503, detail="ClassifierV2 not initialized")

    log = logging.getLogger("backend.api.pipeline")

    try:
        # 查询待分类文章
        query: dict = {}
        if body.url_hashes:
            query["url_hash"] = {"$in": body.url_hashes}
        else:
            query["pipeline_status"] = {"$in": ["crawled", "classified"]}
            if not body.force:
                query["category_v2"] = {"$in": ["", None]}

        cursor = db["articles"].find(query)
        articles = await cursor.to_list(length=100)
        log.info(f"[classify-v2] Found {len(articles)} articles to classify")

        if not articles:
            return {"ok": True, "total": 0, "classified": 0, "results": []}

        # 批量分类
        results = await classifier.classify_batch(articles)

        # 更新数据库
        updated = 0
        for art, result in zip(articles, results, strict=False):
            try:
                await db["articles"].update_one(
                    {"_id": art["_id"]},
                    {"$set": {
                        "category_v2": result.category,
                        "category_v2_confidence": result.confidence,
                        "category_v2_reason": result.reason,
                        "category_v2_fallback": result.is_fallback,
                        "is_pr_eligible": result.is_pr_eligible,
                    }},
                )
                updated += 1
            except Exception as e:
                log.warning(f"[classify-v2] DB update failed: {e}")

        summary = {}
        for r in results:
            cat = r.category
            summary[cat] = summary.get(cat, 0) + 1

        log.info(
            f"[classify-v2] Done: {updated}/{len(articles)} updated, "
            f"{sum(1 for r in results if r.is_pr_eligible)} PR-eligible"
        )
        return {
            "ok": True,
            "total": len(articles),
            "classified": updated,
            "summary": summary,
            "results": [r.to_dict() for r in results],
        }

    except Exception as e:
        log.error(f"[classify-v2] Failed: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e
