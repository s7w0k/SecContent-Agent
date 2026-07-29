"""用户注册、登录、当前用户查询与账号注销 API。"""

from __future__ import annotations

from api.logs import generate_trace_id, log_pipeline
from auth.deps import AuthError, get_current_user
from auth.jwt import create_access_token
from config import get_settings
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from logging_config import get_audit_logger
from models.user import (
    AccountDelete,
    TokenResponse,
    UserCreate,
    UserInDB,
    UserLogin,
    UserPublic,
)
from passlib.context import CryptContext
from pymongo.errors import DuplicateKeyError

router = APIRouter(prefix="/api/auth", tags=["Auth"])
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

PRIVATE_USER_COLLECTIONS = (
    "feedbacks",
    "user_activities",
    "user_profiles",
    "user_drafts",
    "chat_sessions",
    # ── Web 搜索 (SearXNG) ──────────────────────────
    "search_sessions",
    "search_import_batches",
    "search_import_items",
    "pipeline_tasks",
    "pipeline_logs",
    "user_pr_templates",
    "user_pr_template_versions",
    "user_prompts",
    # ── 用户记忆与个性化 ──────────────────────────
    "user_profile_policies",
    "user_memory_events",
    "user_memory_items",
    "user_memory_summaries",
    "generation_runs",
    "personalization_feedbacks",
)


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


def _public_user(user: UserInDB | dict) -> UserPublic:
    if isinstance(user, UserInDB):
        data = user.model_dump()
    else:
        data = dict(user)
        data.pop("_id", None)
    return UserPublic.model_validate(data)


def _public_payload(user: UserInDB | dict) -> dict:
    return _public_user(user).model_dump(mode="json")


def hash_password(password: str) -> str:
    """使用 cost factor 12 的 bcrypt 哈希密码。"""
    return password_context.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """安全校验密码，损坏的哈希按校验失败处理。"""
    try:
        return password_context.verify(password, hashed_password)
    except (TypeError, ValueError):
        return False


def _credentials_error() -> AuthError:
    return AuthError(401, "INVALID_CREDENTIALS", "用户名或密码错误")


@router.post("/register", summary="注册用户")
async def register_user(body: UserCreate, request: Request):
    db = _get_db(request)
    users = db["users"]
    if await users.find_one({"username": body.username}) is not None:
        raise AuthError(409, "USERNAME_EXISTS", "用户名已被占用")

    user = UserInDB(
        username=body.username,
        email=body.email,
        display_name=body.display_name or body.username,
        hashed_password=hash_password(body.password),
    )
    document = user.model_dump(exclude={"id"})
    try:
        await users.insert_one(document)
    except DuplicateKeyError as exc:
        raise AuthError(409, "USERNAME_EXISTS", "用户名已被占用") from exc
    trace_id = generate_trace_id()
    await log_pipeline(
        db,
        "INFO",
        "auth",
        "user registered",
        user_id=user.user_id,
        username=user.username,
        trace_id=trace_id,
        action="register",
        detail={"has_email": bool(user.email)},
    )
    get_audit_logger().log(
        user_id=user.user_id,
        action="register",
    )
    return {"ok": True, "data": _public_payload(user), "trace_id": trace_id}


@router.post("/login", summary="用户登录")
async def login_user(body: UserLogin, request: Request):
    db = _get_db(request)
    document = await db["users"].find_one({"username": body.username})
    if document is None or not verify_password(body.password, document.get("hashed_password", "")):
        raise _credentials_error()
    if not document.get("is_active", True):
        raise AuthError(403, "ACCOUNT_DISABLED", "账号已被禁用")

    token = create_access_token(document["user_id"], document["username"])
    data = TokenResponse(
        access_token=token,
        expires_in=get_settings().JWT_EXPIRE_HOURS * 3600,
        user=_public_user(document),
    )
    trace_id = generate_trace_id()
    await log_pipeline(
        db,
        "INFO",
        "auth",
        "user logged in",
        user_id=document["user_id"],
        username=document["username"],
        trace_id=trace_id,
        action="login",
    )
    get_audit_logger().log(
        user_id=document["user_id"],
        action="login",
    )
    return {"ok": True, "data": data.model_dump(mode="json"), "trace_id": trace_id}


@router.get("/me", summary="获取当前用户")
async def get_me(request: Request, user_id: str = Depends(get_current_user)):
    db = _get_db(request)
    document = await db["users"].find_one({"user_id": user_id})
    if document is None:
        raise AuthError(401, "NOT_AUTHENTICATED", "用户不存在或已注销")
    if not document.get("is_active", True):
        raise AuthError(403, "ACCOUNT_DISABLED", "账号已被禁用")
    return {"ok": True, "data": _public_payload(document)}


@router.delete("/account", summary="注销当前账号")
async def delete_account(
    request: Request,
    body: AccountDelete | None = Body(default=None),
    user_id: str = Depends(get_current_user),
):
    db = _get_db(request)
    users = db["users"]
    document = await users.find_one({"user_id": user_id})
    if document is None:
        raise AuthError(401, "NOT_AUTHENTICATED", "用户不存在或已注销")
    if (
        body
        and body.password
        and not verify_password(
            body.password,
            document.get("hashed_password", ""),
        )
    ):
        raise _credentials_error()

    for collection_name in PRIVATE_USER_COLLECTIONS:
        collection = db[collection_name]
        if collection_name == "user_profiles":
            await collection.delete_one({"user_id": user_id})
        else:
            await collection.delete_many({"user_id": user_id})
    trace_id = generate_trace_id()
    await log_pipeline(
        db,
        "INFO",
        "auth",
        "user account deleted",
        user_id=user_id,
        username=document.get("username", user_id),
        trace_id=trace_id,
        action="logout",
        detail={"account_deleted": True},
    )
    await users.delete_one({"user_id": user_id})
    get_audit_logger().log(
        user_id=user_id,
        action="account_delete",
    )
    return {
        "ok": True,
        "data": {"message": "账号已注销，所有数据已删除", "trace_id": trace_id},
    }
