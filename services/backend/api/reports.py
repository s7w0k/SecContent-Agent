"""
PR 报道 REST API — 报道列表、详情、知识库

端点:
  GET /api/reports             报道列表
  GET /api/reports/{id}        报道详情
  GET /api/knowledge           知识库摘要
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/api", tags=["Reports"])


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _get_knowledge(request: Request):
    knowledge = getattr(request.app.state, "knowledge_loader", None)
    if knowledge is None:
        raise HTTPException(status_code=503, detail="Knowledge loader not initialized")
    return knowledge


# ═══════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════


@router.get("/reports", summary="PR 报道列表")
async def list_reports(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=10, ge=1, le=50, description="每页条数"),
):
    """分页查询 PR 报道列表（按创建时间倒序）"""
    db = _get_db(request)

    total = await db["reports"].count_documents({})
    skip = (page - 1) * page_size

    cursor = db["reports"].find().sort("created_at", -1).skip(skip).limit(page_size)
    reports = await cursor.to_list(length=page_size)

    items = []
    for r in reports:
        r["_id"] = str(r["_id"])
        items.append(r)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/reports/{report_id}", summary="报道详情")
async def get_report(report_id: str, request: Request):
    """获取单篇 PR 报道全文（Markdown 格式）"""
    db = _get_db(request)

    # 验证 ID 格式（MongoDB ObjectId: 24 字符 hex）
    if len(report_id) != 24 or not all(c in "0123456789abcdefABCDEF" for c in report_id):
        raise HTTPException(status_code=400, detail="Invalid report ID format (expected 24-char hex)")

    # 延迟导入 ObjectId
    try:
        from bson import ObjectId
        oid = ObjectId(report_id)
    except ImportError:
        # 无 bson 时回退到字符串查询
        report = await db["reports"].find_one({"_id": report_id})
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        report["_id"] = str(report["_id"]) if "_id" in report else report_id
        return report

    report = await db["reports"].find_one({"_id": oid})
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    report["_id"] = str(report["_id"])
    return report


@router.get("/knowledge", summary="知识库摘要")
async def get_knowledge(request: Request):
    """返回产品知识库的加载状态和 System Prompt 摘要"""
    loader = _get_knowledge(request)

    if not loader.is_loaded:
        return {
            "loaded": False,
            "message": "Knowledge base not yet loaded. Call /api/pipeline/run to trigger loading.",
        }

    knowledge = await loader.load()
    return {
        "loaded": True,
        "product_name": knowledge.product_name,
        "features_count": len(knowledge.core_features),
        "barriers_count": len(knowledge.tech_barriers),
        "control_points_count": len(knowledge.control_points),
        "cases_count": len(knowledge.customer_cases),
        "keywords": knowledge.key_terms,
        "loaded_at": knowledge.loaded_at,
    }
