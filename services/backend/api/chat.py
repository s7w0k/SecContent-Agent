"""
对话改稿 REST API — 问答、改稿、应用修订、对话历史

端点:
  POST /api/chat/ask                                          问答模式
  POST /api/articles/{url_hash}/drafts/{draft_index}/revise   生成修订稿
  POST /api/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply  应用修订
  POST /api/articles/{url_hash}/drafts/{draft_index}/review   手动重新检查稿件
  GET  /api/articles/{url_hash}/drafts/{draft_index}/chat-history  获取对话历史
  DELETE /api/articles/{url_hash}/drafts/{draft_index}/chat-history  清空对话历史
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.draft_chat import MAX_HISTORY_TURNS
from agent.draft_reviewer import DraftReviewer, compute_content_hash
from agent.style_profiler import load_style_hints
from agent.template_compat import normalize_legacy_drafts, template_reference
from api.activity import log_activity
from api.logs import build_log_error, generate_trace_id, log_pipeline
from auth.deps import get_current_user
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from logging_config import get_audit_logger
from models.draft_review import DraftReview
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
    return normalize_legacy_drafts(user_draft.get("drafts", [])) if user_draft else []


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


def _get_draft_reviewer(request: Request) -> DraftReviewer | None:
    """获取共享审核器；兼容只注入 draft_gen 的测试和轻量运行环境。"""

    reviewer = getattr(request.app.state, "draft_reviewer", None)
    if reviewer is not None:
        return reviewer
    draft_gen = getattr(request.app.state, "draft_gen", None)
    llm = getattr(draft_gen, "llm", None)
    if llm is None:
        return None
    reviewer = DraftReviewer(llm=llm)
    request.app.state.draft_reviewer = reviewer
    return reviewer


async def _review_draft_safely(
    request: Request,
    article: dict[str, Any],
    draft: dict[str, Any],
) -> DraftReview:
    """审核失败时返回可存储的失败状态，不影响稿件本身。"""

    reviewer = _get_draft_reviewer(request)
    try:
        if reviewer is None:
            raise RuntimeError("Draft reviewer not initialized")
        result = await reviewer.review(article, draft)
        return result if isinstance(result, DraftReview) else DraftReview.model_validate(result)
    except Exception as exc:
        return DraftReview(
            status="failed",
            content_hash=compute_content_hash(str(draft.get("content_md") or "")),
            summary="稿件检查失败",
            issues=[],
            counts={"high": 0, "medium": 0, "low": 0},
            fact_check_available=bool(article.get("content_md")),
            error=str(exc).strip() or type(exc).__name__,
        )


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


async def _load_chat_history(
    db,
    user_id: str,
    url_hash: str,
    draft_index: int,
) -> list[dict]:
    """从 chat_sessions 加载最近对话历史，供改稿时注入上下文。"""
    session = await db["chat_sessions"].find_one(
        {"user_id": user_id, "article_url_hash": url_hash, "draft_index": draft_index},
    )
    if session is None:
        return []
    messages = session.get("messages", [])
    recent = messages[-MAX_HISTORY_TURNS * 2:] if messages else []
    return [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in recent]


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
    selected_text: str | None = Field(default=None, description="选中的段落原文，非空时进入局部改写模式")
    selected_range: dict | None = Field(default=None, description="段落范围 {start, end}")


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

    # 手动触发偏好学习：用户输入"沉淀偏好""学习偏好"等关键词
    memory_trigger_keywords = ("沉淀偏好", "学习偏好", "保存偏好", "记忆偏好", "提取偏好")
    if any(kw in body.message for kw in memory_trigger_keywords):
        if body.article_url_hash and body.draft_index is not None:
            # 加载对话历史
            chat_history = await _load_chat_history(db, user_id, body.article_url_hash, body.draft_index)
            # 格式化为文本
            history_text = "\n".join(
                f"{'用户' if m.get('role') == 'user' else '助手'}: {m.get('content', '')[:500]}"
                for m in chat_history
            )
            if not history_text.strip():
                history_text = body.message

            # 创建记忆事件
            from agent.memory_event_service import create_memory_event
            from models.memory import MemorySourceType

            category_v2 = article.get("category_v2") if article else None
            await create_memory_event(
                db,
                user_id,
                MemorySourceType.EXPLICIT_CORRECTION,
                source_id=f"manual-{generate_trace_id()[:8]}",
                article_url_hash=body.article_url_hash,
                draft_index=body.draft_index,
                category_v2=category_v2,
                payload={"chat_history": history_text},
                arq_pool=getattr(request.app.state, "arq_pool", None),
            )

            # 保存对话记录
            await _save_chat_message(
                db, user_id, body.article_url_hash, body.draft_index, "user", body.message,
            )
            response_msg = "已从当前对话历史中提取偏好，正在后台学习。学习完成后可在「个人偏好」页面查看。"
            await _save_chat_message(
                db, user_id, body.article_url_hash, body.draft_index, "assistant", response_msg,
            )

            return {
                "ok": True,
                "data": {"answer": response_msg, "sources": []},
                "trace_id": trace_id,
            }
        else:
            return {
                "ok": True,
                "data": {"answer": "请在稿件对话页面中使用此功能，需要先选择一篇文章和草稿。", "sources": []},
                "trace_id": trace_id,
            }

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
    get_audit_logger().log(
        user_id=user_id,
        action="chat_ask",
        resource=body.article_url_hash or "",
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

    # 手动触发偏好学习：用户输入"沉淀偏好""学习偏好"等关键词
    memory_trigger_keywords = ("沉淀偏好", "学习偏好", "保存偏好", "记忆偏好", "提取偏好")
    if any(kw in body.message for kw in memory_trigger_keywords):
        if body.article_url_hash and body.draft_index is not None:
            # 加载对话历史
            chat_history = await _load_chat_history(db, user_id, body.article_url_hash, body.draft_index)
            history_text = "\n".join(
                f"{'用户' if m.get('role') == 'user' else '助手'}: {m.get('content', '')[:500]}"
                for m in chat_history
            )
            if not history_text.strip():
                history_text = body.message

            from agent.memory_event_service import create_memory_event
            from models.memory import MemorySourceType

            category_v2 = article.get("category_v2") if article else None
            await create_memory_event(
                db,
                user_id,
                MemorySourceType.EXPLICIT_CORRECTION,
                source_id=f"manual-{generate_trace_id()[:8]}",
                article_url_hash=body.article_url_hash,
                draft_index=body.draft_index,
                category_v2=category_v2,
                payload={"chat_history": history_text},
                arq_pool=getattr(request.app.state, "arq_pool", None),
            )

            response_msg = "已从当前对话历史中提取偏好，正在后台学习。学习完成后可在「个人偏好」页面查看。"

            # 保存对话记录
            await _save_chat_message(
                db, user_id, body.article_url_hash, body.draft_index, "user", body.message,
            )
            await _save_chat_message(
                db, user_id, body.article_url_hash, body.draft_index, "assistant", response_msg,
            )

            # 以 SSE 格式返回，携带 memory_learning 标记供前端轮询
            async def memory_trigger_stream():
                yield f"data: {json.dumps({'chunk': response_msg}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'done': True, 'answer': response_msg, 'memory_learning': True}, ensure_ascii=False)}\n\n"
            return StreamingResponse(memory_trigger_stream(), media_type="text/event-stream")
        else:
            response_msg = "请在稿件对话页面中使用此功能，需要先选择一篇文章和草稿。"

            async def memory_hint_stream():
                yield f"data: {json.dumps({'chunk': response_msg}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'done': True, 'answer': response_msg}, ensure_ascii=False)}\n\n"
            return StreamingResponse(memory_hint_stream(), media_type="text/event-stream")

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

    # 加载对话历史，让改稿基于当前对话上下文
    chat_history = await _load_chat_history(db, user_id, url_hash, draft_index)

    # 调用 Agent
    from agent.draft_chat import LLMError

    try:
        result = await agent.revise(
            instruction=body.instruction,
            article=article,
            draft=draft,
            style_hints=style_hints,
            selected_text=body.selected_text,
            selected_range=body.selected_range,
            history=chat_history,
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
            **({"selected_text": body.selected_text, "selected_range": body.selected_range}
               if body.selected_text else {}),
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
            **template_reference(draft),
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

    get_audit_logger().log(
        user_id=user_id,
        action="chat_revise",
        resource=url_hash,
    )

    # 双写记忆事件（改稿请求）
    from agent.memory_event_service import create_memory_event
    from models.memory import MemorySourceType

    await create_memory_event(
        db,
        user_id,
        MemorySourceType.REVISION_REQUEST,
        source_id=revision_id,
        article_url_hash=url_hash,
        draft_index=draft_index,
        revision_id=revision_id,
        category_v2=article.get("category_v2"),
        payload={"instruction": body.instruction[:500]},
        idempotency_key=f"revision_request:{revision_id}",
        arq_pool=getattr(request.app.state, "arq_pool", None),
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

    # 加载对话历史，让改稿基于当前对话上下文
    chat_history = await _load_chat_history(db, user_id, url_hash, draft_index)

    from agent.draft_chat import LLMError, apply_section_revise, parse_revise_output

    async def event_stream():
        """SSE 事件生成器"""
        full_text = []
        try:
            async for chunk in agent.stream_revise(
                instruction=body.instruction,
                article=article,
                draft=draft,
                style_hints=style_hints,
                selected_text=body.selected_text,
                selected_range=body.selected_range,
                history=chat_history,
            ):
                full_text.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk}, ensure_ascii=False)}\n\n"

            raw_text = "".join(full_text)
            change_summary, revised_content = parse_revise_output(raw_text)

            # 局部改写：将改写后的段落替换回完整草稿
            if body.selected_text and body.selected_text.strip():
                original_content = draft.get("content_md", "")[:4000]
                revised_content = apply_section_revise(
                    original_content, revised_content, body.selected_text, body.selected_range
                )

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
                    **({"selected_text": body.selected_text, "selected_range": body.selected_range}
                       if body.selected_text else {}),
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
                    **template_reference(draft),
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

            # 双写记忆事件（改稿请求）
            from agent.memory_event_service import create_memory_event
            from models.memory import MemorySourceType

            await create_memory_event(
                db,
                user_id,
                MemorySourceType.REVISION_REQUEST,
                source_id=revision_id,
                article_url_hash=url_hash,
                draft_index=draft_index,
                revision_id=revision_id,
                category_v2=article.get("category_v2"),
                payload={"instruction": body.instruction[:500]},
                idempotency_key=f"revision_request:{revision_id}",
                arq_pool=getattr(request.app.state, "arq_pool", None),
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
    "/articles/{url_hash}/drafts/{draft_index}/review",
    summary="重新检查稿件内容与宣传话术",
)
async def review_draft(
    request: Request,
    url_hash: str,
    draft_index: int,
    user_id: str = Depends(get_current_user),
):
    """手动重查当前草稿，并用新结果覆盖该草稿原有审核结果。"""

    db = _get_db(request)
    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    drafts = await _load_user_drafts(db, user_id, url_hash)
    if draft_index < 0 or draft_index >= len(drafts):
        raise HTTPException(status_code=404, detail="Draft not found")

    result = await _review_draft_safely(request, article, drafts[draft_index])
    result_data = result.model_dump(mode="json")
    await db["user_drafts"].update_one(
        {"user_id": user_id, "article_url_hash": url_hash},
        {
            "$set": {
                f"drafts.{draft_index}.review": result_data,
                "updated_at": _now_cn(),
            }
        },
    )
    return {"ok": True, "data": result_data}


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
    # 移除同步稿件检查，避免用户等待 LLM 推理；用户可单独点击"稿件检查"触发

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
            **template_reference(drafts[draft_index]),
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

    get_audit_logger().log(
        user_id=user_id,
        action="chat_apply",
        resource=url_hash,
    )

    # 双写记忆事件（应用修订，强信号）
    from agent.memory_event_service import create_memory_event
    from models.memory import MemorySourceType

    await create_memory_event(
        db,
        user_id,
        MemorySourceType.REVISION_APPLY,
        source_id=revision_id,
        article_url_hash=url_hash,
        draft_index=draft_index,
        revision_id=revision_id,
        category_v2=article.get("category_v2"),
        payload={
            "instruction": target_revision.get("instruction", "")[:500],
            "diff_summary": target_revision.get("change_summary", [])[:10],
        },
        idempotency_key=f"revision_apply:{revision_id}",
        arq_pool=getattr(request.app.state, "arq_pool", None),
    )

    return {
        "ok": True,
        "data": {
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "revision_id": revision_id,
            "applied": True,
            "review": drafts[draft_index]["review"],
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
