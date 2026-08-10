"""Authenticated article upload and ingestion API."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from api.activity import log_activity
from auth.deps import AuthError, get_current_user
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from models.feedback import ActionType
from pymongo.errors import DuplicateKeyError
from utils.file_parser import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE,
    FileParseError,
    parse,
)

router = APIRouter(prefix="/api/upload", tags=["Upload"])
MIN_CONTENT_LENGTH = 50


class UploadError(AuthError):
    """Upload error rendered through the API's unified error handler."""


def _get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise UploadError(503, "DATABASE_UNAVAILABLE", "数据库暂不可用")
    return db


def _safe_filename(filename: str | None) -> str:
    normalized = (filename or "").replace("\\", "/")
    safe_name = Path(normalized).name.strip()
    if not safe_name or safe_name in {".", ".."}:
        raise UploadError(422, "INVALID_FILENAME", "文件名不能为空")
    return safe_name


def _raise_if_unsupported(filename: str) -> None:
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UploadError(
            422,
            "UNSUPPORTED_FILE_TYPE",
            f"不支持的文件类型，仅支持: {allowed}",
        )


@router.post("/article", summary="上传本地文章并入库")
async def upload_article(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    user_id: str = Depends(get_current_user),
):
    """Parse one local document and persist it as a pending article."""
    db = _get_db(request)
    filename = _safe_filename(file.filename)
    _raise_if_unsupported(filename)

    if file.size is not None and file.size > MAX_FILE_SIZE:
        raise UploadError(413, "FILE_TOO_LARGE", "文件不能超过 10MB")
    content_bytes = await file.read(MAX_FILE_SIZE + 1)
    if len(content_bytes) > MAX_FILE_SIZE:
        raise UploadError(413, "FILE_TOO_LARGE", "文件不能超过 10MB")

    try:
        content_md = parse(filename, content_bytes).strip()
    except FileParseError as exc:
        raise UploadError(422, "PARSE_FAILED", str(exc)) from exc
    finally:
        await file.close()

    if len(content_md) < MIN_CONTENT_LENGTH:
        raise UploadError(
            422,
            "EMPTY_CONTENT",
            "文件可能为扫描件或内容过少，请转换为文本格式后上传",
        )

    article_title = (title or Path(filename).stem).strip()
    if not article_title:
        article_title = Path(filename).stem
    if len(article_title) > 500:
        raise UploadError(422, "INVALID_TITLE", "文章标题不能超过 500 个字符")

    pseudo_url = f"upload://{user_id}/{filename}"
    content_digest = hashlib.sha1(content_md.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    url_hash = hashlib.md5(f"{pseudo_url}#{content_digest}".encode(), usedforsecurity=False).hexdigest()
    articles = db["articles"]
    if await articles.find_one({"url_hash": url_hash}) is not None:
        raise UploadError(409, "DUPLICATE_ARTICLE", "该文件内容已上传过")

    now = datetime.now(UTC)
    article_document = {
        "url_hash": url_hash,
        "title": article_title,
        "url": pseudo_url,
        "source": "用户上传",
        "source_type": "user_upload",
        "uploaded_by": user_id,
        "original_filename": filename,
        "published_at": now,
        "added_at": now,
        "content_md": content_md,
        "summary": "",
        "summary_cn": "",
        "pipeline_status": "pending",
    }
    try:
        await articles.insert_one(article_document)
    except DuplicateKeyError as exc:
        raise UploadError(409, "DUPLICATE_ARTICLE", "该文件内容已上传过") from exc

    await log_activity(
        db,
        user_id,
        ActionType.ARTICLE_UPLOAD,
        {"article_url_hash": url_hash},
        metadata={
            "title": article_title,
            "original_filename": filename,
            "content_length": len(content_md),
        },
    )
    return {
        "ok": True,
        "data": {
            "url_hash": url_hash,
            "title": article_title,
            "source_type": "user_upload",
            "content_length": len(content_md),
            "message": "文章已入库，可在仪表盘触发分类打分",
        },
    }
