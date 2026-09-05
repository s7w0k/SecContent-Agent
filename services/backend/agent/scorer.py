"""
双维度打分 Agent

对文章进行 AI/Agent 安全相关度和可报道性双维度评分（各 0-100），
综合分 ≥ 140 时标记为高价值文章，触发后续报道生成。

特性:
  - System Prompt 注入产品知识库上下文
  - 并发打分控制（asyncio.Semaphore）
  - JSON 响应解析 + 降级处理
  - 分数范围校验 + 必填字段检查

使用:
    from langchain_openai import ChatOpenAI
    from agent.knowledge import KnowledgeLoader
    from agent.scorer import ScoringAgent

    llm = ChatOpenAI(
        model="deepseek-chat",
        api_key="sk-xxx",
        base_url="https://api.deepseek.com",
        temperature=0.1,
    )
    knowledge = await loader.load()
    scorer = ScoringAgent(llm=llm, knowledge=knowledge)
    scores = await scorer.score_batch(articles)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from agent.knowledge import ProductKnowledge
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("backend.agent.scorer")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DEFAULT_CONCURRENCY = 20
SCORE_MIN = 0
SCORE_MAX = 100
HIGH_VALUE_THRESHOLD = 140
DEFAULT_TEMPERATURE = 0.1  # 低温度确保打分一致性
MAX_RETRIES = 1

SYSTEM_PROMPT_TEMPLATE = """你是一个智能体安全领域的技术情报分析师。请根据以下产品知识库，判断文章与产品的语义相关度。

## 产品知识库
{knowledge_context}

## 打分任务
基于产品知识库内容，判断以下文章与该产品的语义相关度和可报道性。

## 评分维度

### 1. AI/Agent安全相关度 (ai_relevance_score: 0-100)
阅读文章全文，判断其内容与上述产品知识库中描述的产品定位、核心功能、技术壁垒、控标点的语义相关度：
- **90-100**: 文章主题与产品核心领域直接对齐（身份安全/Agent安全/MCP协议/权限管控）
- **70-89**: 文章内容与产品相关领域有明确交集（AI安全/模型安全/供应链安全）
- **40-69**: 文章涉及泛安全话题，与产品有间接关联
- **0-39**: 与产品知识库内容无明显语义关联

### 2. 可报道性 (reportability_score: 0-100)
判断该文章作为内部PR情报的价值：
- **90-100**: 含重大漏洞/技术突破，对客户或产品有直接影响
- **70-89**: 有价值的行业趋势/竞品动态，可指导产品规划
- **40-69**: 一般性安全报道，可作为行业背景参考
- **0-39**: 日常动态，无报道价值

## 输出格式
请严格输出JSON，不要加代码块标记：
{{"ai_relevance_score": 85, "reportability_score": 70, "score_reason": "简短理由", "tags": ["标签"]}}
"""

USER_PROMPT_TEMPLATE = """请基于产品知识库对以下文章进行语义相关度评分：

**标题**: {title}
**来源**: {source}
**分类**: {category}
**文章摘要**: {summary}
**文章正文（前段）**: {content}
"""


# ═══════════════════════════════════════════════════════════════
# ScoringAgent
# ═══════════════════════════════════════════════════════════════


class ScoringAgent:
    """双维度打分 Agent。

    使用 LLM 对文章进行 AI 安全相关度和可报道性评分，
    支持单篇和批量并发打分。
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
        self.system_prompt = self._build_system_prompt()

    # ── 公开接口 ──────────────────────────────────────────────

    async def score_single(self, article: dict | Any) -> dict:
        """单篇打分（被 score_batch 批量调用时使用批量 LLM 打分，更快）"""
        art = article if isinstance(article, dict) else article.model_dump()
        return await self._score_with_llm(art)

    async def _score_with_llm(self, art: dict) -> dict:
        user_prompt = self._build_user_prompt(art)
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self.llm.ainvoke(
                    [
                        SystemMessage(content=self.system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                )
                raw = response.content if hasattr(response, "content") else str(response)
                return self._enrich_result(self._validate_and_fix(self._parse_response(raw)))
            except Exception as e:
                if attempt == MAX_RETRIES:
                    return self._fallback_score(str(e))
        return self._fallback_score("max retries")

    async def score_batch(self, articles: list[dict | Any]) -> list[dict]:
        if not articles:
            return []
        logger.info("Scoring %d articles (parallel)", len(articles))
        results = await asyncio.gather(
            *[self._score_with_llm(a if isinstance(a, dict) else a.model_dump()) for a in articles]
        )
        rlist = list(results)
        ok = sum(1 for r in rlist if not r.get("_fallback"))
        logger.info("Scored: %d/%d ok", ok, len(rlist))
        return rlist

    # ── Prompt 构建 ──────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """构建 System Prompt（含产品知识库上下文）"""
        knowledge_context = self.knowledge.as_system_prompt()
        if not knowledge_context:
            knowledge_context = "（知识库未加载，使用通用安全知识）"
        return SYSTEM_PROMPT_TEMPLATE.format(knowledge_context=knowledge_context)

    @staticmethod
    def _build_user_prompt(article: dict) -> str:
        category = article.get("category", "") or "未分类"
        summary = article.get("summary", "") or article.get("summary_cn", "") or "无"
        content = (article.get("content_md", "") or article.get("summary", "") or "")[:500]
        return USER_PROMPT_TEMPLATE.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            category=category,
            summary=summary,
            content=content or "（暂无正文）",
        )

    # ── 响应解析 ─────────────────────────────────────────────

    @staticmethod
    def _parse_response(text: str) -> dict:
        """从 LLM 响应中提取 JSON 并解析。

        支持三种格式:
          1. ```json ... ``` 代码块
          2. 纯 JSON 字符串
          3. 混杂文本中的 JSON 对象
        """
        # 尝试 1: ```json 代码块
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block:
            return json.loads(code_block.group(1).strip())

        # 尝试 2: 直接 JSON
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # 尝试 3: 查找 JSON 对象
        obj_match = re.search(r"\{[^{}]*\}", text)
        if obj_match:
            try:
                return json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Cannot extract JSON from response: {text[:200]}")

    @staticmethod
    def _validate_and_fix(parsed: dict) -> dict:
        """校验并修正打分结果。

        确保:
          - 分数在 0-100 范围内
          - 必填字段存在
          - tags 是列表
        """
        result = {}

        # ai_relevance_score
        score = int(parsed.get("ai_relevance_score", 0))
        result["ai_relevance_score"] = max(SCORE_MIN, min(SCORE_MAX, score))

        # reportability_score
        score = int(parsed.get("reportability_score", 0))
        result["reportability_score"] = max(SCORE_MIN, min(SCORE_MAX, score))

        # score_reason
        result["score_reason"] = str(parsed.get("score_reason", ""))[:500]

        # tags
        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        result["tags"] = [str(t)[:50] for t in tags[:5]]

        return result

    @staticmethod
    def _enrich_result(validated: dict) -> dict:
        """补充计算字段（total_score, is_high_value）"""
        validated["total_score"] = (
            validated["ai_relevance_score"] + validated["reportability_score"]
        )
        validated["is_high_value"] = validated["total_score"] >= HIGH_VALUE_THRESHOLD
        validated["_fallback"] = False
        return validated

    @staticmethod
    def _fallback_score(error: str = "") -> dict:
        """打分失败时的降级结果"""
        return {
            "ai_relevance_score": 0,
            "reportability_score": 0,
            "score_reason": f"Scoring failed: {error[:100]}",
            "tags": [],
            "total_score": 0,
            "is_high_value": False,
            "_fallback": True,
        }
