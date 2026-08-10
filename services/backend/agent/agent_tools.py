"""Agent Loop 只读工具集 -- 阶段一 Step 4。

三个只读工具供 DraftChatAgent 的 Agent Loop 使用：
  - search_knowledge：按产品 ID 查询知识切片
  - retrieve_memory：检索当前用户记忆偏好
  - get_article：查询文章详情（受白名单约束）

安全约束：
  - 模型参数中不提供身份字段（user_id 等通过闭包绑定）
  - 产品/文章 ID 受 RunContext 白名单约束
  - 工具结果只进 tool role，不进 system
  - 结果按预算截断
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from agent.agent_contracts import RunContext, ToolPolicy, TypedToolResult
from langchain_core.tools import tool

logger = logging.getLogger("backend.agent.agent_tools")

# 工具结果单条最大字符数
MAX_TOOL_RESULT_CHARS = 3000


def _truncate(text: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> tuple[str, bool, int]:
    """截断文本，返回 (text, truncated, original_char_count)。"""
    original_len = len(text)
    if original_len <= max_chars:
        return text, False, original_len
    return text[:max_chars] + "...(truncated)", True, original_len


def _args_hash(**kwargs: Any) -> str:
    """计算工具入参的 hash（用于日志审计）。"""
    combined = str(sorted(kwargs.items()))
    return f"sha256:{hashlib.sha256(combined.encode('utf-8')).hexdigest()[:16]}"


# ═══════════════════════════════════════════════════════════════
# 工具策略定义
# ═══════════════════════════════════════════════════════════════

TOOL_POLICIES: dict[str, ToolPolicy] = {
    "search_knowledge": ToolPolicy(
        name="搜索产品知识",
        idempotent=True,
        requires_product_allowlist=True,
    ),
    "retrieve_memory": ToolPolicy(
        name="检索用户偏好",
        idempotent=True,
    ),
    "get_article": ToolPolicy(
        name="查询文章详情",
        idempotent=True,
        requires_article_allowlist=True,
    ),
}


# ═══════════════════════════════════════════════════════════════
# 工具工厂（闭包绑定 db / run_context）
# ═══════════════════════════════════════════════════════════════


def create_agent_tools(
    *,
    db: Any,
    run_context: RunContext,
    knowledge_base_dir: str = "/app/docs",
) -> list[Any]:
    """创建绑定了 db 和 run_context 的工具列表。

    Args:
        db: MongoDB 数据库实例
        run_context: 运行上下文（身份/权限/白名单）
        knowledge_base_dir: 知识库目录

    Returns:
        LangChain @tool 装饰的工具列表
    """

    @tool
    async def search_knowledge(product_id: str, purpose: str = "chat") -> str:
        """搜索产品知识库，返回产品定位和核心功能摘要。当用户询问产品能力或需要产品知识辅助回答时使用。

        Args:
            product_id: 产品 ID（必须为白名单内的产品）
            purpose: 用途，chat=对话问答, draft=草稿改写。默认 chat。
        """
        # 白名单校验
        if not run_context.is_product_allowed(product_id):
            logger.warning(
                "[%s] search_knowledge blocked: product_id=%s not in allowlist",
                run_context.trace_id,
                product_id,
            )
            result = TypedToolResult.failure(
                f"产品 {product_id} 不在允许列表内",
                error_code="permission_denied",
            )
            return result.to_tool_message_content()

        try:
            from agent.knowledge_slice import KnowledgeSliceResolver

            resolver = KnowledgeSliceResolver(
                knowledge_base_dir=knowledge_base_dir,
                db=db,
            )
            slice_result = await resolver.resolve(
                purpose=purpose if purpose in ("chat", "draft", "score") else "chat",
                product_ids=[product_id],
                user_id=run_context.user_id,
                max_chars=MAX_TOOL_RESULT_CHARS,
            )

            if not slice_result.content:
                result = TypedToolResult.failure(
                    f"未找到产品 {product_id} 的知识文档",
                    error_code="not_found",
                )
            else:
                text, truncated, char_count = _truncate(slice_result.content)
                result = TypedToolResult.success(
                    text,
                    source_ids=slice_result.source_document_ids,
                    truncated=truncated,
                    char_count=char_count,
                )
        except Exception as e:
            logger.exception("[%s] search_knowledge failed", run_context.trace_id)
            result = TypedToolResult.failure(
                f"知识检索失败: {type(e).__name__}",
                error_code="db_error",
            )

        return result.to_tool_message_content()

    @tool
    async def retrieve_memory(category: str = "") -> str:
        """检索当前用户的记忆偏好（写作风格、关注点等）。当需要个性化回答或改稿时使用。

        Args:
            category: 可选，按分类过滤（如文章的 category_v2）
        """
        try:
            from config import get_settings

            settings = get_settings()
            if not settings.MEMORY_FEATURE_ENABLED:
                result = TypedToolResult.failure(
                    "记忆功能未启用",
                    error_code="feature_disabled",
                )
                return result.to_tool_message_content()

            from agent.memory_retriever import MemoryRetriever
            from models.memory import MemoryStage

            retriever = MemoryRetriever(db)
            # REVISE 阶段记忆最丰富
            stage = MemoryStage.REVISE
            pack = await retriever.retrieve(
                user_id=run_context.user_id,
                category_v2=category if category else None,
                stage=stage,
            )

            rendered = pack.rendered_text or ""
            if not rendered.strip():
                result = TypedToolResult.failure(
                    "暂无用户偏好记忆",
                    error_code="empty",
                )
            else:
                text, truncated, char_count = _truncate(rendered)
                result = TypedToolResult.success(
                    text,
                    source_ids=pack.memory_ids if hasattr(pack, "memory_ids") else [],
                    truncated=truncated,
                    char_count=char_count,
                )
        except Exception as e:
            logger.exception("[%s] retrieve_memory failed", run_context.trace_id)
            result = TypedToolResult.failure(
                f"记忆检索失败: {type(e).__name__}",
                error_code="db_error",
            )

        return result.to_tool_message_content()

    @tool
    async def get_article(url_hash: str) -> str:
        """查询文章详情（标题、来源、分类、摘要、正文前段）。当需要了解文章内容时使用。

        Args:
            url_hash: 文章 URL hash（必须为本次会话已加载的文章）
        """
        # 白名单校验
        if not run_context.is_article_allowed(url_hash):
            logger.warning(
                "[%s] get_article blocked: url_hash=%s not in allowlist",
                run_context.trace_id,
                url_hash,
            )
            result = TypedToolResult.failure(
                f"文章 {url_hash} 不在允许列表内",
                error_code="permission_denied",
            )
            return result.to_tool_message_content()

        try:
            # 只投影安全字段
            projection = {
                "title": 1,
                "source": 1,
                "category_v2": 1,
                "summary": 1,
                "summary_cn": 1,
                "content_md": 1,
                "url": 1,
                "_id": 0,
            }
            article = await db["articles"].find_one(
                {"url_hash": url_hash},
                projection,
            )

            if article is None:
                result = TypedToolResult.failure(
                    f"文章 {url_hash} 不存在",
                    error_code="not_found",
                )
            else:
                # 构建安全文本（不暴露内部字段）
                parts: list[str] = []
                if article.get("title"):
                    parts.append(f"标题: {article['title']}")
                if article.get("source"):
                    parts.append(f"来源: {article['source']}")
                if article.get("category_v2"):
                    parts.append(f"分类: {article['category_v2']}")
                if article.get("summary_cn"):
                    parts.append(f"摘要: {article['summary_cn']}")
                elif article.get("summary"):
                    parts.append(f"摘要: {article['summary']}")
                if article.get("content_md"):
                    # 正文只取前段
                    content = article["content_md"]
                    parts.append(f"正文前段: {content[:1500]}")

                text = "\n".join(parts)
                text, truncated, char_count = _truncate(text)
                result = TypedToolResult.success(
                    text,
                    source_ids=[url_hash],
                    truncated=truncated,
                    char_count=char_count,
                )
        except Exception as e:
            logger.exception("[%s] get_article failed", run_context.trace_id)
            result = TypedToolResult.failure(
                f"文章查询失败: {type(e).__name__}",
                error_code="db_error",
            )

        return result.to_tool_message_content()

    return [search_knowledge, retrieve_memory, get_article]
