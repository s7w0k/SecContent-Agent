"""
PR 报道生成 Agent

对高分文章（total_score ≥ 140）生成结构化 Markdown PR 报道，
包含导语、背景、分析、影响评估、行动建议五个板块。

特性:
  - System Prompt 注入产品知识 + 报道模板
  - User Prompt 包含文章全文 + 打分详情
  - 报道存入 MongoDB reports collection
  - 更新 articles collection 的 has_report / report_id
  - 失败时降级为空报道（不阻塞流水线）

使用:
    from langchain_openai import ChatOpenAI
    from agent.reporter import ReportAgent

    llm = ChatOpenAI(model="deepseek-chat", ...)
    reporter = ReportAgent(llm=llm, knowledge=knowledge, db=mongo_db)
    report = await reporter.generate_report(article, scores)
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.knowledge import ProductKnowledge
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("backend.agent.reporter")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DEFAULT_TEMPERATURE = 0.4  # 报道需要适度创造性
MAX_RETRIES = 1
REPORT_TEMPLATE_NAME = "standard_pr"
MAX_CONTENT_LENGTH = 8000  # 截断过长文章

SYSTEM_PROMPT_TEMPLATE = """你是一个智能体安全行业的技术 PR 撰稿人。
请根据提供的文章内容和产品背景，撰写一篇面向公司内部的产品 PR 情报报道。

## 产品背景
{knowledge_context}

{style_hints}

## 报道模板要求
请按以下 Markdown 模板生成报道（使用中文）：

# [文章标题]

## 导语
[2-3句话概述事件核心，必须点明与智能体身份安全的关联]

## 背景
[事件发生的背景信息、涉及厂商/技术、行业上下文]

## 分析
[结合公司产品能力的技术分析，说明为什么这对我们重要。
可以从以下角度展开：
- 与公司产品的关联点（MCP安全、身份认证、权限管控等）
- 技术深度解读
- 行业影响判断]

## 影响评估
[对以下方面的潜在影响：
- 客户：是否需向客户预警或推广
- 行业：对智能体安全领域的推动或冲击
- 竞品：是否涉及竞争对手动态]

## 行动建议
[产品侧可采取的应对建议，关联具体功能模块]

## 关键词
[3-5个关键词标签]
"""

USER_PROMPT_TEMPLATE = """请基于以下信息生成 PR 报道：

## 文章信息
**标题**: {title}
**来源**: {source}
**发布时间**: {published_at}
**链接**: {url}
**分类**: {category}

## 打分信息
- AI/Agent安全相关度: {ai_score}/100
- 可报道性: {reportability}/100
- 综合分: {total_score}/200
- 打分理由: {score_reason}

## 文章全文
{content}
"""


# ═══════════════════════════════════════════════════════════════
# ReportAgent
# ═══════════════════════════════════════════════════════════════


class ReportAgent:
    """PR 报道生成 Agent。

    对高分文章生成结构化 Markdown 报道，并持久化到 MongoDB。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        knowledge: ProductKnowledge,
        db=None,  # AsyncIOMotorDatabase, optional for tests
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        """
        Args:
            llm: LangChain ChatModel
            knowledge: 产品知识库
            db: MongoDB 数据库实例（测试时可传 None）
            temperature: LLM 温度
        """
        self.llm = llm
        self.llm.temperature = temperature
        self.knowledge = knowledge
        self.db = db
        self.system_prompt = self._build_system_prompt()

    # ── 公开接口 ──────────────────────────────────────────────

    async def generate_report(
        self,
        article: dict | Any,
        scores: dict | None = None,
        style_hints: str | None = None,
    ) -> dict:
        """为单篇高分文章生成 PR 报道。

        Args:
            article: 文章数据（dict 或 ArticleInDB 对象）
            scores: 打分结果（含 ai_relevance_score, reportability_score 等）

        Returns:
            {
                "ok": bool,
                "report": dict | None,   # ReportCreate 兼容 dict
                "error": str | None,
            }
        """
        art = article if isinstance(article, dict) else article.model_dump()
        scores = scores or {}

        # 阶段3 S3-3：经 ContextBridge 构建草稿上下文（active 命中时替代全局知识）
        context_plan_content, _context_telemetry = await self._build_context_plan(art)
        knowledge_context = context_plan_content or self.knowledge.as_system_prompt()

        # 截断过长内容
        content = (art.get("content_md", "") or "")[:MAX_CONTENT_LENGTH]

        user_prompt = self._build_user_prompt(art, content, scores)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self.llm.ainvoke(
                    [
                        SystemMessage(
                            content=self._build_system_prompt(style_hints, knowledge_context)
                        ),
                        HumanMessage(content=user_prompt),
                    ]
                )
                raw_text = response.content if hasattr(response, "content") else str(response)
                report_md = self._clean_report(raw_text, art.get("title", ""))

                # 构造 Report 文档
                report = self._build_report_doc(art, scores, report_md)

                # 持久化到 MongoDB
                if self.db is not None:
                    await self._save_to_db(art, report)

                return {"ok": True, "report": report, "error": None}

            except Exception as e:
                logger.warning(
                    "Report generation attempt %d/%d failed: %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    e,
                )
                if attempt == MAX_RETRIES:
                    return {
                        "ok": False,
                        "report": self._fallback_report(art, scores, str(e)),
                        "error": str(e),
                    }

        return {"ok": False, "report": None, "error": "max retries exceeded"}

    # ── Prompt 构建 ──────────────────────────────────────────

    def _build_system_prompt(
        self,
        style_hints: str | None = None,
        knowledge_context: str | None = None,
    ) -> str:
        if knowledge_context is None:
            knowledge_context = self.knowledge.as_system_prompt()
        if not knowledge_context:
            knowledge_context = "（知识库未加载，使用通用产品背景）"
        return SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_context=knowledge_context,
            style_hints=style_hints.strip() if style_hints and style_hints.strip() else "",
        )

    async def _build_context_plan(self, article: dict) -> tuple[str, dict]:
        """阶段3 S3-3：优先经 ContextBridge 构建草稿上下文（off/shadow 仍走全局）。

        返回 (knowledge_context, telemetry)。产品来源取文章冻结的 resolved_products /
        best_product_id；无产品时不做跨产品回退。
        """
        if self.db is None:
            return "", {}
        try:
            from agent.context_bridge import ContextBridge
            from agent.context_cache import get_context_cache
            from agent.context_queries import build_draft_query
            from config import get_settings

            settings = get_settings()
            bridge = ContextBridge(
                db=self.db,
                settings=settings,
                cache=get_context_cache(settings.CONTEXT_CACHE_TTL_SECONDS)
                if settings.KNOWLEDGE_SKILLS_ENABLED
                else None,
            )
            routing_meta = article.get("routing_meta") or {}
            products = list(routing_meta.get("resolved_products") or [])
            if not products:
                best = article.get("best_product_id") or ""
                if best:
                    products = [best]
            if not products:
                return "", {}
            mode = bridge.effective_mode(article.get("user_id") or "")
            if mode == "off":
                return "", {}
            result = await bridge.build_plan(
                purpose="draft",
                user_id=article.get("user_id") or "",
                products=products,
                query=build_draft_query(article),
                model_id=settings.DEEPSEEK_MODEL,
                task_id=str(article.get("task_id") or ""),
                trace_id=str(article.get("trace_id") or ""),
            )
            if result is None:
                return "", {}
            telemetry = result.telemetry
            telemetry["mode"] = mode
            if mode == "active":
                return result.plan.rendered(), telemetry
            logger.info(
                "[reporter] context purpose=draft mode=%s products=%s plan_hash=%s",
                mode,
                products,
                result.plan.plan_hash[:16],
            )
            return "", telemetry
        except Exception as exc:
            logger.warning("[reporter] ContextBridge build failed: %s", exc)
            return "", {}

    @staticmethod
    def _build_user_prompt(
        article: dict,
        content: str,
        scores: dict,
    ) -> str:
        return USER_PROMPT_TEMPLATE.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            published_at=article.get("published_at", "未知"),
            url=article.get("url", ""),
            category=article.get("category", "") or "未分类",
            ai_score=scores.get("ai_relevance_score", 0),
            reportability=scores.get("reportability_score", 0),
            total_score=scores.get("total_score", 0),
            score_reason=scores.get("score_reason", ""),
            content=content or "（文章全文不可用）",
        )

    # ── 报道清理 ─────────────────────────────────────────────

    @staticmethod
    def _clean_report(raw_text: str, title: str) -> str:
        """清理 LLM 生成的报道文本。

        处理:
          - 移除开头的多余空白
          - 确保以 # 标题开头
          - 移除末尾的废话（"以上是..." 等）
        """
        text = raw_text.strip()

        # 如果 LLM 没有按要求以 # 标题开头，插入标题
        if not text.startswith("# "):
            text = f"# [{title}]\n\n{text}"

        # 移除常见结尾废话
        cut_patterns = [
            r"\n*---+\n*.*$",  # 水平线后内容
            r"\n*以上是[^#]*$",  # "以上是..."
            r"\n*希望这篇[^#]*$",  # "希望这篇..."
            r"\n*备注[：:][^#]*$",  # "备注：..."
        ]
        for pattern in cut_patterns:
            text = re.sub(pattern, "", text)

        return text.strip()

    # ── 数据库操作 ───────────────────────────────────────────

    @staticmethod
    def _build_report_doc(
        article: dict,
        scores: dict,
        report_md: str,
    ) -> dict:
        """构造 Report 文档（兼容 ReportCreate schema）"""
        url_hash = article.get("url_hash", "")
        if not url_hash:
            url_hash = hashlib.md5(
                article.get("url", "").encode(), usedforsecurity=False
            ).hexdigest()

        tz = timezone(timedelta(hours=8))
        return {
            "article_url_hash": url_hash,
            "title": article.get("title", ""),
            "content_md": report_md,
            "template": REPORT_TEMPLATE_NAME,
            "scores": {
                "relevance": scores.get("ai_relevance_score", 0),
                "reportability": scores.get("reportability_score", 0),
            },
            "generated_by": "pr-agent-pipeline",
            "created_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _save_to_db(self, article: dict, report: dict) -> None:
        """将报道存入 MongoDB 并更新文章状态"""
        if self.db is None:
            return

        try:
            # 插入报道
            reports_col = self.db["reports"]
            result = await reports_col.insert_one(report)
            report_id = str(result.inserted_id)

            # 更新文章状态
            articles_col = self.db["articles"]
            url_hash = report["article_url_hash"]
            await articles_col.update_one(
                {"url_hash": url_hash},
                {
                    "$set": {
                        "has_report": True,
                        "report_id": report_id,
                    }
                },
            )

            logger.info(
                "Report saved: %s → article=%s report=%s",
                report["title"][:40],
                url_hash[:12],
                report_id,
            )
        except Exception as e:
            logger.error("Failed to save report to MongoDB: %s", e)

    @staticmethod
    def _fallback_report(
        article: dict,
        scores: dict,
        error: str,
    ) -> dict:
        """生成失败时的降级报道"""
        return {
            "article_url_hash": article.get("url_hash", ""),
            "title": f"[待完善] {article.get('title', '')}",
            "content_md": (
                f"# [{article.get('title', '')}]\n\n"
                f"## 导语\n（报道生成失败: {error[:100]}）\n\n"
                f"## 背景\n待人工补充\n\n"
                f"## 分析\n待人工补充\n\n"
                f"## 影响评估\n待人工补充\n\n"
                f"## 行动建议\n待人工补充\n"
            ),
            "template": REPORT_TEMPLATE_NAME,
            "scores": {
                "relevance": scores.get("ai_relevance_score", 0),
                "reportability": scores.get("reportability_score", 0),
            },
            "generated_by": "pr-agent-pipeline-fallback",
        }
