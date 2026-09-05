"""产品知识库目录查询 API - 所有登录用户可访问。"""

from __future__ import annotations

import logging
from pathlib import Path

from auth.deps import get_current_user
from config import get_settings
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from knowledge_admin.catalog import KnowledgeCatalog
from knowledge_admin.usage_classifier import UsageClassifier

logger = logging.getLogger("backend.api.knowledge_catalog")

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _get_catalog() -> KnowledgeCatalog:
    settings = get_settings()
    return KnowledgeCatalog(root_dir=settings.KNOWLEDGE_BASE_DIR)


def _get_root_dir() -> Path:
    return Path(get_settings().KNOWLEDGE_BASE_DIR)


@router.get("/tree", summary="获取知识库目录树")
async def get_knowledge_tree(
    include_empty: bool = Query(True, description="是否返回空目录"),
    include_raw: bool = Query(True, description="是否返回原始文档节点"),
    _user_id: str = Depends(get_current_user),
):
    """返回真实文件系统目录树。"""
    catalog = _get_catalog()
    tree = catalog.build_tree(include_empty=include_empty, include_raw=include_raw)

    # 附加 Loader 状态
    loader = _get_loader_from_app()
    if loader:
        tree["knowledge_hash"] = loader._last_hash or ""
        tree["loaded_at"] = loader.loaded_at.isoformat() if loader.loaded_at else ""
    else:
        tree["knowledge_hash"] = ""
        tree["loaded_at"] = ""

    return {"ok": True, "data": tree}


@router.get("/documents", summary="获取知识库文档")
async def get_knowledge_document(
    request: Request,
    path: str = Query(..., description="文档相对路径"),
    _user_id: str = Depends(get_current_user),
):
    """返回正式 Markdown 文档及其元数据。"""
    catalog = _get_catalog()

    try:
        doc = catalog.get_document(path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    root_dir = _get_root_dir()
    metadata = UsageClassifier.get_file_metadata(
        path,
        doc["content"],
        root_dir=root_dir,
    )

    return {"ok": True, "data": {**doc, **metadata}}


@router.get("/search", summary="搜索知识库")
async def search_knowledge(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    role: str | None = Query(None, description="按用途过滤"),
    direct_scoring_prompt: bool | None = Query(None, description="按是否直接打分过滤"),
    _user_id: str = Depends(get_current_user),
):
    """按文件名和正文子串搜索知识库。"""
    catalog = _get_catalog()
    results = catalog.search(q)

    enriched: list[dict] = []
    for item in results:
        rel_path = item["relative_path"]
        classifier = UsageClassifier
        file_role = classifier.classify(rel_path)
        is_direct = classifier.is_direct_scoring_prompt(rel_path)

        if role is not None and file_role != role:
            continue
        if direct_scoring_prompt is not None and is_direct != direct_scoring_prompt:
            continue

        enriched.append(
            {
                **item,
                "knowledge_role": file_role,
                "direct_scoring_prompt": is_direct,
                "document_id": classifier.get_document_id(rel_path),
            }
        )

    return {"ok": True, "data": enriched}


@router.get("/status", summary="获取知识库加载状态")
async def get_knowledge_status(
    _user_id: str = Depends(get_current_user),
):
    """返回 Loader 状态和知识库摘要。"""
    catalog = _get_catalog()
    root_dir = _get_root_dir()
    loader = _get_loader_from_app()

    file_count = catalog.count_files()

    # 统计 loader_relevant 数量
    loader_relevant_count = 0
    direct_scoring_count = 0
    for md_file in sorted(root_dir.rglob("*.md")):
        if ".git" in md_file.parts:
            continue
        rel_path = str(md_file.relative_to(root_dir)).replace("\\", "/")
        if UsageClassifier.is_loader_relevant(root_dir, rel_path):
            loader_relevant_count += 1
        if UsageClassifier.is_direct_scoring_prompt(rel_path):
            direct_scoring_count += 1

    data = {
        "root_path": "/app/documents",
        "loaded": loader is not None and loader.is_loaded,
        "file_count": file_count,
        "loader_relevant_count": loader_relevant_count,
        "direct_scoring_file_count": direct_scoring_count,
        "knowledge_hash": loader._last_hash if loader else "",
        "loaded_at": loader.loaded_at.isoformat() if loader and loader.loaded_at else "",
    }

    return {"ok": True, "data": data}


@router.get("/usage-map", summary="获取知识库用途分类说明")
async def get_usage_map(
    _user_id: str = Depends(get_current_user),
):
    """返回知识分层和用途说明，供前端绘制图例。"""
    return {"ok": True, "data": UsageClassifier.get_usage_legend()}


def _get_loader_from_app():
    """从全局获取 KnowledgeLoader（API 进程中可用）。"""
    try:
        from main import app

        return getattr(app.state, "knowledge_loader", None)
    except (ImportError, AttributeError):
        return None
