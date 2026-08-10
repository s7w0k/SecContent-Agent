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
from agent.schemas import SingleProductScoreSchema
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
请根据产品知识库和文章内容，对指定产品评估相关性，并评估事件影响面。

## 产品知识库
{knowledge_context}

## 待评产品
{product_list}

## 评分维度

### 1. 该产品能力相关度 (relevance: 0-100)
评估这个事件与上述产品的关系--产品能否解决它？能否蹭这个热点？

- **90-100**: 直接涉及产品核心能力，产品能直接解决/参与
- **70-89**: 与产品能力有明确交集，可作为典型案例或营销素材
- **50-69**: 泛安全/泛AI事件，产品能部分关联
- **30-49**: 弱关联，仅作行业背景参考
- **0-29**: 与产品无关

### 2. 事件影响面与传播力 (event_impact: 0-100)
评估这个事件本身有多大的话题价值。

- **90-100**: 全球性/国家级重大事件，主流媒体广泛报道
- **70-89**: 行业内有较大影响，安全圈热传
- **50-69**: 细分领域有影响力，专业媒体覆盖
- **30-49**: 一般性报道，有一定参考价值
- **0-29**: 常规动态，无显著传播力

## 输出格式
严格输出 JSON，不要加代码块标记：
{{"relevance": 85, "event_impact": 70, "reason": "40字以内的打分理由"}}
"""

USER_PROMPT_TEMPLATE = """请对以下文章进行评分：

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

    def refresh_prompt(self) -> None:
        """重建 System Prompt（发布后热刷新用）。

        不改变 LLM、评分维度和阈值，只重新从当前 knowledge 读取并拼接 Prompt。
        """
        self.system_prompt = self._build_system_prompt()
        logger.info("ScoringAgentV2 prompt refreshed")

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
        product_list_override: str | None = None,
        products: list[dict] | None = None,
    ) -> dict:
        """单篇打分，按产品并发调用 LLM。

        Args:
            product_list_override: 直接传入产品列表文本（旧模式，单次调用）。
            products: 产品列表 [{product_id, product_name}]，每个产品单独并发评分。
        """
        art = (
            article
            if isinstance(article, dict)
            else (article.model_dump() if hasattr(article, "model_dump") else article)
        )

        if products:
            return await self._score_concurrent(
                art,
                products=products,
                threshold=threshold,
                threshold_adjustment=threshold_adjustment,
                user_id=user_id,
                trace_id=trace_id,
                task_id=task_id,
            )

        return await self._score_with_llm(
            art,
            threshold=threshold,
            threshold_adjustment=threshold_adjustment,
            user_id=user_id,
            trace_id=trace_id,
            task_id=task_id,
            product_list_override=product_list_override,
        )

    async def _score_concurrent(
        self,
        article: dict,
        *,
        products: list[dict],
        threshold: int = PR_THRESHOLD,
        threshold_adjustment: int = 0,
        user_id: str = "",
        trace_id: str = "",
        task_id: str = "",
    ) -> dict:
        """对多个产品并发评分，每个产品一次 LLM 调用。"""
        sem = asyncio.Semaphore(5)

        async def score_one_product(product: dict) -> dict:
            async with sem:
                return await self._score_single_product(
                    article,
                    product_id=product["product_id"],
                    product_name=product["product_name"],
                    threshold=threshold,
                    threshold_adjustment=threshold_adjustment,
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                )

        results = await asyncio.gather(*[score_one_product(p) for p in products])

        # 聚合结果
        product_scores = []
        max_relevance = 0
        event_impact = 0
        best_reason = ""
        for r in results:
            if r.get("_fallback"):
                continue
            product_scores.append({
                "product_id": r["product_id"],
                "product_name": r["product_name"],
                "score": r["relevance"],
                "reason": r.get("reason", ""),
            })
            if r["relevance"] > max_relevance:
                max_relevance = r["relevance"]
                best_reason = r.get("reason", "")
            if r.get("event_impact", 0) > event_impact:
                event_impact = r["event_impact"]

        pr_total = max_relevance + event_impact
        return {
            "product_relevance": max_relevance,
            "event_impact": event_impact,
            "pr_total_score": pr_total,
            "score_reason": best_reason,
            "product_scores": product_scores,
            "is_pr_candidate": pr_total >= threshold,
            "pr_threshold": threshold,
            "threshold_adjustment": threshold_adjustment,
            "_fallback": len(product_scores) == 0,
        }

    async def _score_single_product(
        self,
        article: dict,
        *,
        product_id: str,
        product_name: str,
        threshold: int = PR_THRESHOLD,
        threshold_adjustment: int = 0,
        user_id: str = "",
        trace_id: str = "",
        task_id: str = "",
    ) -> dict:
        """对单个产品评分（一次 LLM 调用）。"""
        system_prompt, context_meta = await self._build_system_prompt_for_product(
            product_id, product_name, user_id=user_id
        )
        user_prompt = self._build_user_prompt(article)

        for attempt in range(MAX_RETRIES + 1):
            try:
                structured = await self.llm_wrapper.invoke_structured(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_schema=SingleProductScoreSchema,
                    agent_type="scorer_v2",
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                    context_meta=context_meta or None,
                )
                data = structured.model_dump()
                return {
                    "product_id": product_id,
                    "product_name": product_name,
                    "relevance": max(SCORE_MIN, min(SCORE_MAX, int(data.get("relevance", 0)))),
                    "event_impact": max(SCORE_MIN, min(SCORE_MAX, int(data.get("event_impact", 0)))),
                    "reason": str(data.get("reason", ""))[:200],
                    "_fallback": False,
                }
            except Exception as e:
                if attempt == MAX_RETRIES:
                    return {
                        "product_id": product_id,
                        "product_name": product_name,
                        "relevance": 0,
                        "event_impact": 0,
                        "reason": f"Failed: {e!s}"[:200],
                        "_fallback": True,
                    }

        return {
            "product_id": product_id,
            "product_name": product_name,
            "relevance": 0,
            "event_impact": 0,
            "reason": "max retries",
            "_fallback": True,
        }

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
        """构建 System Prompt（含 V2 知识库上下文和产品列表）。"""
        if hasattr(self.knowledge, "as_scoring_prompt"):
            knowledge_context = self.knowledge.as_scoring_prompt()
        elif hasattr(self.knowledge, "as_system_prompt"):
            knowledge_context = self.knowledge.as_system_prompt()
        else:
            knowledge_context = ""
        if not knowledge_context:
            knowledge_context = "（知识库未加载，使用通用安全知识）"

        # 构建产品列表
        product_list = self._build_product_list()

        return SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_context=knowledge_context,
            product_list=product_list,
        )

    @staticmethod
    def _build_product_list() -> str:
        """构建待评产品列表文本。"""
        try:
            from agent.product_catalog import _PRODUCTS

            products = [p for p in _PRODUCTS if p.published]
            if not products:
                return "（无可用产品，请综合评估）"
            lines = []
            for p in products:
                lines.append(f"- product_id: {p.product_id}, product_name: {p.name}")
            return "\n".join(lines)
        except Exception:
            return "（产品目录未加载，请综合评估）"

    @staticmethod
    def _build_product_list_for_ids(product_ids: list[str]) -> str:
        """构建指定产品的列表文本。"""
        try:
            from agent.product_catalog import _PRODUCTS

            products = [p for p in _PRODUCTS if p.product_id in product_ids]
            if not products:
                return ScoringAgentV2._build_product_list()
            lines = []
            for p in products:
                lines.append(f"- product_id: {p.product_id}, product_name: {p.name}")
            return "\n".join(lines)
        except Exception:
            return ScoringAgentV2._build_product_list()

    def _build_system_prompt_with_text(self, product_list_text: str) -> str:
        """构建包含指定产品列表文本的系统提示词。"""
        if hasattr(self.knowledge, "as_scoring_prompt"):
            knowledge_context = self.knowledge.as_scoring_prompt()
        elif hasattr(self.knowledge, "as_system_prompt"):
            knowledge_context = self.knowledge.as_system_prompt()
        else:
            knowledge_context = ""
        if not knowledge_context:
            knowledge_context = "（知识库未加载，使用通用安全知识）"
        return SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_context=knowledge_context,
            product_list=product_list_text,
        )

    async def _build_system_prompt_for_product(
        self,
        product_id: str,
        product_name: str,
        *,
        user_id: str = "",
    ) -> tuple[str, dict]:
        """按产品构建系统提示词。

        只注入当前待评产品的知识：
          - 全局产品：读取该产品 knowledge_root 下的 overview.md / market-brief.md
          - 用户级产品：从 user_knowledge_entries 读取该用户该产品的知识条目
          - 共享参考文件（hot-event-playbook 等）作为兜底上下文

        知识切片解析失败时回退到全局评分知识。

        返回 (system_prompt, context_telemetry)。telemetry 含
        context_plan_hash/skill_versions/knowledge_snapshot/source_ids，
        供 LLM 日志记录；off/shadow 模式下内容仍走旧路径。
        """
        knowledge_context = ""
        telemetry: dict = {}
        mode = "off"
        if self.db is not None:
            try:
                from agent.context_bridge import ContextBridge
                from agent.context_cache import get_context_cache

                try:
                    from config import get_settings

                    settings = get_settings()
                    base_dir = settings.KNOWLEDGE_BASE_DIR
                except Exception:
                    settings = None
                    base_dir = "/app/docs"

                bridge = ContextBridge(
                    db=self.db,
                    settings=settings,
                    knowledge_base_dir=base_dir,
                    cache=get_context_cache(settings.CONTEXT_CACHE_TTL_SECONDS)
                    if settings is not None and settings.KNOWLEDGE_SKILLS_ENABLED
                    else None,
                )
                mode = bridge.effective_mode(user_id)
                if mode != "off":
                    result = await bridge.build_plan(
                        purpose="score",
                        user_id=user_id,
                        products=[product_id],
                        model_id=(settings.DEEPSEEK_MODEL if settings else "deepseek-chat"),
                    )
                    if result is not None:
                        telemetry = result.telemetry
                        telemetry["mode"] = mode
                        if mode == "active":
                            knowledge_context = result.plan.rendered()
            except Exception as exc:
                logger.warning(
                    "ContextManager build failed for product %s: %s", product_id, exc
                )

        if not knowledge_context:
            # 旧路径：切片解析 + 全局评分知识兜底
            if self.db is not None:
                try:
                    from agent.knowledge_slice import KnowledgeSliceResolver

                    try:
                        from config import get_settings

                        base_dir = get_settings().KNOWLEDGE_BASE_DIR
                    except Exception:
                        base_dir = "/app/docs"

                    resolver = KnowledgeSliceResolver(base_dir, db=self.db)
                    slice_result = await resolver.resolve(
                        purpose="score",
                        product_ids=[product_id],
                        user_id=user_id or None,
                    )
                    knowledge_context = slice_result.content
                except Exception as exc:
                    logger.warning(
                        "Knowledge slice resolve failed for product %s: %s", product_id, exc
                    )

            if not knowledge_context:
                if hasattr(self.knowledge, "as_scoring_prompt"):
                    knowledge_context = self.knowledge.as_scoring_prompt()
                elif hasattr(self.knowledge, "as_system_prompt"):
                    knowledge_context = self.knowledge.as_system_prompt()
            if not knowledge_context:
                knowledge_context = "（知识库未加载，使用通用安全知识）"

        product_list_text = f"- product_id: {product_id}, product_name: {product_name}"
        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            knowledge_context=knowledge_context,
            product_list=product_list_text,
        )
        if mode != "off" and telemetry:
            telemetry["context_plan_hash"] = telemetry.get("context_plan_hash") or ""
            logger.info(
                "[scorer-v2] purpose=score product=%s mode=%s plan_hash=%s tokens=%d",
                product_id,
                mode,
                telemetry.get("context_plan_hash", "")[:16],
                telemetry.get("total_tokens", 0),
            )
        return prompt, telemetry

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
        product_list_override: str | None = None,
    ) -> dict:
        """调用 LLM 进行双维度打分（含重试和降级）。"""
        # 构建系统提示词
        if product_list_override is not None:
            system_prompt = self._build_system_prompt_with_text(product_list_override)
        else:
            system_prompt = self.system_prompt
        user_prompt = self._build_user_prompt(article)

        for attempt in range(MAX_RETRIES + 1):
            try:
                structured = await self.llm_wrapper.invoke_structured(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_schema=SingleProductScoreSchema,
                    agent_type="scorer_v2",
                    user_id=user_id,
                    trace_id=trace_id,
                    task_id=task_id,
                )
                data = structured.model_dump()
                relevance = max(SCORE_MIN, min(SCORE_MAX, int(data.get("relevance", 0))))
                event_impact = max(SCORE_MIN, min(SCORE_MAX, int(data.get("event_impact", 0))))
                reason = str(data.get("reason", ""))[:200]
                pr_total = relevance + event_impact
                return {
                    "product_relevance": relevance,
                    "event_impact": event_impact,
                    "pr_total_score": pr_total,
                    "score_reason": reason,
                    "product_scores": [],
                    "is_pr_candidate": pr_total >= threshold,
                    "pr_threshold": threshold,
                    "threshold_adjustment": threshold_adjustment,
                    "_fallback": False,
                }
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

        支持: 1) ```json ``` 代码块 2) 纯 JSON 3) 文本中 JSON 对象（含嵌套）
        """
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block:
            return json.loads(code_block.group(1).strip())

        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # 匹配最外层花括号（支持嵌套，如 product_scores 数组中的对象）
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            candidate = text[first_brace : last_brace + 1]
            try:
                return json.loads(candidate)
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
          - product_scores 是有效的列表
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

        # 校验 product_scores
        product_scores = parsed.get("product_scores", [])
        if not isinstance(product_scores, list):
            product_scores = []
        validated_ps = []
        for ps in product_scores[:10]:
            if not isinstance(ps, dict):
                continue
            try:
                score = max(0, min(100, int(ps.get("score", 0))))
            except (TypeError, ValueError):
                score = 0
            validated_ps.append({
                "product_id": str(ps.get("product_id", ""))[:100],
                "product_name": str(ps.get("product_name", ""))[:100],
                "score": score,
                "reason": str(ps.get("reason", ""))[:100],
            })
        result["product_scores"] = validated_ps

        # 如果 product_scores 非空，取最高分作为 product_relevance
        if validated_ps:
            max_score = max(ps["score"] for ps in validated_ps)
            result["product_relevance"] = max(result["product_relevance"], max_score)

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
            "product_scores": [],
            "pr_total_score": 0,
            "is_pr_candidate": False,
            "pr_threshold": threshold,
            "threshold_adjustment": threshold_adjustment,
            "_fallback": True,
        }
