"""我的稿件库 API — 用户独立管理的稿件(md 文档)。

用于 Agent 聊天链路的「保存生成稿 / 选稿件来对话改稿」：
  POST   /api/manuscripts                保存一份稿件（标题+markdown 正文）
  GET    /api/manuscripts                列出当前用户的稿件
  GET    /api/manuscripts/{id}           稿件详情（含正文）
  GET    /api/manuscripts/{id}/download  下载为 .md 文件
  DELETE /api/manuscripts/{id}           删除稿件

稿件存 mongodb `user_manuscripts` 集合，按 user_id 隔离；也可作为对话改稿的附件上下文。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from auth.deps import AuthError, get_current_user
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.api.manuscripts")

router = APIRouter(prefix="/api/manuscripts", tags=["Manuscripts"])

COLLECTION = "user_manuscripts"
MAX_CONTENT = 500_000  # 单份稿件最长正文（字符），避免超大文档


class ManuscriptError(AuthError):
    """稿件相关错误，走统一错误处理。"""


class SaveManuscriptRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content_md: str = Field(...)
    source: str = "manual"  # manual(手动/上传) | agent_generate(Agent 生成) | ...
    news_title: str | None = Field(default=None, max_length=500)  # 对应的新闻题目（可选）


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库暂不可用")
    return db


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _brief(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "manuscript_id": doc["_id"],
        "title": doc.get("title", ""),
        "source": doc.get("source", "manual"),
        "news_title": doc.get("news_title") or None,
        "content_length": len(doc.get("content_md", "")),
        "created_at": doc.get("created_at", ""),
        "updated_at": doc.get("updated_at", ""),
    }


def _detail(doc: dict[str, Any]) -> dict[str, Any]:
    data = _brief(doc)
    data["content_md"] = doc.get("content_md", "")
    return data


@router.post("/", summary="保存一份稿件到稿件库")
async def create_manuscript(
    body: SaveManuscriptRequest,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    db = _get_db(request)
    content_md = body.content_md.strip()
    if not content_md:
        raise HTTPException(status_code=422, detail="稿件正文不能为空")
    if len(content_md) > MAX_CONTENT:
        raise HTTPException(status_code=413, detail=f"稿件正文过长（最多 {MAX_CONTENT} 字符）")

    manuscript_id = uuid4().hex[:16]
    now = _now()
    doc = {
        "_id": manuscript_id,
        "user_id": user_id,
        "title": body.title.strip(),
        "content_md": content_md,
        "source": body.source,
        "news_title": (body.news_title or "").strip() or None,
        "created_at": now,
        "updated_at": now,
    }
    await db[COLLECTION].insert_one(doc)
    return {"ok": True, "data": _detail(doc)}


@router.get("/", summary="列出当前用户稿件库")
async def list_manuscripts(
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    db = _get_db(request)
    cursor = db[COLLECTION].find({"user_id": user_id}).sort("updated_at", -1).limit(500)
    items = [_brief(d) async for d in cursor]
    return {"items": items, "total": len(items)}


@router.get("/{manuscript_id}", summary="稿件详情")
async def get_manuscript(
    manuscript_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    db = _get_db(request)
    doc = await db[COLLECTION].find_one({"_id": manuscript_id, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="稿件不存在")
    return {"ok": True, "data": _detail(doc)}


@router.get("/{manuscript_id}/download", summary="下载稿件为 .md 文件")
async def download_manuscript(
    manuscript_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> Response:
    db = _get_db(request)
    doc = await db[COLLECTION].find_one({"_id": manuscript_id, "user_id": user_id})
    if doc is None:
        raise HTTPException(status_code=404, detail="稿件不存在")
    filename = quote((doc.get("title") or "稿件").strip() or "稿件") + ".md"
    return Response(
        content=(doc.get("content_md") or ""),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.delete("/{manuscript_id}", summary="删除稿件")
async def delete_manuscript(
    manuscript_id: str,
    request: Request,
    user_id: str = Depends(get_current_user),
) -> dict[str, Any]:
    db = _get_db(request)
    res = await db[COLLECTION].delete_one({"_id": manuscript_id, "user_id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="稿件不存在")
    return {"ok": True}