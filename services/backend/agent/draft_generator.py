"""
PR 草稿生成器 (V2)

对 V2 双维度打分 ≥ 80 的文章，匹配 PR 模板生成 4 篇草稿：
  模板 A × 角度1 → 草稿1
  模板 A × 角度2 → 草稿2
  模板 B × 角度1 → 草稿3
  模板 B × 角度2 → 草稿4

特性:
  - 基于 V2 6分类自动匹配模板（爆点/法规/AI技术）
  - 每篇文章 2 套模板 × 2 个角度 = 4 篇草稿
  - System Prompt 注入产品知识库 + 模板结构
  - 降级处理：LLM 失败时返回模板骨架
  - 草稿保存到 Article.pr_drafts 字段

使用:
    from langchain_openai import ChatOpenAI
    from agent.draft_generator import DraftGenerator

    llm = ChatOpenAI(model="deepseek-chat", ...)
    generator = DraftGenerator(llm=llm, knowledge=knowledge)
    drafts = await generator.generate(article, v2_scores)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent.knowledge import ProductKnowledge
from agent.pr_templates import PRTemplate, match_templates
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("backend.agent.draft_generator")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DRAFTS_PER_ARTICLE = 4  # 2 templates × 2 perspectives
MAX_RETRIES = 1
DEFAULT_TEMPERATURE = 0.4  # 报道需要适度创造性
MAX_CONTENT_LENGTH = 4000

SYSTEM_PROMPT_TEMPLATE = """你是一个智能体安全行业的技术 PR 撰稿人。
请根据产品知识库和报道模板，撰写一篇面向公司内部的产品 PR 情报报道。

## 产品知识库
{knowledge_context}

{template_spec}
{style_hints}
## 写作要求
1. 使用中文撰写，专业但易懂
2. 严格按章节结构输出 Markdown
3. 内容具体，避免空泛的套话
4. 引用文章中的具体事实和数据
5. 每章 2-5 句，精炼有料
"""

USER_PROMPT_TEMPLATE = """请根据以下文章和打分信息生成 PR 报道：

## 文章信息
**标题**: {title}
**来源**: {source}
**发布时间**: {published_at}
**链接**: {url}
**V2分类**: {category_v2}

## V2 打分信息
- 产品能力相关度: {product_relevance}/100
- 事件影响面与传播力: {event_impact}/100
- 综合分: {pr_total_score}/200
- 打分理由: {score_reason}

## 文章全文（前段）
{content}
"""


# ═══════════════════════════════════════════════════════════════
# DraftGenerator
# ═══════════════════════════════════════════════════════════════


class DraftGenerator:
    """PR 草稿生成器 (V2)。

    对高分文章（pr_total_score ≥ 80）生成 4 篇结构化 PR 草稿。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        knowledge: ProductKnowledge,
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        self.llm = llm
        self.llm.temperature = temperature
        self.knowledge = knowledge

    # ── 公开接口 ──────────────────────────────────────────────

    async def generate(
        self,
        article: dict | Any,
        v2_scores: dict | None = None,
        style_hints: str | None = None,
    ) -> dict:
        """为单篇文章生成 4 篇 PR 草稿。

        Args:
            article: 文章数据（dict 或 Pydantic model）
            v2_scores: V2 打分结果（含 product_relevance / event_impact / pr_total_score）
            style_hints: 用户风格偏好提示词（可选）

        Returns:
            {
                "ok": bool,
                "drafts": list[dict],   # 每篇草稿含 template / perspective / content
                "error": str | None,
            }
        """
        art = (
            article
            if isinstance(article, dict)
            else (article.model_dump() if hasattr(article, "model_dump") else article)
        )
        scores = v2_scores or {}

        # 匹配模板
        category_v2 = art.get("category_v2", "")
        templates = match_templates(category_v2)

        if not templates:
            return {
                "ok": False,
                "drafts": [],
                "error": f"No templates for category: {category_v2}",
            }

        # 为每套模板的每个角度生成草稿
        tasks = []
        for tpl in templates:
            for i, perspective in enumerate(tpl.perspectives):
                tasks.append(
                    self._generate_draft(art, scores, tpl, perspective, i + 1, style_hints),
                )

        results = await asyncio.gather(*tasks)
        drafts = []
        for r in results:
            if r is not None:
                drafts.append(r)

        ok = len(drafts) > 0
        logger.info(
            "Generated %d/%d drafts for: %s",
            len(drafts),
            DRAFTS_PER_ARTICLE,
            art.get("title", "")[:40],
        )

        return {
            "ok": ok,
            "drafts": drafts,
            "error": None if ok else "All drafts failed",
        }

    # ── 单篇草稿生成 ──────────────────────────────────────────

    async def _generate_draft(
        self,
        article: dict,
        scores: dict,
        template: PRTemplate,
        perspective: str,
        index: int,
        style_hints: str | None = None,
    ) -> dict | None:
        """生成单篇草稿（含重试）。"""
        system_prompt = self._build_system_prompt(template, perspective, style_hints)
        user_prompt = self._build_user_prompt(article, scores)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self.llm.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                )
                raw = response.content if hasattr(response, "content") else str(response)
                content = self._clean_draft(raw, article.get("title", ""))

                return {
                    "template": template.name,
                    "perspective": perspective,
                    "content_md": content,
                    "title": article.get("title", ""),
                    "index": index,
                }
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.warning(
                        "Draft %s/%d failed: %s",
                        template.name,
                        index,
                        e,
                    )
                    return self._fallback_draft(article, template, perspective, index, str(e))

        return None

    # ── Prompt 构建 ──────────────────────────────────────────

    def _build_system_prompt(
        self,
        template: PRTemplate,
        perspective: str,
        style_hints: str | None = None,
    ) -> str:
        knowledge_context = self.knowledge.as_system_prompt()
        if not knowledge_context:
            knowledge_context = "（知识库未加载）"
        template_spec = template.build_system_prompt(perspective)
        style_section = (
            f"\n{style_hints.strip()}\n" if style_hints and style_hints.strip() else "\n"
        )
        return SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_context=knowledge_context,
            template_spec=template_spec,
            style_hints=style_section,
        )

    @staticmethod
    def _build_user_prompt(article: dict, scores: dict) -> str:
        content = (article.get("content_md", "") or article.get("summary", "") or "")[
            :MAX_CONTENT_LENGTH
        ]
        return USER_PROMPT_TEMPLATE.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            published_at=article.get("published_at", "未知"),
            url=article.get("url", ""),
            category_v2=article.get("category_v2", "未分类"),
            product_relevance=scores.get("product_relevance", 0),
            event_impact=scores.get("event_impact", 0),
            pr_total_score=scores.get("pr_total_score", 0),
            score_reason=scores.get("score_reason", ""),
            content=content or "（文章全文不可用）",
        )

    # ── 草稿清理 ─────────────────────────────────────────────

    @staticmethod
    def _clean_draft(raw_text: str, title: str) -> str:
        """清理 LLM 生成的草稿（移除代码块标记、多余空白等）。"""
        text = raw_text.strip()

        # 移除 markdown 代码块包裹
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # 确保以标题开头
        if not text.startswith("# "):
            text = f"# [{title}]\n\n{text}"

        # 移除尾部常见废话
        import re

        cut_patterns = [
            r"\n*---+\n*.*$",
            r"\n*以上是[^#]*$",
            r"\n*希望这篇[^#]*$",
            r"\n*备注[：:][^#]*$",
        ]
        for pattern in cut_patterns:
            text = re.sub(pattern, "", text)

        return text.strip()

    # ── 降级草稿 ─────────────────────────────────────────────

    @staticmethod
    def _fallback_draft(
        article: dict,
        template: PRTemplate,
        perspective: str,
        index: int,
        error: str,
    ) -> dict:
        """生成失败的降级草稿骨架。"""
        sections_md = "\n\n".join(
            f"## {sec['heading']}\n（待人工补充：{sec['guide'][:30]}...）"
            for sec in template.sections
        )
        return {
            "template": template.name,
            "perspective": perspective,
            "content_md": (
                f"# [待完善] {article.get('title', '')}\n\n"
                f"> 生成失败: {error[:80]}\n"
                f"> 模板: {template.name} | 角度: {perspective}\n\n"
                f"{sections_md}"
            ),
            "title": article.get("title", ""),
            "index": index,
        }
