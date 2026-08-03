"""通用用户提示词 API - 列表、详情、校验、保存、重置、历史、回滚、预览。

旧 `/draft-system` 端点继续可用，内部通过兼容映射工作。
新 `/api/user-prompts` 通用端点支持所有注册的提示词类型。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agent.draft_generator import SYSTEM_PROMPT_TEMPLATE
from agent.prompt_composer import compose_prompt
from agent.prompt_registry import get_registry, resolve_prompt_key
from agent.prompt_resolver import PromptResolver
from auth.deps import AuthError, get_current_user
from fastapi import APIRouter, Depends, Query, Request
from models.user_prompt import EffectivePrompt, UserPromptUpdate
from pydantic import BaseModel

logger = logging.getLogger("backend.api.user_prompts")

router = APIRouter(prefix="/api/user-prompts", tags=["User Prompts"])

DRAFT_SYSTEM_PROMPT_KEY = "draft_system"

# 旧 draft_system 的默认内容和占位符（保持兼容）
PROMPT_DEFAULTS = {DRAFT_SYSTEM_PROMPT_KEY: SYSTEM_PROMPT_TEMPLATE}
PROMPT_PLACEHOLDERS = {
    DRAFT_SYSTEM_PROMPT_KEY: ("knowledge_context", "template_spec", "style_hints")
}


class UserPromptError(AuthError):
    """Prompt error rendered through the API's unified error handler."""


class PromptValidateRequest(BaseModel):
    content: str


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise UserPromptError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    return db


def _get_resolver(request: Request) -> PromptResolver:
    resolver = getattr(request.app.state, "prompt_resolver", None)
    if resolver is None:
        resolver = PromptResolver(_get_db(request))
        request.app.state.prompt_resolver = resolver
    return resolver


# ── 旧兼容端点 ───────────────────────────────────────────


@router.get("/draft-system", summary="获取生效的初稿 System Prompt（兼容端点）")
async def get_draft_system_prompt(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """旧端点，保持向后兼容。返回完整 SYSTEM_PROMPT_TEMPLATE 作为默认。"""
    db = _get_db(request)
    default_content = PROMPT_DEFAULTS[DRAFT_SYSTEM_PROMPT_KEY]
    required_placeholders = PROMPT_PLACEHOLDERS[DRAFT_SYSTEM_PROMPT_KEY]

    document = await db["user_prompts"].find_one(
        {"user_id": user_id, "prompt_key": DRAFT_SYSTEM_PROMPT_KEY}
    )
    if document is None:
        return _response(
            EffectivePrompt(
                prompt_key=DRAFT_SYSTEM_PROMPT_KEY,
                content=default_content,
                is_custom=False,
                required_placeholders=list(required_placeholders),
            )
        )
    return _response(
        EffectivePrompt(
            prompt_key=DRAFT_SYSTEM_PROMPT_KEY,
            content=document["content"],
            is_custom=True,
            required_placeholders=list(required_placeholders),
            updated_at=document.get("updated_at"),
        )
    )


@router.put("/draft-system", summary="保存自定义初稿 System Prompt（兼容端点）")
async def save_draft_system_prompt(
    body: UserPromptUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """旧端点，保持向后兼容。"""
    db = _get_db(request)
    required_placeholders = PROMPT_PLACEHOLDERS[DRAFT_SYSTEM_PROMPT_KEY]
    missing = [name for name in required_placeholders if f"{{{name}}}" not in body.content]
    if missing:
        rendered = ", ".join(f"{{{name}}}" for name in missing)
        raise UserPromptError(422, "MISSING_PLACEHOLDER", f"提示词缺少必需占位符: {rendered}")

    now = datetime.now(UTC)
    await db["user_prompts"].update_one(
        {"user_id": user_id, "prompt_key": DRAFT_SYSTEM_PROMPT_KEY},
        {
            "$set": {"content": body.content, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return _response(
        EffectivePrompt(
            prompt_key=DRAFT_SYSTEM_PROMPT_KEY,
            content=body.content,
            is_custom=True,
            required_placeholders=list(required_placeholders),
            updated_at=now,
        )
    )


@router.post("/draft-system/reset", summary="恢复系统默认初稿 System Prompt（兼容端点）")
async def reset_draft_system_prompt(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """旧端点，保持向后兼容。"""
    db = _get_db(request)
    await db["user_prompts"].delete_one(
        {"user_id": user_id, "prompt_key": DRAFT_SYSTEM_PROMPT_KEY}
    )
    return await get_draft_system_prompt(request, user_id)


# ── 新通用端点 ───────────────────────────────────────────


@router.get("", summary="获取提示词目录")
async def list_prompts(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """列出所有可编辑提示词及其当前状态。"""
    registry = get_registry()
    resolver = _get_resolver(request)

    items = []
    for definition in registry.list_all():
        effective = await resolver.get_effective(user_id, definition.prompt_key)
        items.append({
            "prompt_key": definition.prompt_key,
            "display_name": definition.display_name,
            "stage": definition.stage,
            "description": definition.description,
            "source": effective.source,
            "version": effective.version,
            "default_version": definition.default_version,
            "is_custom": effective.is_custom,
            "updated_at": effective.updated_at.isoformat() if effective.updated_at else None,
        })

    return {"ok": True, "data": {"items": items}}


@router.get("/{prompt_key}", summary="获取单个生效提示词")
async def get_prompt(
    prompt_key: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """返回提示词内容、来源、版本、允许变量和必需变量。"""
    resolver = _get_resolver(request)
    try:
        effective = await resolver.get_effective(user_id, prompt_key)
    except KeyError:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}") from None
    return _response(effective)


@router.post("/{prompt_key}/validate", summary="校验提示词内容")
async def validate_prompt(
    prompt_key: str,
    body: PromptValidateRequest,
    user_id: str = Depends(get_current_user),
):
    """校验提示词内容，不保存。

    检查：缺失必需变量、未允许变量、花括号平衡、长度、风险提示。
    """
    registry = get_registry()
    resolved = resolve_prompt_key(prompt_key)
    definition = registry.get(resolved)
    if definition is None:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}")

    content = body.content
    errors: list[str] = []
    warnings: list[str] = []

    # 检查必需占位符
    missing = [
        name for name in definition.required_placeholders
        if f"{{{name}}}" not in content
    ]
    if missing:
        rendered = ", ".join(f"{{{name}}}" for name in missing)
        errors.append(f"缺少必需占位符: {rendered}")

    # 检查未允许的占位符
    import re
    found_placeholders = set(re.findall(r"\{(\w+)\}", content))
    unknown = found_placeholders - set(definition.allowed_placeholders)
    if unknown:
        warnings.append(f"发现未声明的占位符: {', '.join(sorted(unknown))}")

    # 检查花括号平衡
    if content.count("{") != content.count("}"):
        errors.append("花括号不平衡")

    # 检查长度
    if len(content) < definition.min_length:
        errors.append(f"内容长度 {len(content)} 低于最小要求 {definition.min_length}")
    if len(content) > definition.max_length:
        errors.append(f"内容长度 {len(content)} 超过最大限制 {definition.max_length}")

    # 风险提示
    risk_keywords = ["忽略系统", "ignore system", " disregard", "绕过安全", "override fixed"]
    for kw in risk_keywords:
        if kw.lower() in content.lower():
            warnings.append(f"疑似包含覆盖系统规则的表述: '{kw}'")

    # 预估 Token 数（粗略：1 个中文字符 ≈ 1.5 token）
    est_tokens = int(len(content) * 1.5)

    return {
        "ok": True,
        "data": {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "estimated_tokens": est_tokens,
            "char_count": len(content),
        },
    }


@router.put("/{prompt_key}", summary="保存提示词")
async def save_prompt(
    prompt_key: str,
    body: UserPromptUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """保存用户提示词覆盖，支持乐观锁。

    - 首次保存：创建用户版本 1
    - 再次保存：匹配 expected_version 后版本加 1
    - 不匹配返回 409 VERSION_CONFLICT
    """
    resolver = _get_resolver(request)
    try:
        effective = await resolver.save(
            user_id,
            prompt_key,
            body.content,
            expected_version=body.expected_version,
        )
    except ValueError as exc:
        msg = str(exc)
        if "VERSION_CONFLICT" in msg:
            raise UserPromptError(409, "PROMPT_VERSION_CONFLICT", msg) from exc
        if "MISSING_PLACEHOLDER" in msg:
            raise UserPromptError(422, "MISSING_PLACEHOLDER", msg) from exc
        if "CONTENT_TOO_SHORT" in msg or "CONTENT_TOO_LONG" in msg:
            raise UserPromptError(422, "INVALID_CONTENT_LENGTH", msg) from exc
        raise UserPromptError(400, "INVALID_PROMPT", msg) from exc
    except KeyError:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}") from None

    return _response(effective)


@router.post("/{prompt_key}/reset", summary="恢复系统默认")
async def reset_prompt(
    prompt_key: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """删除用户覆盖，保留历史，返回系统默认。"""
    resolver = _get_resolver(request)
    try:
        effective = await resolver.reset(user_id, prompt_key)
    except KeyError:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}") from None
    return _response(effective)


@router.get("/{prompt_key}/versions", summary="版本历史")
async def list_versions(
    prompt_key: str,
    request: Request,
    user_id: str = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
):
    """分页查询提示词历史版本。"""
    resolver = _get_resolver(request)
    try:
        result = await resolver.list_versions(
            user_id, prompt_key, page=page, page_size=page_size
        )
    except KeyError:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}") from None
    return {"ok": True, "data": result}


@router.post("/{prompt_key}/versions/{version}/restore", summary="回滚到历史版本")
async def restore_version(
    prompt_key: str,
    version: int,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """回滚到历史版本（创建新版本，不倒退版本号）。"""
    resolver = _get_resolver(request)
    try:
        effective = await resolver.restore_version(user_id, prompt_key, version)
    except KeyError:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}") from None
    except ValueError as exc:
        raise UserPromptError(404, "VERSION_NOT_FOUND", str(exc)) from exc
    return _response(effective)


@router.post("/{prompt_key}/preview", summary="预览组合后的提示词")
async def preview_prompt(
    prompt_key: str,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    """预览固定策略层和用户业务层的组合位置，不调用 LLM。

    使用脱敏示例数据，不返回真实文章或产品知识。
    """
    registry = get_registry()
    resolver = _get_resolver(request)

    resolved = resolve_prompt_key(prompt_key)
    definition = registry.get(resolved)
    if definition is None:
        raise UserPromptError(404, "PROMPT_NOT_FOUND", f"不支持的提示词键: {prompt_key}")

    effective = await resolver.get_effective(user_id, resolved)

    # 使用脱敏示例数据
    sample_contexts: dict[str, str] = {}
    for ph in definition.allowed_placeholders:
        sample_contexts[ph] = f"[示例{ph}数据 - 实际运行时由系统注入]"

    composed = compose_prompt(
        user_business_prompt=effective.content,
        readonly_contexts=sample_contexts,
        output_contract="[输出格式协议 - 由各Agent代码固定]",
    )

    return {
        "ok": True,
        "data": {
            "prompt_key": resolved,
            "source": effective.source,
            "version": effective.version,
            "composed_preview": composed,
            "layers": {
                "fixed_policy": "[固定策略层 - 不可编辑]",
                "user_business": effective.content[:200] + "..." if len(effective.content) > 200 else effective.content,
                "runtime_data": "[运行时数据层 - 系统注入]",
                "output_contract": "[输出协议 - 代码固定]",
            },
        },
    }


def _response(prompt: EffectivePrompt) -> dict:
    return {"ok": True, "data": prompt.model_dump()}
