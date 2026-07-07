"""
对话改稿 REST API — 问答、改稿、应用修订

端点:
  POST /api/chat/ask                                          问答模式
  POST /api/articles/{url_hash}/drafts/{draft_index}/revise   生成修订稿
  POST /api/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply  应用修订
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
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


def _get_draft_chat_agent(request: Request):
    """获取 DraftChatAgent 实例，必要时从现有组件构造"""
    # 优先使用已挂载的 agent
    agent = getattr(request.app.state, "draft_chat_agent", None)
    if agent is not None:
        return agent

    # 从现有组件构造
    knowledge_loader = getattr(request.app.state, "knowledge_loader", None)
    draft_gen = getattr(request.app.state, "draft_gen", None)

    if knowledge_loader is None or draft_gen is None:
        raise HTTPException(
            status_code=503,
            detail="Agent components not initialized (knowledge_loader or draft_gen missing)",
        )

    # 懒加载构造并缓存
    from agent.draft_chat import DraftChatAgent

    agent = DraftChatAgent(
        llm=draft_gen.llm,
        knowledge_loader=knowledge_loader,
    )
    request.app.state.draft_chat_agent = agent
    return agent


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
async def chat_ask(request: Request, body: ChatAskRequest):
    """问答模式：基于文章/草稿/知识库回答用户问题。

    - 如果传入 article_url_hash，读取文章上下文
    - 如果传入 draft_index，读取对应 PR 草稿
    - 加载产品知识库摘要
    - 调用 DraftChatAgent.answer() 返回回答
    """
    db = _get_db(request)
    agent = _get_draft_chat_agent(request)

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
            drafts = article.get("pr_drafts", [])
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
        )
    except LLMError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return {"ok": True, "data": result}


@router.post(
    "/articles/{url_hash}/drafts/{draft_index}/revise",
    summary="生成修订稿",
)
async def revise_draft(
    request: Request,
    url_hash: str,
    draft_index: int,
    body: DraftReviseRequest,
):
    """改稿模式：根据修改意见改写指定 PR 初稿。

    - 根据 url_hash 查询文章
    - 根据 draft_index 定位草稿
    - 调用 DraftChatAgent.revise() 生成修订稿
    - save=true 时追加到 pr_drafts[draft_index].revisions
    """
    db = _get_db(request)
    agent = _get_draft_chat_agent(request)

    # 查询文章
    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    # 定位草稿
    drafts = article.get("pr_drafts", [])
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
        )
    except LLMError as e:
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
            "created_at": datetime.now(_TZ_CN).isoformat(),
            "created_by": "local-user",
            "applied": False,
        }

        # 读出整个 pr_drafts 数组 → 内存修改 → 整体 $set 回写
        drafts[draft_index].setdefault("revisions", [])
        drafts[draft_index]["revisions"].append(revision_record)

        await db["articles"].update_one(
            {"url_hash": url_hash},
            {"$set": {"pr_drafts": drafts}},
        )
        saved = True

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
    "/articles/{url_hash}/drafts/{draft_index}/revisions/{revision_id}/apply",
    summary="应用修订稿",
)
async def apply_revision(
    request: Request,
    url_hash: str,
    draft_index: int,
    revision_id: str,
):
    """应用修订：将修订稿写回草稿主稿 content_md，并标记 applied=true。

    - 定位文章、草稿和修订记录
    - 将 revision.content_md 写回 pr_drafts[draft_index].content_md
    - 标记该修订记录 applied = true
    """
    db = _get_db(request)

    # 查询文章
    article = await db["articles"].find_one({"url_hash": url_hash})
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    # 定位草稿
    drafts = article.get("pr_drafts", [])
    if draft_index < 0 or draft_index >= len(drafts):
        raise HTTPException(status_code=404, detail="Draft not found")

    # 定位修订记录
    revisions = drafts[draft_index].get("revisions", [])
    target_revision = None
    for r in revisions:
        if r.get("revision_id") == revision_id:
            target_revision = r
            break

    if target_revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")

    # 内存修改：写回主稿 + 标记 applied
    drafts[draft_index]["content_md"] = target_revision["content_md"]
    target_revision["applied"] = True

    # 整体 $set 回写
    await db["articles"].update_one(
        {"url_hash": url_hash},
        {"$set": {"pr_drafts": drafts}},
    )

    return {
        "ok": True,
        "data": {
            "article_url_hash": url_hash,
            "draft_index": draft_index,
            "revision_id": revision_id,
            "applied": True,
        },
    }
