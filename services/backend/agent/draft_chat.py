"""
对话改稿 Agent (V2)

围绕文章、产品知识库、PR 初稿提供自然语言问答和改稿能力：

  1. answer() — 问答模式
     基于文章上下文、草稿、知识库回答用户问题

  2. revise() — 改稿模式
     根据用户修改意见对 PR 初稿进行改写，输出修订稿 + 修改摘要

特性:
  - System Prompt 注入产品知识库上下文（复用 KnowledgeLoader）
  - 6分类标签 + 双维度分数作为上下文
  - Markdown 清洗（去除代码块包裹）
  - 修改摘要结构化解析（## 修改摘要 / ## 修订稿）
  - LLM 异常友好处理

使用:
    from langchain_openai import ChatOpenAI
    from agent.knowledge import KnowledgeLoader
    from agent.draft_chat import DraftChatAgent

    llm = ChatOpenAI(model="deepseek-chat", ...)
    agent = DraftChatAgent(llm=llm, knowledge_loader=knowledge_loader)

    # 问答
    result = await agent.answer(
        message="这篇稿子传播角度够强吗？",
        article=article_dict,
        draft=draft_dict,
    )

    # 改稿
    result = await agent.revise(
        instruction="标题更有冲击力，减少技术细节",
        article=article_dict,
        draft=draft_dict,
    )
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("backend.agent.draft_chat")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DEFAULT_TEMPERATURE = 0.4
MAX_CONTENT_LENGTH = 4000
MAX_HISTORY_TURNS = 5

# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════


class LLMError(Exception):
    """LLM 调用失败异常。"""


# ═══════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════

ANSWER_SYSTEM_PROMPT = """你是一个智能体安全行业的 PR 情报分析助手。

## 回答规则
1. 只能基于提供的文章、草稿、修订稿和产品知识库回答
2. 不确定时说明缺少依据，不编造数据、客户、产品能力
3. 输出中文
4. 对涉及传播策略的问题，给出可执行建议
5. 回答简洁有力，避免空泛套话

## 产品知识库
{knowledge_context}

{style_hints}
"""

ANSWER_USER_PROMPT = """请回答以下问题：

## 用户问题
{message}

{article_context}

{draft_context}

{history_context}
"""

REVISE_SYSTEM_PROMPT = """你是一个智能体安全行业的 PR 改稿专家。

## 改稿规则
1. 必须保留事实边界，不编造数据、客户、产品能力
2. 必须输出完整 Markdown 稿件，不是片段
3. 必须根据用户意见改写，而不是只点评
4. 标题、结构、段落可以调整，但不得丢失核心事实
5. 保持与产品知识库一致的产品定位

## 产品知识库
{knowledge_context}

{style_hints}

## 输出格式（严格遵守）
```markdown
## 修改摘要
- 修改点1
- 修改点2

## 修订稿
# [标题]

（完整 Markdown 稿件）
```
"""

REVISE_USER_PROMPT = """请根据以下意见改写 PR 初稿：

## 用户修改意见
{instruction}

## 原 PR 初稿
- 模板: {template}
- 视角: {perspective}

{original_content}

## 文章信息
- 标题: {title}
- 来源: {source}
- V2分类: {category_v2}
- 产品相关度: {product_relevance}/100
- 事件影响力: {event_impact}/100
"""


# ═══════════════════════════════════════════════════════════════
# DraftChatAgent
# ═══════════════════════════════════════════════════════════════


class DraftChatAgent:
    """对话改稿 Agent，提供问答和改稿两种能力。

    Args:
        llm: LangChain ChatModel 实例（如 ChatOpenAI）
        knowledge_loader: KnowledgeLoader 实例（产品知识库）
    """

    def __init__(
        self,
        llm: BaseChatModel,
        knowledge_loader: Any,
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        self.llm = llm
        self.llm.temperature = temperature
        self.knowledge_loader = knowledge_loader

    # ── 问答 ──────────────────────────────────────────────────

    async def answer(
        self,
        message: str,
        article: dict | None = None,
        draft: dict | None = None,
        revision: dict | None = None,
        history: list[dict] | None = None,
        style_hints: str | None = None,
    ) -> dict:
        """问答模式：基于上下文回答用户问题。

        Args:
            message: 用户问题
            article: 文章数据（可选）
            draft: PR 草稿数据（可选）
            revision: 修订稿数据（可选）
            history: 对话历史 [{role, content}, ...]

        Returns:
            {"answer": str, "references": list[str]}

        Raises:
            LLMError: LLM 调用失败时抛出
        """
        knowledge_context = self._get_knowledge_prompt()
        system_prompt = ANSWER_SYSTEM_PROMPT.format(
            knowledge_context=knowledge_context,
            style_hints=self._style_hints_section(style_hints),
        )

        article_context = self._build_article_context(article) if article else ""
        draft_context = self._build_draft_context(draft, revision)
        history_context = self._build_history_context(history)

        user_prompt = ANSWER_USER_PROMPT.format(
            message=message,
            article_context=article_context,
            draft_context=draft_context,
            history_context=history_context,
        )

        try:
            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
        except Exception as e:
            logger.error("LLM answer failed: %s", e)
            raise LLMError(f"LLM 调用失败: {e}") from e

        raw = response.content if hasattr(response, "content") else str(response)
        answer = raw.strip()

        references = self._build_references(article, draft, revision)

        logger.info(
            "Answer generated, references=%s, length=%d",
            references,
            len(answer),
        )

        return {"answer": answer, "references": references}

    # ── 流式问答 ─────────────────────────────────────────────

    async def stream_answer(
        self,
        message: str,
        article: dict | None = None,
        draft: dict | None = None,
        revision: dict | None = None,
        history: list[dict] | None = None,
        style_hints: str | None = None,
    ) -> AsyncIterator[str]:
        """流式问答模式：逐 chunk 产出回答文本。

        用法:
            async for chunk in agent.stream_answer(message=...):
                print(chunk, end="", flush=True)

        Yields:
            str: LLM 产出的文本片段

        Raises:
            LLMError: LLM 调用失败时抛出
        """
        knowledge_context = self._get_knowledge_prompt()
        system_prompt = ANSWER_SYSTEM_PROMPT.format(
            knowledge_context=knowledge_context,
            style_hints=self._style_hints_section(style_hints),
        )

        article_context = self._build_article_context(article) if article else ""
        draft_context = self._build_draft_context(draft, revision)
        history_context = self._build_history_context(history)

        user_prompt = ANSWER_USER_PROMPT.format(
            message=message,
            article_context=article_context,
            draft_context=draft_context,
            history_context=history_context,
        )

        try:
            async for chunk in self.llm.astream(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            ):
                text = chunk.content if hasattr(chunk, "content") else str(chunk)
                if text:
                    yield text
        except Exception as e:
            logger.error("LLM stream_answer failed: %s", e)
            raise LLMError(f"LLM 流式调用失败: {e}") from e

    # ── 改稿 ──────────────────────────────────────────────────

    async def revise(
        self,
        instruction: str,
        article: dict,
        draft: dict,
        style_hints: str | None = None,
    ) -> dict:
        """改稿模式：根据修改意见改写 PR 初稿。

        Args:
            instruction: 用户修改意见
            article: 文章数据
            draft: PR 草稿数据

        Returns:
            {"revised_content_md": str, "change_summary": list[str]}

        Raises:
            LLMError: LLM 调用失败时抛出
        """
        knowledge_context = self._get_knowledge_prompt()
        system_prompt = REVISE_SYSTEM_PROMPT.format(
            knowledge_context=knowledge_context,
            style_hints=self._style_hints_section(style_hints),
        )

        original_content = draft.get("content_md", "")[:MAX_CONTENT_LENGTH]
        user_prompt = REVISE_USER_PROMPT.format(
            instruction=instruction,
            template=draft.get("template", ""),
            perspective=draft.get("perspective", ""),
            original_content=original_content,
            title=article.get("title", ""),
            source=article.get("source", ""),
            category_v2=article.get("category_v2", "未分类"),
            product_relevance=article.get("product_relevance", 0),
            event_impact=article.get("event_impact", 0),
        )

        try:
            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
        except Exception as e:
            logger.error("LLM revise failed: %s", e)
            raise LLMError(f"LLM 调用失败: {e}") from e

        raw = response.content if hasattr(response, "content") else str(response)
        change_summary, revised_content = parse_revise_output(raw)

        logger.info(
            "Revise completed, summary_count=%d, content_length=%d",
            len(change_summary),
            len(revised_content),
        )

        return {
            "revised_content_md": revised_content,
            "change_summary": change_summary,
        }

    # ── 流式改稿 ─────────────────────────────────────────────

    async def stream_revise(
        self,
        instruction: str,
        article: dict,
        draft: dict,
        style_hints: str | None = None,
    ) -> AsyncIterator[str]:
        """流式改稿模式：逐 chunk 产出改稿文本。

        与 revise() 不同，stream_revise 只产出原始文本，
        解析（修改摘要 + 修订稿分离）由调用方在流结束后处理。

        Yields:
            str: LLM 产出的文本片段

        Raises:
            LLMError: LLM 调用失败时抛出
        """
        knowledge_context = self._get_knowledge_prompt()
        system_prompt = REVISE_SYSTEM_PROMPT.format(
            knowledge_context=knowledge_context,
            style_hints=self._style_hints_section(style_hints),
        )

        original_content = draft.get("content_md", "")[:MAX_CONTENT_LENGTH]
        user_prompt = REVISE_USER_PROMPT.format(
            instruction=instruction,
            template=draft.get("template", ""),
            perspective=draft.get("perspective", ""),
            original_content=original_content,
            title=article.get("title", ""),
            source=article.get("source", ""),
            category_v2=article.get("category_v2", "未分类"),
            product_relevance=article.get("product_relevance", 0),
            event_impact=article.get("event_impact", 0),
        )

        try:
            async for chunk in self.llm.astream(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            ):
                text = chunk.content if hasattr(chunk, "content") else str(chunk)
                if text:
                    yield text
        except Exception as e:
            logger.error("LLM stream_revise failed: %s", e)
            raise LLMError(f"LLM 流式调用失败: {e}") from e

    # ── 上下文构建 ─────────────────────────────────────────────

    def _get_knowledge_prompt(self) -> str:
        """获取产品知识库 System Prompt 片段。"""
        knowledge = getattr(self.knowledge_loader, "_cache", None)
        if knowledge and hasattr(knowledge, "as_system_prompt"):
            prompt = knowledge.as_system_prompt()
            return prompt if prompt else "（知识库未加载）"
        return "（知识库未加载）"

    @staticmethod
    def _style_hints_section(style_hints: str | None) -> str:
        """规范化可选风格提示，未配置时不改变默认 Prompt。"""
        return style_hints.strip() if style_hints and style_hints.strip() else ""

    @staticmethod
    def _build_article_context(article: dict) -> str:
        """构建文章上下文片段。"""
        parts: list[str] = ["## 文章上下文"]
        parts.append(f"- 标题: {article.get('title', '')}")
        parts.append(f"- 来源: {article.get('source', '')}")
        parts.append(f"- V2分类: {article.get('category_v2', '未分类')}")

        pr_total = article.get("pr_total_score", 0)
        if pr_total:
            parts.append(f"- 产品相关度: {article.get('product_relevance', 0)}/100")
            parts.append(f"- 事件影响力: {article.get('event_impact', 0)}/100")
            parts.append(f"- 综合分: {pr_total}/200")

        summary = article.get("summary_cn", "") or article.get("summary", "")
        if summary:
            parts.append(f"- 摘要: {summary[:300]}")

        content = article.get("content_md", "")
        if content:
            parts.append(f"- 正文片段: {content[:500]}...")

        return "\n".join(parts)

    @staticmethod
    def _build_draft_context(
        draft: dict | None,
        revision: dict | None,
    ) -> str:
        """构建草稿/修订稿上下文片段。"""
        if not draft and not revision:
            return ""

        parts: list[str] = []

        if draft:
            parts.append("## 当前 PR 草稿")
            parts.append(f"- 模板: {draft.get('template', '')}")
            parts.append(f"- 视角: {draft.get('perspective', '')}")
            content = draft.get("content_md", "")[:MAX_CONTENT_LENGTH]
            if content:
                parts.append(f"- 内容:\n{content}")

        if revision:
            parts.append("## 当前修订稿")
            parts.append(f"- 修改意见: {revision.get('instruction', '')}")
            content = revision.get("content_md", "")[:MAX_CONTENT_LENGTH]
            if content:
                parts.append(f"- 内容:\n{content}")

        return "\n\n".join(parts)

    @staticmethod
    def _build_history_context(history: list[dict] | None) -> str:
        """构建对话历史上下文片段。"""
        if not history:
            return ""

        recent = history[-MAX_HISTORY_TURNS:]
        parts: list[str] = ["## 对话历史"]
        for msg in recent:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            label = "用户" if role == "user" else "助手"
            parts.append(f"**{label}**: {content}")

        return "\n".join(parts)

    @staticmethod
    def _build_references(
        article: dict | None,
        draft: dict | None,
        revision: dict | None,
    ) -> list[str]:
        """构建引用上下文类型列表。"""
        refs: list[str] = []
        if article:
            refs.append("article")
        if draft:
            refs.append("draft")
        if revision:
            refs.append("revision")
        refs.append("knowledge")
        return refs


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════


def clean_markdown(raw_text: str) -> str:
    """清洗 LLM 返回的 Markdown 文本。

    - 去除首尾代码块包裹（```markdown ... ```）
    - 去除多余空白
    """
    text = raw_text.strip()

    # 移除 markdown 代码块包裹
    if text.startswith("```"):
        lines = text.split("\n")
        # 去除开头的 ```markdown 或 ```
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # 去除结尾的 ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    return text


def parse_revise_output(raw_text: str) -> tuple[list[str], str]:
    """解析改稿 LLM 返回内容，提取修改摘要和修订稿。

    预期格式:
        ## 修改摘要
        - ...
        - ...

        ## 修订稿
        # [标题]
        ...

    解析失败时保底返回：完整输出作为修订稿，修改摘要为空列表。

    Returns:
        (change_summary, revised_content_md)
    """
    text = clean_markdown(raw_text)

    # 尝试匹配 "## 修改摘要" 和 "## 修订稿" 两段
    pattern = re.compile(
        r"##\s*修改摘要\s*\n(.*?)\n*##\s*修订稿\s*\n(.*)",
        re.DOTALL,
    )
    match = pattern.search(text)

    if not match:
        # 解析失败，保底处理
        logger.warning("Failed to parse revise output structure, using fallback")
        return [], text

    summary_section = match.group(1).strip()
    content_section = match.group(2).strip()

    # 提取摘要列表项
    summary_lines: list[str] = []
    for line in summary_section.split("\n"):
        line = line.strip()
        # 去除开头的 - / * / 数字序号
        cleaned = re.sub(r"^[-*]\s*", "", line)
        cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
        if cleaned:
            summary_lines.append(cleaned)

    return summary_lines, content_section
