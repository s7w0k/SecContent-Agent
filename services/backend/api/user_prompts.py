"""Authenticated APIs for per-user prompt overrides."""

from __future__ import annotations

from datetime import UTC, datetime

from agent.draft_generator import SYSTEM_PROMPT_TEMPLATE
from auth.deps import AuthError, get_current_user
from fastapi import APIRouter, Depends, Request
from models.user_prompt import EffectivePrompt, UserPromptUpdate

router = APIRouter(prefix="/api/user-prompts", tags=["User Prompts"])

DRAFT_SYSTEM_PROMPT_KEY = "draft_system"
PROMPT_DEFAULTS = {DRAFT_SYSTEM_PROMPT_KEY: SYSTEM_PROMPT_TEMPLATE}
# Keep this list aligned with DraftGenerator._build_system_prompt().
PROMPT_PLACEHOLDERS = {
    DRAFT_SYSTEM_PROMPT_KEY: ("knowledge_context", "template_spec", "style_hints")
}


class UserPromptError(AuthError):
    """Prompt error rendered through the API's unified error handler."""


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise UserPromptError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    return db


def _prompt_definition(prompt_key: str) -> tuple[str, tuple[str, ...]]:
    try:
        return PROMPT_DEFAULTS[prompt_key], PROMPT_PLACEHOLDERS[prompt_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported prompt key: {prompt_key}") from exc


async def get_effective_prompt(db, user_id: str, prompt_key: str) -> EffectivePrompt:
    """Return the user's override when present, otherwise the system default."""
    default_content, required_placeholders = _prompt_definition(prompt_key)
    document = await db["user_prompts"].find_one({"user_id": user_id, "prompt_key": prompt_key})
    if document is None:
        return EffectivePrompt(
            prompt_key=prompt_key,
            content=default_content,
            is_custom=False,
            required_placeholders=list(required_placeholders),
        )
    return EffectivePrompt(
        prompt_key=prompt_key,
        content=document["content"],
        is_custom=True,
        required_placeholders=list(required_placeholders),
        updated_at=document.get("updated_at"),
    )


def _response(prompt: EffectivePrompt) -> dict:
    return {"ok": True, "data": prompt.model_dump()}


@router.get("/draft-system", summary="获取生效的初稿 System Prompt")
async def get_draft_system_prompt(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    prompt = await get_effective_prompt(_get_db(request), user_id, DRAFT_SYSTEM_PROMPT_KEY)
    return _response(prompt)


@router.put("/draft-system", summary="保存自定义初稿 System Prompt")
async def save_draft_system_prompt(
    body: UserPromptUpdate,
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    _, required_placeholders = _prompt_definition(DRAFT_SYSTEM_PROMPT_KEY)
    missing = [name for name in required_placeholders if f"{{{name}}}" not in body.content]
    if missing:
        rendered = ", ".join(f"{{{name}}}" for name in missing)
        raise UserPromptError(
            422,
            "MISSING_PLACEHOLDER",
            f"提示词缺少必需占位符: {rendered}",
        )

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


@router.post("/draft-system/reset", summary="恢复系统默认初稿 System Prompt")
async def reset_draft_system_prompt(
    request: Request,
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    await db["user_prompts"].delete_one({"user_id": user_id, "prompt_key": DRAFT_SYSTEM_PROMPT_KEY})
    return _response(await get_effective_prompt(db, user_id, DRAFT_SYSTEM_PROMPT_KEY))
