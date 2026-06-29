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
from typing import Any, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from agent.knowledge import ProductKnowledge

logger = logging.getLogger("backend.agent.scorer")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DEFAULT_CONCURRENCY = 3
SCORE_MIN = 0
SCORE_MAX = 100
HIGH_VALUE_THRESHOLD = 140
DEFAULT_TEMPERATURE = 0.1  # 低温度确保打分一致性
MAX_RETRIES = 1

SYSTEM_PROMPT_TEMPLATE = """你是一个智能体安全领域的技术情报分析师。

## 产品背景
{knowledge_context}

## 打分任务
请对给定的安全新闻文章进行双维度评分，判断是否值得生成 PR 情报报道。

## 评分维度

### 1. AI/Agent安全相关度 (ai_relevance_score: 0-100)
- **90-100**: 直接涉及智能体身份安全核心领域
  （身份认证、权限管控、意图识别、MCP协议安全、Agent劫持、提示注入防御）
- **70-89**: 密切相关领域重要事件
  （模型安全、AI供应链安全、AI数据安全、AI合规标准）
- **40-69**: 泛安全领域有一定关联
  （传统安全涉及AI元素、AI应用安全事件）
- **0-39**: 基本不相关
  （传统网络安全事件，无明显AI关联）

### 2. 可报道性 (reportability_score: 0-100)
- **90-100**: 重大漏洞披露/新技术突破/行业标志性事件/直接影响客户
- **70-89**: 有分析价值的趋势变化/竞品重要动态/行业标准更新
- **40-69**: 常规安全新闻报道，有一定参考价值
- **0-39**: 日常报道，无明显新闻价值或与产品无关

## 输出格式要求
请严格按以下 JSON 格式输出（不要添加其他内容）：
```json
{{
  "ai_relevance_score": <0-100的整数>,
  "reportability_score": <0-100的整数>,
  "score_reason": "<50字以内的中文理由>",
  "tags": ["<标签1>", "<标签2>"]
}}
```

## 标签参考
MCP协议、身份认证、权限管控、意图注入、提示注入、模型攻击、
供应链安全、合规政策、竞品动态、技术突破、漏洞披露、行业趋势
"""

USER_PROMPT_TEMPLATE = """请对以下文章进行评分：

**标题**: {title}
**来源**: {source}
**分类**: {category}
**原标题摘要**: {summary}
**AI判断**:
  - AI安全相关: {is_ai_security}
  - Agent安全相关: {is_agent_security}
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
        concurrency: int = DEFAULT_CONCURRENCY,
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        """
        Args:
            llm: LangChain ChatModel（ChatOpenAI 兼容接口）
            knowledge: 产品知识库
            concurrency: 并发打分上限
            temperature: LLM 温度参数
        """
        self.llm = llm
        self.llm.temperature = temperature  # 覆盖温度
        self.knowledge = knowledge
        self.semaphore = asyncio.Semaphore(concurrency)
        self.system_prompt = self._build_system_prompt()

    # ── 公开接口 ──────────────────────────────────────────────

    async def score_single(self, article: dict | Any) -> dict:
        """对单篇文章打分。

        Args:
            article: 文章数据（dict 或 ArticleBase 对象）

        Returns:
            {
                "ai_relevance_score": int,
                "reportability_score": int,
                "score_reason": str,
                "tags": list[str],
                "total_score": int,
                "is_high_value": bool,
            }
        """
        # 统一转为 dict
        art = article if isinstance(article, dict) else article.model_dump()

        user_prompt = self._build_user_prompt(art)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self.llm.ainvoke([
                    SystemMessage(content=self.system_prompt),
                    HumanMessage(content=user_prompt),
                ])
                raw_text = response.content if hasattr(response, "content") else str(response)
                parsed = self._parse_response(raw_text)
                validated = self._validate_and_fix(parsed)
                return self._enrich_result(validated)

            except Exception as e:
                logger.warning(
                    "Scoring attempt %d/%d failed: %s",
                    attempt + 1, MAX_RETRIES + 1, e,
                )
                if attempt == MAX_RETRIES:
                    return self._fallback_score(str(e))

        return self._fallback_score("max retries exceeded")

    async def score_batch(self, articles: list[dict | Any]) -> list[dict]:
        """批量并发打分。

        Args:
            articles: 文章列表

        Returns:
            打分结果列表（与输入同序）
        """
        if not articles:
            return []

        logger.info("Scoring %d articles (max concurrency=%d)", len(articles), DEFAULT_CONCURRENCY)

        async def _score_with_limit(article):
            async with self.semaphore:
                return await self.score_single(article)

        tasks = [_score_with_limit(a) for a in articles]
        results = await asyncio.gather(*tasks)
        results_list = list(results)

        success_count = sum(1 for r in results_list if not r.get("_fallback"))
        logger.info("Scoring complete: %d/%d successful", success_count, len(results_list))
        return results_list

    # ── Prompt 构建 ──────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """构建 System Prompt（含产品知识库上下文）"""
        knowledge_context = self.knowledge.as_system_prompt()
        if not knowledge_context:
            knowledge_context = "（知识库未加载，使用通用安全知识）"
        return SYSTEM_PROMPT_TEMPLATE.format(knowledge_context=knowledge_context)

    @staticmethod
    def _build_user_prompt(article: dict) -> str:
        """构建单篇文章的 User Prompt"""
        category = article.get("category", "") or "未分类"
        summary = article.get("summary", "") or article.get("summary_cn", "") or "无"
        return USER_PROMPT_TEMPLATE.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            category=category,
            summary=summary,
            is_ai_security=article.get("is_ai_security", False),
            is_agent_security=article.get("is_agent_security", False),
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
