"""Authenticated tenant-scoped PR template management API."""

from __future__ import annotations

from typing import Any

from agent.template_repository import (
    TemplateNotFoundError,
    TemplateRepository,
    TemplateVersionConflictError,
)
from api.logs import generate_trace_id, log_pipeline
from auth.deps import AuthError, get_current_user
from fastapi import APIRouter, Depends, Path, Query, Request
from logging_config import get_audit_logger
from models.pr_template import PRTemplateCategory, UserPRTemplateUpdate

router = APIRouter(prefix="/api/pr-templates", tags=["PR Templates"])


def _runtime(request: Request) -> tuple[Any, TemplateRepository]:
    db = getattr(request.app.state, "db", None)
    repository = getattr(request.app.state, "template_repository", None)
    if db is None or repository is None:
        raise AuthError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    return db, repository


def _not_found(exc: TemplateNotFoundError) -> AuthError:
    return AuthError(404, "TEMPLATE_NOT_FOUND", str(exc))


def _conflict(exc: TemplateVersionConflictError) -> AuthError:
    return AuthError(409, "TEMPLATE_VERSION_CONFLICT", str(exc))


def _preview_markdown(body: UserPRTemplateUpdate) -> str:
    lines = [body.title_template]
    for section in body.sections:
        lines.extend(["", f"## {section.heading}", "", f"[{section.guide}]"])
    if body.extra_instructions:
        lines.extend(["", f"> 补充要求：{body.extra_instructions}"])
    return "\n".join(lines)


async def _log_mutation(
    request: Request,
    db: Any,
    *,
    user_id: str,
    action: str,
    template_key: str,
    template_id: str,
    old_version: int | None,
    new_version: int,
    source: str,
    trace_id: str,
) -> None:
    detail = {
        "template_key": template_key,
        "template_id": template_id,
        "old_version": old_version,
        "new_version": new_version,
        "source": source,
    }
    await log_pipeline(
        db,
        "INFO",
        "pr_template",
        f"PR template {action}",
        user_id=user_id,
        username=getattr(request.state, "username", None) or user_id,
        trace_id=trace_id,
        action=action,
        detail=detail,
    )
    get_audit_logger().log(
        user_id=user_id,
        action=f"pr_template_{action}",
        resource=template_key,
        detail={**detail, "trace_id": trace_id},
    )


@router.get("", summary="查询当前用户的有效 PR 模板")
async def list_pr_templates(
    request: Request,
    category_v2: PRTemplateCategory | None = Query(default=None),
    user_id: str = Depends(get_current_user),
):
    _, repository = _runtime(request)
    items = await repository.list_effective_templates(
        user_id,
        category_v2.value if category_v2 else None,
    )
    return {"ok": True, "data": {"items": items, "total": len(items)}}


@router.get("/{template_key}", summary="查询单个有效 PR 模板")
async def get_pr_template(
    template_key: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    _, repository = _runtime(request)
    try:
        template = await repository.get_effective(user_id, template_key)
    except TemplateNotFoundError as exc:
        raise _not_found(exc) from exc
    return {"ok": True, "data": template}


@router.put("/{template_key}", summary="保存当前用户的 PR 模板覆盖")
async def save_pr_template(
    template_key: str,
    body: UserPRTemplateUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db, repository = _runtime(request)
    try:
        current = await repository.get_override(user_id, template_key)
        template = await repository.save_override(user_id, template_key, body)
    except TemplateNotFoundError as exc:
        raise _not_found(exc) from exc
    except TemplateVersionConflictError as exc:
        raise _conflict(exc) from exc

    trace_id = generate_trace_id()
    await _log_mutation(
        request,
        db,
        user_id=user_id,
        action="update",
        template_key=template_key,
        template_id=template.template_id,
        old_version=current.version if current else None,
        new_version=template.version,
        source=template.source,
        trace_id=trace_id,
    )
    return {"ok": True, "data": template, "trace_id": trace_id}


@router.post("/{template_key}/reset", summary="恢复系统默认 PR 模板")
async def reset_pr_template(
    template_key: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db, repository = _runtime(request)
    try:
        current = await repository.get_override(user_id, template_key)
        template = await repository.reset_override(user_id, template_key)
    except TemplateNotFoundError as exc:
        raise _not_found(exc) from exc
    except TemplateVersionConflictError as exc:
        raise _conflict(exc) from exc

    trace_id = generate_trace_id()
    await _log_mutation(
        request,
        db,
        user_id=user_id,
        action="reset",
        template_key=template_key,
        template_id=current.template_id if current else template.template_id,
        old_version=current.version if current else None,
        new_version=template.version,
        source=template.source,
        trace_id=trace_id,
    )
    return {"ok": True, "data": template, "trace_id": trace_id}


@router.post("/{template_key}/preview", summary="预览 PR 模板 Markdown 骨架")
async def preview_pr_template(
    template_key: str,
    body: UserPRTemplateUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    _, repository = _runtime(request)
    try:
        await repository.get_effective(user_id, template_key)
    except TemplateNotFoundError as exc:
        raise _not_found(exc) from exc
    return {"ok": True, "data": {"content_md": _preview_markdown(body)}}


@router.get("/{template_key}/versions", summary="查询 PR 模板历史版本")
async def list_pr_template_versions(
    template_key: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user_id: str = Depends(get_current_user),
):
    _, repository = _runtime(request)
    try:
        items = await repository.list_versions(
            user_id,
            template_key,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        total = await repository.count_versions(user_id, template_key)
    except TemplateNotFoundError as exc:
        raise _not_found(exc) from exc
    return {
        "ok": True,
        "data": {"items": items, "total": total, "page": page, "page_size": page_size},
    }


@router.post(
    "/{template_key}/versions/{version}/restore",
    summary="将历史快照恢复为 PR 模板新版本",
)
async def restore_pr_template_version(
    template_key: str,
    request: Request,
    version: int = Path(ge=1),
    user_id: str = Depends(get_current_user),
):
    db, repository = _runtime(request)
    try:
        current = await repository.get_override(user_id, template_key)
        template = await repository.restore(user_id, template_key, version)
    except TemplateNotFoundError as exc:
        raise _not_found(exc) from exc
    except TemplateVersionConflictError as exc:
        raise _conflict(exc) from exc

    trace_id = generate_trace_id()
    await _log_mutation(
        request,
        db,
        user_id=user_id,
        action="restore",
        template_key=template_key,
        template_id=template.template_id,
        old_version=current.version if current else None,
        new_version=template.version,
        source=template.source,
        trace_id=trace_id,
    )
    return {"ok": True, "data": template, "trace_id": trace_id}
