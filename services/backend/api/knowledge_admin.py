"""产品知识库管理 API - 管理员可访问。"""

from __future__ import annotations

import logging

from auth.deps import require_admin
from config import get_settings
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from knowledge_admin.catalog import KnowledgeCatalog
from knowledge_admin.preview import KnowledgePreviewService
from knowledge_admin.publication import ConflictError, KnowledgePublicationService
from knowledge_admin.repository import KnowledgeDraftRepository
from knowledge_admin.usage_classifier import UsageClassifier
from models.knowledge_management import (
    KnowledgeDraftCreate,
    KnowledgeDraftResponse,
    KnowledgeDraftUpdate,
    KnowledgePublicationCreate,
    KnowledgeRollbackRequest,
    KnowledgeValidationResult,
    KnowledgeValidationStatus,
)

logger = logging.getLogger("backend.api.knowledge_admin")

router = APIRouter(prefix="/api/admin/knowledge", tags=["knowledge-admin"])


def _get_catalog() -> KnowledgeCatalog:
    return KnowledgeCatalog(root_dir=get_settings().KNOWLEDGE_BASE_DIR)


def _get_repo(request: Request) -> KnowledgeDraftRepository:
    db = request.app.state.db
    return KnowledgeDraftRepository(db)


@router.post("/drafts", summary="创建草稿")
async def create_draft(
    request: Request,
    body: KnowledgeDraftCreate,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """从正式文件创建草稿。"""
    user_id, _ = user_id_and_doc
    catalog = _get_catalog()

    # Resolve document_id to relative_path
    relative_path = catalog.resolve_document_id(body.document_id)
    if relative_path is None:
        raise HTTPException(status_code=404, detail="文档不存在")

    # Check if file is editable
    if not UsageClassifier.is_editable(relative_path):
        raise HTTPException(status_code=403, detail="该文件不允许编辑")

    # Read current content
    try:
        doc = catalog.get_document(relative_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Verify base hash
    if doc["content_hash"] != body.base_content_hash:
        raise HTTPException(status_code=409, detail="文件已被修改，请重新载入")

    repo = _get_repo(request)
    draft = await repo.create_draft(
        relative_path=relative_path,
        base_content_hash=body.base_content_hash,
        content_md=doc["content"],
        user_id=user_id,
    )

    return {
        "ok": True,
        "data": KnowledgeDraftResponse(
            draft_id=draft.draft_id,
            document_id=draft.document_id,
            relative_path=draft.relative_path,
            base_content_hash=draft.base_content_hash,
            content_md=draft.content_md,
            status=draft.status,
            validation=draft.validation,
            change_summary=draft.change_summary,
            created_by=draft.created_by,
            updated_by=draft.updated_by,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        ),
    }


@router.get("/drafts/{draft_id}", summary="获取草稿")
async def get_draft(
    draft_id: str,
    request: Request,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """获取草稿详情。"""
    repo = _get_repo(request)
    draft = await repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")

    # Also fetch current formal content for diff
    catalog = _get_catalog()
    try:
        formal_doc = catalog.get_document(draft.relative_path)
        formal_content = formal_doc["content"]
        formal_hash = formal_doc["content_hash"]
    except ValueError:
        formal_content = ""
        formal_hash = ""

    return {
        "ok": True,
        "data": {
            "draft": KnowledgeDraftResponse(
                draft_id=draft.draft_id,
                document_id=draft.document_id,
                relative_path=draft.relative_path,
                base_content_hash=draft.base_content_hash,
                content_md=draft.content_md,
                status=draft.status,
                validation=draft.validation,
                change_summary=draft.change_summary,
                created_by=draft.created_by,
                updated_by=draft.updated_by,
                created_at=draft.created_at,
                updated_at=draft.updated_at,
            ),
            "formal_content": formal_content,
            "formal_hash": formal_hash,
        },
    }


@router.put("/drafts/{draft_id}", summary="保存草稿")
async def update_draft(
    draft_id: str,
    body: KnowledgeDraftUpdate,
    request: Request,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """保存草稿内容。"""
    user_id, _ = user_id_and_doc
    repo = _get_repo(request)

    # Validate content
    if not body.content_md or not body.content_md.strip():
        raise HTTPException(status_code=400, detail="草稿内容不能为空")

    # Check if file is a protected path and content has at least one heading
    draft = await repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")

    if UsageClassifier.is_protected_path(draft.relative_path) and not any(
        line.strip().startswith("#") for line in body.content_md.split("\n")
    ):
        raise HTTPException(status_code=422, detail="核心文件必须包含至少一个 Markdown 标题")

    updated = await repo.update_draft(draft_id, body.content_md, user_id, body.change_summary)
    if updated is None:
        raise HTTPException(status_code=404, detail="草稿不存在或已发布")

    return {
        "ok": True,
        "data": KnowledgeDraftResponse(
            draft_id=updated.draft_id,
            document_id=updated.document_id,
            relative_path=updated.relative_path,
            base_content_hash=updated.base_content_hash,
            content_md=updated.content_md,
            status=updated.status,
            validation=updated.validation,
            change_summary=updated.change_summary,
            created_by=updated.created_by,
            updated_by=updated.updated_by,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
        ),
    }


@router.delete("/drafts/{draft_id}", summary="放弃草稿")
async def delete_draft(
    draft_id: str,
    request: Request,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """放弃草稿（不删除正式文件）。"""
    user_id, _ = user_id_and_doc
    repo = _get_repo(request)
    deleted = await repo.delete_draft(draft_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="草稿不存在或已发布")
    return {"ok": True, "data": {"deleted": True}}


@router.get("/drafts", summary="列出草稿")
async def list_drafts(
    request: Request,
    relative_path: str | None = None,
    status: str | None = None,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """列出草稿。"""
    repo = _get_repo(request)
    drafts = await repo.list_drafts(relative_path=relative_path, status=status)
    return {
        "ok": True,
        "data": [
            KnowledgeDraftResponse(
                draft_id=d.draft_id,
                document_id=d.document_id,
                relative_path=d.relative_path,
                base_content_hash=d.base_content_hash,
                content_md=d.content_md,
                status=d.status,
                validation=d.validation,
                change_summary=d.change_summary,
                created_by=d.created_by,
                updated_by=d.updated_by,
                created_at=d.created_at,
                updated_at=d.updated_at,
            )
            for d in drafts
        ],
    }


def _get_preview_service() -> KnowledgePreviewService:
    return KnowledgePreviewService(root_dir=get_settings().KNOWLEDGE_BASE_DIR)


@router.post("/drafts/{draft_id}/validate", summary="校验草稿")
async def validate_draft(
    draft_id: str,
    request: Request,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """校验草稿内容并更新校验状态。"""
    repo = _get_repo(request)
    draft = await repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")

    preview_service = _get_preview_service()
    result = await preview_service.validate_draft(draft.relative_path, draft.content_md)

    # Update draft validation in repository
    validation = KnowledgeValidationResult(
        status=KnowledgeValidationStatus.PASSED
        if result["status"] == "passed"
        else KnowledgeValidationStatus.FAILED,
        errors=result["errors"],
        warnings=result["warnings"],
    )
    await repo.update_validation(draft_id, validation)

    return {"ok": True, "data": result}


@router.post("/drafts/{draft_id}/preview-prompt", summary="预览打分 Prompt")
async def preview_prompt(
    draft_id: str,
    request: Request,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """预览新旧打分 Prompt 对比。"""
    repo = _get_repo(request)
    draft = await repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")

    preview_service = _get_preview_service()
    result = await preview_service.preview_prompt(draft.relative_path, draft.content_md)

    return {"ok": True, "data": result}


@router.post("/drafts/{draft_id}/preview-score", summary="试打分对比")
async def preview_score(
    draft_id: str,
    request: Request,
    body: dict = Body(...),
    user_id_and_doc: tuple = Depends(require_admin),
):
    """试打分对比新旧知识下的评分差异。"""
    repo = _get_repo(request)
    draft = await repo.get_draft(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")

    article = body.get("article", {})
    preview_service = _get_preview_service()
    result = await preview_service.preview_score(draft.relative_path, draft.content_md, article)

    return {"ok": True, "data": result}


def _get_publication_service(request: Request) -> KnowledgePublicationService:
    return KnowledgePublicationService(
        db=request.app.state.db,
        root_dir=get_settings().KNOWLEDGE_BASE_DIR,
    )


@router.post("/publications", summary="发布草稿到正式知识库")
async def publish(
    request: Request,
    body: KnowledgePublicationCreate,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """将草稿内容发布到正式知识库文件。

    发布完成后自动刷新当前进程的知识缓存。
    """
    user_id, _ = user_id_and_doc
    publication_service = _get_publication_service(request)

    try:
        result = await publication_service.publish(
            draft_ids=body.draft_ids,
            version_name=body.version_name,
            release_notes=body.release_notes,
            user_id=user_id,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Refresh API process knowledge
    from agent.knowledge_runtime import KnowledgeRuntimeRefresher

    refresher = KnowledgeRuntimeRefresher(request.app.state)
    await refresher.refresh_if_changed()

    return {"ok": True, "data": result}


@router.get("/publications", summary="发布历史列表")
async def list_publications(
    request: Request,
    limit: int = 20,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """列出发布历史。"""
    publication_service = _get_publication_service(request)
    docs = await publication_service.list_publications(limit=limit)
    return {"ok": True, "data": docs}


@router.get("/publications/{publication_id}", summary="发布详情")
async def get_publication(
    publication_id: str,
    request: Request,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """获取发布详情，包含修订记录。"""
    publication_service = _get_publication_service(request)
    result = await publication_service.get_publication(publication_id)
    if result is None:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    return {"ok": True, "data": result}


@router.post("/publications/{publication_id}/rollback", summary="回滚发布")
async def rollback_publication(
    publication_id: str,
    request: Request,
    body: KnowledgeRollbackRequest,
    user_id_and_doc: tuple = Depends(require_admin),
):
    """回滚指定发布，恢复文件到发布前状态。"""
    user_id, _ = user_id_and_doc
    publication_service = _get_publication_service(request)

    try:
        result = await publication_service.rollback(
            publication_id=publication_id,
            reason=body.reason,
            user_id=user_id,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Refresh API process knowledge
    from agent.knowledge_runtime import KnowledgeRuntimeRefresher

    refresher = KnowledgeRuntimeRefresher(request.app.state)
    await refresher.refresh_if_changed()

    return {"ok": True, "data": result}
