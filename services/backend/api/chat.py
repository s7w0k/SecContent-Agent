"""
对话改稿 REST API — 问答、改稿、应用修订、对话历史

端点:
  POST /api/chat/ask                                          问答模式
  POST /api/articles/{url_hash}/drafts/{draft_index}/revise   生成修订稿
  POST /api/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply  应用修订
  GET  /api/articles/{url_hash}/drafts/{draft_index}/chat-history  获取对话历史
  DELETE /api/articles/{url_hash}/drafts/{draft_index}/chat-history  清空对话历史
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.style_profiler import load_style_hints
from api.activity import log_activity
from api.logs import build_log_error, generate_trace_id, log_pipeline
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["Chat"])

# 东八区时区
_TZ_CN = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _get_db(request: Request):
    """从 app.state 获取 MongoDB 数据库实例"""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return db


async def _load_user_drafts(db, user_id: str, url_hash: str) -> list[dict]:
    user_draft = await db["user_drafts"].find_one(
        {"user_id": user_id, "article_url_hash": url_hash}
    )
    return user_draft.get("drafts", []) if user_draft else []


def _get_draft_chat_agent(request: Request):
    """获取 DraftChatAgent 实例，必要时从现有组件构造"""
    agent = getattr(request.app.state, "draft_chat_agent", None)
    if agent is not None:
        return agent

    knowledge_loader = getattr(request.app.state, "knowledge_loader", None)
    draft_gen = getattr(request.app.state, "draft_gen", None)

    if knowledge_loader is None or draft_gen is None:
        raise HTTPException(
            status_code=503,
            detail="Agent components not initialized (knowledge_loader or draft_gen missing)",
        )

    from agent.draft_chat import DraftChatAgent

    agent = DraftChatAgent(
        llm=draft_gen.llm,
        knowledge_loader=knowledge_loader,
    )
    request.app.state.draft_chat_agent = agent
    return agent


def _now_cn() -> str:
    """当前东八区时间 ISO 字符串"""
    return datetime.now(_TZ_CN).isoformat()


def _username(request: Request, user_id: str) -> str:
    return getattr(getattr(request, "state", None), "username", None) or user_id


async def _log_chat_operation(
    db: Any,
    request: Request,
    user_id: str,
    phase: str,
    message: str,
    trace_id: str,
    *,
    action: str = "complete",
    started: float | None = None,
    detail: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    duration_ms = int((time.perf_counter() - started) * 1000) if started is not None else None
    await log_pipeline(
        db,
        "ERROR" if error else "INFO",
        phase,
        message,
        user_id=user_id,
        username=_username(request, user_id),
        trace_id=trace_id,
        action="error" if error else action,
        duration_ms=duration_ms,
        detail=detail,
        error=build_log_error(error) if error else None,
    )


async def _save_chat_message(
    db,
    user_id: str,
    url_hash: str,
    draft_index: int,
    role: str,
    content: str,
) -> None:
    """将一条消息追加到 chat_sessions 集合。

    使用 (user_id, article_url_hash, draft_index) 作为复合唯一键，
    不存在则创建，存在则追加到 messages 数组。
    """
    msg = {
        "role": role,
        "content": content,
        "created_at": _now_cn(),
    }

    await db["chat_sessions"].update_one(
        {
            "user_id": user_id,
            "article_url_hash": url_hash,
            "draft_index": draft_index,
        },
        {
            "$push": {"messages": msg},
            "$set": {"updated_at": _now_cn()},
            "$setOnInsert": {"user_id": user_id, "created_at": _now_cn()},
        },
        upsert=True,
    )


# ═══════════════════════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════════════════════


class ChatMessage(BaseModel):
    """对话消息"""

    role: str = Field(..., description="角色: user / assistant")
    content: str = Field(..., description="消息内容")


class ChatAskRequest(BaseModel):
    """问答请求"""

    message: str = Field(..., min_length=1, description="用户问题")
    article_url_hash: str | None = Field(default=None, description="文章 URL hash")
    draft_index: int | None = Field(default=None, ge=0, description="草稿序号")
    revision_id: str | None = Field(default=None, description="修订 ID")
    history: list[ChatMessage] = Field(default_factory=list, description="对话历史")


class ChatAskResponse(BaseModel):
    """问答响应"""

    answer: str
    references: list[str]


class DraftReviseRequest(BaseModel):
    """改稿请求"""

    instruction: str = Field(..., min_length=1, description="修改意见")
    save: bool = Field(default=True, description="是否保存修订记录到 MongoDB")


class DraftReviseResponse(BaseModel):
    """改稿响应"""

    revision_id: str
    revised_content_md: str
    change_summary: list[str]
    saved: bool


class ApplyRevisionResponse(BaseModel):
    """应用修订响应"""

    article_url_hash: str
    draft_index: int
    revision_id: str
    applied: bool


# ═══════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════


@router.post("/chat/ask", summary="对话问答")
async def chat_ask(
    request: Request,
    body: ChatAskRequest,
    user_id: str = Depends(get_current_user),
):
    """问答模式：基于文章/草稿/知识库回答用户问题。

    - 如果传入 article_url_hash，读取文章上下文
    - 如果传入 draft_index，读取对应 PR 草稿
    - 加载产品知识库摘要
    - 调用 DraftChatAgent.answer() 返回回答
    - 自动保存对话记录到 chat_sessions 集合
    """
    db = _get_db(request)
    trace_id = generate_trace_id()
    started = time.perf_counter()
    agent = _get_draft_chat_agent(request)
    style_hints = await load_style_hints(db, user_id)

    article = None
    draft = None
    revision = None

    # 读取文章上下文
    if body.article_url_hash:
        article = await db["articles"].find_one({"url_hash": body.article_url_hash})
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        article["_id"] = str(article["_id"])

        # 读取草稿
        if body.draft_index is not None:
            drafts = await _load_user_drafts(db, user_id, body.article_url_hash)
            if body.draft_index < 0 or body.draft_index >= len(drafts):
                raise HTTPException(status_code=404, detail="Draft not found")
            draft = drafts[body.draft_index]

            # 读取修订稿
            if body.revision_id:
                revisions = draft.get("revisions", [])
                revision = next(
                    (r for r in revisions if r.get("revision_id") == body.revision_id),
                    None,
                )
                if revision is None:
                    raise HTTPException(status_code=404, detail="Revision not found")

    # 调用 Agent
    from agent.draft_chat import LLMError

    try:
        result = await agent.answer(
            message=body.message,
            article=article,
            draft=draft,
            revision=revision,
            history=[m.model_dump() for m in body.history] if body.history else None,
            style_hints=style_hints,
        )
    except LLMError as e:
        await _log_chat_operation(
            db,
            request,
            user_id,
            "chat_ask",
            "chat ask failed",
            trace_id,
            started=started,
            detail={"article_url_hash": body.article_url_hash, "draft_index": body.draft_index},
            error=e,
        )
        raise HTTPException(status_code=502, detail=str(e)) from e

    # 保存对话记录到 chat_sessions
    if body.article_url_hash and body.draft_index is not None:
        await _save_chat_message(
            db,
            user_id,
            body.article_url_hash,
            body.draft_index,
            "user",
            body.message,
        )
        await _save_chat_message(
            db,
            user_id,
            body.article_url_hash,
            body.draft_index,
            "assistant",
            result["answer"],
        )

    await _log_chat_operation(
        db,
        request,
        user_id,
        "chat_ask",
        "chat ask completed",
        trace_id,
        started=started,
        detail={
            "article_url_hash": body.article_url_hash,
            "draft_index": body.draft_index,
            "question_length": len(body.message),
            "answer_length": len(result["answer"]),
            "stream": False,
        },
    )
    return {"ok": True, "data": result, "trace_id": trace_id}


@router.post("/chat/ask_stream", summary="流式对话问答（SSE）")
async def chat_ask_stream(
    request: Request,
    body: ChatAskRequest,
    user_id: str = Depends(get_current_user),
):
    """流式问答模式：通过 SSE 逐 chunk 返回回答。

    SSE 事件格式:
        data: {"chunk": "文本片段"}\\n\\n
        data: {"done": true, "answer": "完整回答"}\\n\\n
        data: {"error": "错误信息"}\\n\\n
    """
    db = _get_db(request)
    trace_id = generate_trace_id()
    started = time.perf_counter()
    agent = _get_draft_chat_agent(request)
    style_hints = await load_style_hints(db, user_id)

    article = None
    draft = None
    revision = None

    # 读取文章上下文
    if body.article_url_hash:
        article = await db["articles"].find_one({"url_hash": body.article_url_hash})
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
        article["_id"] = str(article["_id"])

        if body.draft_index is not None:
            drafts = await _load_user_drafts(db, user_id, body.article_url_hash)
            if body.draft_index < 0 or body.draft_index >= len(drafts):
                raise HTTPException(status_code=404, detail="Draft not found")
            draft = drafts[body.draft_index]

            if body.revision_id:
                revisions = draft.get("revisions", [])
                revision = next(
                    (r for r in revisions if r.get("revision_id") == body.revision_id),
                    None,
                )
                if revision is None:
                    raise HTTPException(status_code=404, detail="Revision not found")

    from agent.draft_chat import LLMError

    async def event_stream():
        """SSE 事件生成器"""
        full_answer = []
        try:
            async for chunk in agent.stream_answer(
                message=body.message,
                article=article,
                draft=draft,
                revision=revision,
                history=[m.model_dump() for m in body.history] if body.history else None,
                style_hints=style_hints,
            ):
                full_answer.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"

            answer_text = "".join(full_answer)

            # 保存对话记录
            if body.article_url_hash and body.draft_index is not None:
                await _save_chat_message(
                    db,
                    user_id,
                    body.article_url_hash,
                    body.draft_index,
                    "user",
                    body.message,
                )
                await _save_chat_message(
                    db,
                    user_id,
                    body.article_url_hash,
                    body.draft_index,
                    "assistant",
                    answer_text,
                )

            await _log_chat_operation(
                db,
                request,
                user_id,
                "chat_ask",
                "stream chat ask completed",
                trace_id,
                started=started,
                detail={
                    "article_url_hash": body.article_url_hash,
                    "draft_index": body.draft_index,
                    "question_length": len(body.message),
                    "answer_length": len(answer_text),
                    "stream": True,
                },
            )

            yield f"data: {json.dumps({'done': True, 'answer': answer_text}, ensure_ascii=False)}\n\n"

        except LLMError as e:
            await _log_chat_operation(
                db,
                request,
                user_id,
                "chat_ask",
                "stream chat ask failed",
                trace_id,
                started=started,
                detail={"article_url_hash": body.article_url_hash, "stream": True},
                error=e,
            )
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            await _log_chat_operation(
                db,
                request,
                user_id,
                "chat_ask",
                "stream chat ask failed",
                trace_id,
                started=started,
                detail={"article_url_hash": body.article_url_hash, "stream": True},
                error=e,
            )
            yield f"data: {json.dumps({'error': f'服务器错误: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-ID": trace_id,
        },
    )


@router.post(
    "/articles/{url_hash}/drafts/{draft_index}/revise",
    summary="生成修订稿",
)
async def revise_draft(
    request: Request,
    url_hash: str,
    draft_index: int,
    body: DraftReviseRequest,
    user_id: str = Depends(get_current_user),
):
    """改稿模式：根据修改意见改写指定 PR 初稿。

    - 根据 url_hash 查询文章
    - 根据 draft_index 定位草稿
    - 调用 DraftChatAgent.revise() 生成修订稿
    - save=true 时追加到 pr_drafts[draft_index].revisions
    - 自动保存对话记录到 chat_sessions 集合
    """
    db = _get_db(request)
    trace_id = generate_trace_id()
    started = time.perf_counter()
    agent = _get_draft_chat_agent(request)
    style_hints = await load_style_hints(db, user_id)

    # 查询文章
    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    # 定位草稿
    drafts = await _load_user_drafts(db, user_id, url_hash)
    if draft_index < 0 or draft_index >= len(drafts):
        raise HTTPException(status_code=404, detail="Draft not found")

    draft = drafts[draft_index]
    article["_id"] = str(article["_id"])

    # 调用 Agent
    from agent.draft_chat import LLMError

    try:
        result = await agent.revise(
            instruction=body.instruction,
            article=article,
            draft=draft,
            style_hints=style_hints,
        )
    except LLMError as e:
        await _log_chat_operation(
            db,
            request,
            user_id,
            "chat_revise",
            "draft revision failed",
            trace_id,
            started=started,
            detail={"article_url_hash": url_hash, "draft_index": draft_index},
            error=e,
        )
        raise HTTPException(status_code=502, detail=str(e)) from e

    revised_content = result["revised_content_md"]
    change_summary = result["change_summary"]
    revision_id = str(uuid.uuid4())
    saved = False

    # 保存修订记录
    if body.save:
        revision_record = {
            "revision_id": revision_id,
            "instruction": body.instruction,
            "content_md": revised_content,
            "change_summary": change_summary,
            "created_at": _now_cn(),
            "created_by": user_id,
            "applied": False,
        }

        drafts[draft_index].setdefault("revisions", [])
        drafts[draft_index]["revisions"].append(revision_record)

        await db["user_drafts"].update_one(
            {"user_id": user_id, "article_url_hash": url_hash},
            {"$set": {"drafts": drafts, "updated_at": _now_cn()}},
        )
        saved = True

    # 保存对话记录到 chat_sessions
    assistant_content = "已生成修订稿。\n\n修改摘要：\n" + "\n".join(
        f"- {s}" for s in change_summary
    )
    await _save_chat_message(db, user_id, url_hash, draft_index, "user", body.instruction)
    await _save_chat_message(
        db,
        user_id,
        url_hash,
        draft_index,
        "assistant",
        assistant_content,
    )
    await log_activity(
        db,
        user_id,
        "draft_revise",
        {
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "template": draft.get("template"),
            "perspective": draft.get("perspective"),
            "revision_id": revision_id if saved else None,
        },
        {
            "instruction": body.instruction,
            "saved": saved,
        },
    )

    await _log_chat_operation(
        db,
        request,
        user_id,
        "chat_revise",
        "draft revision completed",
        trace_id,
        started=started,
        detail={
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "instruction_length": len(body.instruction),
            "revision_id": revision_id,
            "saved": saved,
            "stream": False,
        },
    )

    return {
        "ok": True,
        "data": {
            "revision_id": revision_id,
            "revised_content_md": revised_content,
            "change_summary": change_summary,
            "saved": saved,
        },
    }


@router.post(
    "/articles/{url_hash}/drafts/{draft_index}/revise_stream",
    summary="流式改稿（SSE）",
)
async def revise_draft_stream(
    request: Request,
    url_hash: str,
    draft_index: int,
    body: DraftReviseRequest,
    user_id: str = Depends(get_current_user),
):
    """流式改稿模式：通过 SSE 逐 chunk 返回改稿内容。

    SSE 事件格式:
        data: {"chunk": "文本片段"}\\n\\n
        data: {"done": true, "revision_id": "...", "revised_content_md": "...", "change_summary": [...], "saved": true}\\n\\n
        data: {"error": "错误信息"}\\n\\n
    """
    db = _get_db(request)
    trace_id = generate_trace_id()
    started = time.perf_counter()
    agent = _get_draft_chat_agent(request)
    style_hints = await load_style_hints(db, user_id)

    # 查询文章
    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    drafts = await _load_user_drafts(db, user_id, url_hash)
    if draft_index < 0 or draft_index >= len(drafts):
        raise HTTPException(status_code=404, detail="Draft not found")

    draft = drafts[draft_index]
    article["_id"] = str(article["_id"])

    from agent.draft_chat import LLMError, parse_revise_output

    async def event_stream():
        """SSE 事件生成器"""
        full_text = []
        try:
            async for chunk in agent.stream_revise(
                instruction=body.instruction,
                article=article,
                draft=draft,
                style_hints=style_hints,
            ):
                full_text.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"

            raw_text = "".join(full_text)
            change_summary, revised_content = parse_revise_output(raw_text)
            revision_id = str(uuid.uuid4())
            saved = False

            # 保存修订记录
            if body.save:
                revision_record = {
                    "revision_id": revision_id,
                    "instruction": body.instruction,
                    "content_md": revised_content,
                    "change_summary": change_summary,
                    "created_at": _now_cn(),
                    "created_by": user_id,
                    "applied": False,
                }

                drafts[draft_index].setdefault("revisions", [])
                drafts[draft_index]["revisions"].append(revision_record)

                await db["user_drafts"].update_one(
                    {"user_id": user_id, "article_url_hash": url_hash},
                    {"$set": {"drafts": drafts, "updated_at": _now_cn()}},
                )
                saved = True

            # 保存对话记录
            assistant_content = "已生成修订稿。\n\n修改摘要：\n" + "\n".join(
                f"- {s}" for s in change_summary
            )
            await _save_chat_message(
                db,
                user_id,
                url_hash,
                draft_index,
                "user",
                body.instruction,
            )
            await _save_chat_message(
                db,
                user_id,
                url_hash,
                draft_index,
                "assistant",
                assistant_content,
            )
            await log_activity(
                db,
                user_id,
                "draft_revise",
                {
                    "article_url_hash": url_hash,
                    "draft_index": draft_index,
                    "template": draft.get("template"),
                    "perspective": draft.get("perspective"),
                    "revision_id": revision_id if saved else None,
                },
                {
                    "instruction": body.instruction,
                    "saved": saved,
                    "stream": True,
                },
            )

            await _log_chat_operation(
                db,
                request,
                user_id,
                "chat_revise",
                "stream draft revision completed",
                trace_id,
                started=started,
                detail={
                    "article_url_hash": url_hash,
                    "draft_index": draft_index,
                    "instruction_length": len(body.instruction),
                    "revision_id": revision_id,
                    "saved": saved,
                    "stream": True,
                },
            )

            yield f"data: {json.dumps({'done': True, 'revision_id': revision_id, 'revised_content_md': revised_content, 'change_summary': change_summary, 'saved': saved}, ensure_ascii=False)}\n\n"

        except LLMError as e:
            await _log_chat_operation(
                db,
                request,
                user_id,
                "chat_revise",
                "stream draft revision failed",
                trace_id,
                started=started,
                detail={"article_url_hash": url_hash, "draft_index": draft_index, "stream": True},
                error=e,
            )
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            await _log_chat_operation(
                db,
                request,
                user_id,
                "chat_revise",
                "stream draft revision failed",
                trace_id,
                started=started,
                detail={"article_url_hash": url_hash, "draft_index": draft_index, "stream": True},
                error=e,
            )
            yield f"data: {json.dumps({'error': f'服务器错误: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-ID": trace_id,
        },
    )


@router.post(
    "/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply",
    summary="应用修订稿",
)
async def apply_revision(
    request: Request,
    url_hash: str,
    draft_index: int,
    revision_id: str,
    user_id: str = Depends(get_current_user),
):
    """应用修订：将修订稿写回草稿主稿 content_md，并标记 applied=true。"""
    db = _get_db(request)
    trace_id = generate_trace_id()
    started = time.perf_counter()

    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    drafts = await _load_user_drafts(db, user_id, url_hash)
    if draft_index < 0 or draft_index >= len(drafts):
        raise HTTPException(status_code=404, detail="Draft not found")

    revisions = drafts[draft_index].get("revisions", [])
    target_revision = None
    for r in revisions:
        if r.get("revision_id") == revision_id:
            target_revision = r
            break

    if target_revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")

    drafts[draft_index]["content_md"] = target_revision["content_md"]
    target_revision["applied"] = True

    await db["user_drafts"].update_one(
        {"user_id": user_id, "article_url_hash": url_hash},
        {"$set": {"drafts": drafts, "updated_at": _now_cn()}},
    )
    await log_activity(
        db,
        user_id,
        "revision_apply",
        {
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "template": drafts[draft_index].get("template"),
            "perspective": drafts[draft_index].get("perspective"),
            "revision_id": revision_id,
        },
    )

    await _log_chat_operation(
        db,
        request,
        user_id,
        "chat_apply",
        "revision applied",
        trace_id,
        started=started,
        detail={
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "revision_id": revision_id,
        },
    )

    return {
        "ok": True,
        "data": {
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "revision_id": revision_id,
            "applied": True,
            "trace_id": trace_id,
        },
    }


@router.get(
    "/articles/{url_hash}/drafts/{draft_index}/chat-history",
    summary="获取对话历史",
)
async def get_chat_history(
    request: Request,
    url_hash: str,
    draft_index: int,
    user_id: str = Depends(get_current_user),
):
    """获取指定文章+草稿的对话历史记录。

    返回 chat_sessions 集合中存储的 messages 数组。
    """
    db = _get_db(request)

    session = await db["chat_sessions"].find_one(
        {
            "user_id": user_id,
            "article_url_hash": url_hash,
            "draft_index": draft_index,
        },
    )

    if session is None:
        return {"ok": True, "data": {"messages": []}}

    return {
        "ok": True,
        "data": {
            "messages": session.get("messages", []),
        },
    }


@router.delete(
    "/articles/{url_hash}/drafts/{draft_index}/chat-history",
    summary="清空对话历史",
)
async def clear_chat_history(
    request: Request,
    url_hash: str,
    draft_index: int,
    user_id: str = Depends(get_current_user),
):
    """清空指定文章+草稿的对话历史记录。"""
    db = _get_db(request)

    await db["chat_sessions"].delete_one(
        {
            "user_id": user_id,
            "article_url_hash": url_hash,
            "draft_index": draft_index,
        },
    )

    return {"ok": True, "data": {"cleared": True}}
