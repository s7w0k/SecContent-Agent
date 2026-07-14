"""
双维度打分 Agent (V2)

对 V2 6分类后的 PR 候选文章进行双维度评分：
  1. 产品能力相关度 (product_relevance: 0-100)
  2. 事件影响面与传播力 (event_impact: 0-100)

综合分 = product_relevance + event_impact，≥ 80 进入 PR 草稿生成。

与 V1 差异:
  - 评分维度: 产品相关度 + 事件影响力（替代 AI安全相关度 + 可报道性）
  - 阈值: ≥80（替代 ≥140）
  - 仅对 is_pr_eligible=True 的文章打分
  - 使用 V2 知识库（多文件）

特性:
  - System Prompt 注入 V2 产品知识库上下文
  - 并发打分控制（asyncio.Semaphore）
  - JSON 响应解析 + 降级处理
  - 分数范围校验 + V2 分类上下文注入

使用:
    from langchain_openai import ChatOpenAI
    from agent.scorer_v2 import ScoringAgentV2

    llm = ChatOpenAI(model="deepseek-chat", ...)
    scorer = ScoringAgentV2(llm=llm, knowledge=knowledge)
    scores = await scorer.score_batch(articles)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from agent.llm_wrapper import LLMWrapper
from agent.schemas import ScoreResultSchema
from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("backend.agent.scorer_v2")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DEFAULT_CONCURRENCY = 20
SCORE_MIN = 0
SCORE_MAX = 100
PR_THRESHOLD = 80  # V2: 综合分 ≥ 80 进入 PR 草稿
MAX_THRESHOLD_ADJUSTMENT = 10
THRESHOLD_STEP_PER_SIGNAL = 2
DEFAULT_TEMPERATURE = 0.1
MAX_RETRIES = 1

_TOO_HIGH_KEYWORDS = ("偏高", "过高", "打分高", "分数高", "too_high", "high")
_TOO_LOW_KEYWORDS = ("偏低", "过低", "打分低", "分数低", "too_low", "low")

SYSTEM_PROMPT_TEMPLATE = """你是一个智能体安全领域的技术情报分析师。
请根据产品知识库和文章内容，从以下两个维度对文章评分。

## 产品知识库
{knowledge_context}

## 评分维度

### 1. 产品能力相关度 (product_relevance: 0-100)
评估这个事件与我们产品的关系——产品能否解决它？能否蹭这个热点？

- **90-100**: 直接涉及产品核心能力（身份安全、Agent安全、MCP协议），产品能直接解决/参与
- **70-89**: 与产品能力有明确交集，可作为典型案例或营销素材
- **50-69**: 泛安全/泛AI事件，产品能部分关联
- **30-49**: 弱关联，仅作行业背景参考
- **0-29**: 与产品无关

### 2. 事件影响面与传播力 (event_impact: 0-100)
评估这个事件本身有多大的话题价值——是我们蹭它，还是找论据？

- **90-100**: 全球性/国家级重大事件，主流媒体广泛报道，行业标杆意义
- **70-89**: 行业内有较大影响，安全圈热传，可引发客户关注
- **50-69**: 细分领域有影响力，专业媒体覆盖
- **30-49**: 一般性报道，有一定参考价值
- **0-29**: 常规动态，无显著传播力

## 综合分
综合分 = 产品能力相关度 + 事件影响面与传播力（范围 0-200）

## 输出格式
严格输出 JSON，不要加代码块标记：
{{"product_relevance": 85, "event_impact": 70, "reason": "40字以内的打分理由", "tags": ["标签"]}}
"""

USER_PROMPT_TEMPLATE = """请对以下文章进行双维度评分：

**标题**: {title}
**来源**: {source}
**V2分类**: {category_v2}
**摘要**: {summary}
**正文前段**: {content}
"""


# ═══════════════════════════════════════════════════════════════
# ScoringAgentV2
# ═══════════════════════════════════════════════════════════════


class ScoringAgentV2:
    """双维度打分 Agent (V2)。

    对 V2 6分类后的 PR 候选文章进行：
      - 产品能力相关度 (product_relevance: 0-100)
      - 事件影响面与传播力 (event_impact: 0-100)
    综合分 ≥ 80 → 进入 PR 草稿生成。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        knowledge: Any,  # ProductKnowledge or KnowledgeLoader
        temperature: float = DEFAULT_TEMPERATURE,
        db: Any = None,
    ):
        self.llm = llm
        self.llm.temperature = temperature
        self.knowledge = knowledge
        self.db = db
        self.system_prompt = self._build_system_prompt()
        self.llm_wrapper = LLMWrapper(llm, db)

    # ── 公开接口 ──────────────────────────────────────────────

    async def score_single(
        self,
        article: dict | Any,
        *,
        threshold: int = PR_THRESHOLD,
        threshold_adjustment: int = 0,
        user_id: str = "",
        trace_id: str = "",
        task_id: str = "",
    ) -> dict:
        """单篇打分，返回包含 product_relevance / event_impact / pr_total_score 的 dict。"""
        art = (
            article
            if isinstance(article, dict)
            else (article.model_dump() if hasattr(article, "model_dump") else article)
        )
        return await self._score_with_llm(
            art,
            threshold=threshold,
            threshold_adjustment=threshold_adjustment,
            user_id=user_id,
            trace_id=trace_id,
            task_id=task_id,
        )

    async def score_batch(
        self,
        articles: list[dict | Any],
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        threshold: int = PR_THRESHOLD,
        threshold_adjustment: int = 0,
        user_id: str = "",
        trace_id: str = "",
        task_id: str = "",
    ) -> list[dict]:
        """批量并发打分。

        Args:
            articles: 文章列表（应只包含 is_pr_eligible=True 的 PR 候选文章）
            concurrency: 并发数

        Returns:
            打分结果列表（与输入顺序一致），每项含 _fallback 标记
        """
        if not articles:
            return []

        logger.info("Scoring %d articles (V2, parallel)", len(articles))
        sem = asyncio.Semaphore(concurrency)

        async def _score_one(art: dict) -> dict:
            async with sem:
                d = (
                    art
                    if isinstance(art, dict)
                    else (art.model_dump() if hasattr(art, "model_dump") else art)
                )
                return await self._score_with_llm(
                    d,
                    threshold=threshold,
                    threshold_adjustment=threshold_adjustment,
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                )

        results = await asyncio.gather(*[_score_one(a) for a in articles])
        rlist = list(results)
        ok = sum(1 for r in rlist if not r.get("_fallback"))
        pr = sum(1 for r in rlist if r.get("is_pr_candidate"))
        logger.info(
            "V2 Scored: %d/%d ok, %d PR candidates (≥%d)",
            ok,
            len(rlist),
            pr,
            threshold,
        )
        return rlist

    async def adjust_threshold(self, db: Any | None = None, *, user_id: str) -> dict:
        """根据文章打分反馈微调 PR 入选阈值。

        偏高反馈会提高阈值，偏低反馈会降低阈值；调整幅度限制在 ±10 分。
        """
        active_db = db if db is not None else self.db
        if active_db is None:
            return self._threshold_result(
                adjustment=0,
                feedback_count=0,
                directional_count=0,
            )

        try:
            cursor = active_db["feedbacks"].find(
                {
                    "user_id": user_id,
                    "status": "active",
                    "target_type": "article_score",
                }
            )
            feedbacks = await cursor.to_list(length=500)
        except Exception as exc:
            logger.warning("Failed to load score feedbacks for threshold adjustment: %s", exc)
            return self._threshold_result(
                adjustment=0,
                feedback_count=0,
                directional_count=0,
            )

        adjustment, directional_count = self.calculate_threshold_adjustment(feedbacks)
        return self._threshold_result(adjustment, len(feedbacks), directional_count)

    @staticmethod
    def _threshold_result(
        adjustment: int,
        feedback_count: int,
        directional_count: int,
    ) -> dict:
        return {
            "base_threshold": PR_THRESHOLD,
            "adjustment": adjustment,
            "threshold": PR_THRESHOLD + adjustment,
            "feedback_count": feedback_count,
            "directional_count": directional_count,
        }

    @classmethod
    def calculate_threshold_adjustment(cls, feedbacks: list[dict]) -> tuple[int, int]:
        """从打分反馈中计算阈值偏移量。"""
        signal = 0
        directional_count = 0
        for feedback in feedbacks:
            direction = cls._score_feedback_direction(feedback)
            if direction:
                signal += direction
                directional_count += 1

        adjustment = signal * THRESHOLD_STEP_PER_SIGNAL
        adjustment = max(-MAX_THRESHOLD_ADJUSTMENT, min(MAX_THRESHOLD_ADJUSTMENT, adjustment))
        return adjustment, directional_count

    @staticmethod
    def _score_feedback_direction(feedback: dict) -> int:
        text_parts = [
            str(feedback.get("comment", "")),
            " ".join(str(tag) for tag in feedback.get("tags", [])),
        ]
        dimensions = feedback.get("rating_dimensions") or {}
        if isinstance(dimensions, dict):
            text_parts.extend(f"{key}:{value}" for key, value in dimensions.items())
        text = " ".join(text_parts).lower()

        has_too_high = any(keyword in text for keyword in _TOO_HIGH_KEYWORDS)
        has_too_low = any(keyword in text for keyword in _TOO_LOW_KEYWORDS)
        if has_too_high and not has_too_low:
            return 1
        if has_too_low and not has_too_high:
            return -1
        return 0

    # ── Prompt 构建 ──────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """构建 System Prompt（含 V2 知识库上下文）。"""
        if hasattr(self.knowledge, "as_scoring_prompt"):
            knowledge_context = self.knowledge.as_scoring_prompt()
        elif hasattr(self.knowledge, "as_system_prompt"):
            knowledge_context = self.knowledge.as_system_prompt()
        else:
            knowledge_context = ""
        if not knowledge_context:
            knowledge_context = "（知识库未加载，使用通用安全知识）"
        return SYSTEM_PROMPT_TEMPLATE.format(knowledge_context=knowledge_context)

    @staticmethod
    def _build_user_prompt(article: dict) -> str:
        """构建 User Prompt（含 V2 分类标签）。"""
        category_v2 = article.get("category_v2", "") or "未分类"
        summary = article.get("summary_cn", "") or article.get("summary", "") or "无"
        content = (article.get("content_md", "") or article.get("summary", "") or "")[:800]

        return USER_PROMPT_TEMPLATE.format(
            title=article.get("title", ""),
            source=article.get("source", ""),
            category_v2=category_v2,
            summary=summary,
            content=content or "（暂无正文）",
        )

    # ── 核心打分逻辑 ──────────────────────────────────────────

    async def _score_with_llm(
        self,
        article: dict,
        *,
        threshold: int,
        threshold_adjustment: int,
        user_id: str = "",
        trace_id: str = "",
        task_id: str = "",
    ) -> dict:
        """调用 LLM 进行双维度打分（含重试和降级）。"""
        user_prompt = self._build_user_prompt(article)

        for attempt in range(MAX_RETRIES + 1):
            try:
                structured = await self.llm_wrapper.invoke_structured(
                    system_prompt=self.system_prompt,
                    user_prompt=user_prompt,
                    output_schema=ScoreResultSchema,
                    agent_type="scorer_v2",
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                )
                return self._enrich_result(
                    self._validate_and_fix(structured.model_dump()),
                    threshold=threshold,
                    threshold_adjustment=threshold_adjustment,
                )
            except Exception as e:
                if attempt == MAX_RETRIES:
                    return self._fallback_score(
                        str(e),
                        threshold=threshold,
                        threshold_adjustment=threshold_adjustment,
                    )

        return self._fallback_score(
            "max retries",
            threshold=threshold,
            threshold_adjustment=threshold_adjustment,
        )

    # ── 响应解析 ─────────────────────────────────────────────

    @staticmethod
    def _parse_response(text: str) -> dict:
        """从 LLM 响应中提取 JSON。

        支持: 1) ```json ``` 代码块 2) 纯 JSON 3) 文本中 JSON 对象
        """
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block:
            return json.loads(code_block.group(1).strip())

        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        obj_match = re.search(r"\{[^{}]*\}", text)
        if obj_match:
            try:
                return json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Cannot extract JSON from response: {text[:200]}")

    # ── 结果校验 ─────────────────────────────────────────────

    @staticmethod
    def _validate_and_fix(parsed: dict) -> dict:
        """校验并修正打分结果。

        确保:
          - product_relevance 在 0-100 范围内
          - event_impact 在 0-100 范围内
          - reason 不超长，tags 是列表
        """
        result: dict = {}

        result["product_relevance"] = max(
            SCORE_MIN, min(SCORE_MAX, int(parsed.get("product_relevance", 0)))
        )
        result["event_impact"] = max(SCORE_MIN, min(SCORE_MAX, int(parsed.get("event_impact", 0))))
        result["score_reason"] = str(parsed.get("reason", ""))[:200]

        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        result["tags"] = [str(t)[:50] for t in tags[:5]]

        return result

    @staticmethod
    def _enrich_result(
        validated: dict,
        threshold: int = PR_THRESHOLD,
        threshold_adjustment: int = 0,
    ) -> dict:
        """补充计算字段：pr_total_score, is_pr_candidate。"""
        validated["pr_total_score"] = validated["product_relevance"] + validated["event_impact"]
        validated["is_pr_candidate"] = validated["pr_total_score"] >= threshold
        validated["pr_threshold"] = threshold
        validated["threshold_adjustment"] = threshold_adjustment
        validated["_fallback"] = False
        return validated

    @staticmethod
    def _fallback_score(
        error: str = "",
        *,
        threshold: int = PR_THRESHOLD,
        threshold_adjustment: int = 0,
    ) -> dict:
        """打分失败时的降级结果。"""
        return {
            "product_relevance": 0,
            "event_impact": 0,
            "score_reason": f"Scoring failed: {error[:100]}",
            "tags": [],
            "pr_total_score": 0,
            "is_pr_candidate": False,
            "pr_threshold": threshold,
            "threshold_adjustment": threshold_adjustment,
            "_fallback": True,
        }
