"""
6分类 Agent — 安全情报文章智能分类

将文章归入 6 个预定义类别之一，支持单篇和批量分类。

6 类别:
  1. 爆点事件 — 重大漏洞/攻击/数据泄露等突发安全事件 → 进入PR
  2. 法律法规/监管动态 — 国内外安全法规、合规政策更新 → 进入PR
  3. AI技术重大进展 — AI/Agent安全领域技术突破 → 进入PR
  4. 国内外竞品信息 — 友商产品、融资、合作动态 → 不进PR
  5. 运营商/行业事件 — 电信/金融/能源等行业安全事件 → 不进PR
  6. 学术/会展/高校 — 论文、会议、产学研 → 不进PR

特性:
  - System Prompt 定义每类关键词和判断标准
  - LLM JSON 响应解析 + 类别校验
  - 并发批量分类（asyncio.Semaphore）
  - 降级处理（LLM 异常时自动归入默认类）
  - 类别白名单校验（非6类则降级）

使用:
    from langchain_openai import ChatOpenAI
    from agent.classifier_v2 import ClassifierV2

    llm = ChatOpenAI(model="deepseek-chat", api_key="sk-xxx", base_url="https://api.deepseek.com")
    classifier = ClassifierV2(llm=llm)
    results = await classifier.classify_batch(articles)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from enum import StrEnum
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("backend.agent.classifier_v2")

# ═══════════════════════════════════════════════════════════════
# 6 分类枚举
# ═══════════════════════════════════════════════════════════════


class CategoryV2(StrEnum):
    """6 分类标签（值 = 中文类别名）"""

    BREAKING_EVENT = "爆点事件"            # 重大安全事件
    LAW_AND_REGULATION = "法律法规/监管动态"  # 法规合规
    AI_TECH_PROGRESS = "AI技术重大进展"     # AI/Agent技术突破
    COMPETITOR = "国内外竞品信息"            # 友商动态
    INDUSTRY_EVENT = "运营商/行业事件"       # 行业安全事件
    ACADEMIC = "学术/会展/高校"              # 学术前沿
    NOT_RELEVANT = "不相关"                  # 与AI/Agent安全无关

    @classmethod
    def valid_values(cls) -> set[str]:
        """返回所有有效类别名的集合"""
        return {m.value for m in cls if m != cls.NOT_RELEVANT}

    @classmethod
    def pr_eligible(cls) -> set[str]:
        """返回可进入 PR 流程的类别"""
        return {
            cls.BREAKING_EVENT.value,
            cls.LAW_AND_REGULATION.value,
            cls.AI_TECH_PROGRESS.value,
        }

    @classmethod
    def default(cls) -> str:
        """降级时使用的默认类别"""
        return cls.NOT_RELEVANT.value


# ═══════════════════════════════════════════════════════════════
# 分类结果数据类
# ═══════════════════════════════════════════════════════════════


class ClassifyResultV2:
    """单篇分类结果"""

    __slots__ = ("_error", "_fallback", "category", "confidence", "reason")

    def __init__(
        self,
        category: str = "",
        confidence: int = 0,
        reason: str = "",
        fallback: bool = False,
        error: str = "",
    ):
        self.category = category
        self.confidence = confidence
        self.reason = reason
        self._fallback = fallback
        self._error = error

    @property
    def is_pr_eligible(self) -> bool:
        """该文章是否应进入 PR 流程"""
        return self.category in CategoryV2.pr_eligible()

    @property
    def is_fallback(self) -> bool:
        """是否降级结果"""
        return self._fallback

    def to_dict(self) -> dict:
        return {
            "category_v2": self.category,
            "category_v2_confidence": self.confidence,
            "category_v2_reason": self.reason,
            "category_v2_fallback": self._fallback,
            "is_pr_eligible": self.is_pr_eligible,
        }


# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

DEFAULT_CONCURRENCY = 20
DEFAULT_TEMPERATURE = 0.1  # 低温度确保分类一致性
MAX_RETRIES = 1
CONFIDENCE_MIN = 0
CONFIDENCE_MAX = 100

# ── System Prompt ──────────────────────────────────────────

SYSTEM_PROMPT = """你是一个安全情报分析师。请按两步完成分类：

## 第一步：判断是否与AI/Agent安全相关

首先判断文章是否涉及以下任一领域，若不涉及则直接返回"不相关"：

**相关领域（满足任一即进入第二步）：**
- AI安全：大模型安全、AI模型攻击/防御、AI基础设施安全
- Agent安全：智能体身份、MCP协议、A2A协议、工具调用安全、Agent越权
- 安全漏洞/事件：漏洞披露（CVE）、APT攻击、数据泄露、勒索软件、0day
- 网络安全法规：GDPR、网络安全法、数据安全法、等保、合规监管
- AI/Agent技术突破：新模型发布、Agent框架重大更新、MCP/A2A协议进展

**不相关（直接返回"不相关"）：**
- 纯AI公司融资/人事/财报（不涉及安全议题）
- 通用IT新闻、消费电子、游戏娱乐
- 非安全类的学术论文、教育培训
- 传统行业新闻（金融/医疗/制造无安全角度）

## 第二步：若相关，归入以下6类之一

## 分类定义

### 1. 爆点事件
重大突发性安全事件，引起行业广泛关注。
- 典型特征：漏洞披露（CVE/CNNVD编号）、APT攻击活动、0day漏洞、勒索软件攻击、
  数据泄露事件、重大网络攻击、供应链攻击
- 关键词：漏洞、攻击、泄露、勒索、0day、APT、黑客、入侵
- 判断标准：事件本身具有新闻爆点属性，安全圈/科技媒体广泛报道

### 2. 法律法规/监管动态
国内外网络安全相关法规、政策、标准的发布或更新。
- 典型特征：新法规出台、政策解读、合规检查、监管处罚、标准发布
- 关键词：GDPR、网络安全法、数据安全法、等保、合规、监管、条例、
  个人信息保护、关基
- 判断标准：涉及政策法规层面，对行业合规有指导意义

### 3. AI技术重大进展
AI/Agent安全领域的重要技术突破或产品发布。
- 典型特征：LLM新模型发布、Agent框架重大更新、MCP/A2A协议进展、
  AI安全工具/平台发布、重要论文落地应用
- 关键词：大模型、Agent、MCP、A2A、LLM、GPT、Claude、深度学习、
  多模态、RAG、工具调用
- 判断标准：技术层面有实质性突破或里程碑意义

### 4. 国内外竞品信息
友商（安全厂商/AI厂商）的商业动态。
- 典型特征：产品发布/更新、融资消息、战略合作、财报、人事变动
- 关键词：融资、发布、合作、上市、财报、收购、产品、解决方案
- 判断标准：以厂商商业动态为主，非技术性安全事件

### 5. 运营商/行业事件
重点行业（电信运营商、金融、能源、政务等）的安全事件或采购动态。
- 典型特征：运营商安全建设、行业安全项目、政企采购、行业安全报告
- 关键词：中国移动、中国电信、中国联通、金融安全、能源安全、
  政务云、智慧城市、工业互联网
- 判断标准：以行业/垂直领域视角为主

### 6. 学术/会展/高校
学术论文发表、安全会议、高校合作、产学研动态。
- 典型特征：顶会论文、学术期刊、安全竞赛、高校实验室成果、培训认证
- 关键词：论文、会议、学术、高校、大学、实验室、竞赛、CCS、
  USENIX、S&P、NDSS
- 判断标准：以学术/教育视角为主，非产业落地

## 输出格式
严格按 JSON 格式输出，不要添加代码块标记：
{"category": "类别名", "confidence": 0-100的整数, "reason": "20字以内的分类理由"}

**category 可选值（7选1）：**
- "不相关" — 与AI/Agent安全无关
- "爆点事件"
- "法律法规/监管动态"
- "AI技术重大进展"
- "国内外竞品信息"
- "运营商/行业事件"
- "学术/会展/高校"

## 注意事项
- 若文章与AI/Agent安全完全无关，category 必须返回 "不相关"，confidence 给 90+
- 若有关但类别模糊，选最接近的，confidence 适当降低
- 不得自创上述7个值以外的类别
"""

# ── User Prompt 模板 ──────────────────────────────────────

USER_PROMPT_TEMPLATE = """请对以下文章进行6分类：

**标题**: {title}
**来源**: {source}
**摘要**: {summary}
**正文前段**: {content}
"""


# ═══════════════════════════════════════════════════════════════
# ClassifierV2
# ═══════════════════════════════════════════════════════════════


class ClassifierV2:
    """6 分类 Agent。

    使用 LLM 对安全情报文章进行6类别归类，
    支持单篇和批量并发分类。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        temperature: float = DEFAULT_TEMPERATURE,
    ):
        self.llm = llm
        self.llm.temperature = temperature

    # ── 公开接口 ──────────────────────────────────────────────

    async def classify_single(self, article: dict | Any) -> ClassifyResultV2:
        """对单篇文章进行分类（LLM 自行判断安全相关性 + 6分类）。

        Args:
            article: 文章数据（dict 或 Pydantic model）

        Returns:
            ClassifyResultV2 分类结果
        """
        art = article if isinstance(article, dict) else (
            article.model_dump() if hasattr(article, "model_dump") else article
        )
        return await self._classify_with_llm(art)

    async def classify_batch(
        self,
        articles: list[dict | Any],
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> list[ClassifyResultV2]:
        """批量并发分类文章。

        Args:
            articles: 文章列表
            concurrency: 并发数

        Returns:
            分类结果列表（与输入顺序一致）
        """
        if not articles:
            return []

        logger.info("Batch classifying %d articles (concurrency=%d)", len(articles), concurrency)
        sem = asyncio.Semaphore(concurrency)

        async def _classify_one(art: dict) -> ClassifyResultV2:
            async with sem:
                d = art if isinstance(art, dict) else (
                    art.model_dump() if hasattr(art, "model_dump") else art
                )
                return await self._classify_with_llm(d)

        results = await asyncio.gather(*[_classify_one(a) for a in articles])
        rlist = list(results)
        ok_count = sum(1 for r in rlist if not r.is_fallback)
        pr_count = sum(1 for r in rlist if r.is_pr_eligible)
        not_relevant = sum(1 for r in rlist if r.category == CategoryV2.NOT_RELEVANT.value)
        logger.info(
            "Classified: %d/%d ok, %d PR-eligible, %d not-relevant",
            ok_count, len(rlist), pr_count, not_relevant,
        )
        return rlist

    # ── 核心分类逻辑 ──────────────────────────────────────────

    async def _classify_with_llm(self, article: dict) -> ClassifyResultV2:
        """调用 LLM 进行6分类（含重试和降级）。"""
        user_prompt = self._build_user_prompt(article)

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await self.llm.ainvoke([
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=user_prompt),
                ])
                raw = response.content if hasattr(response, "content") else str(response)
                parsed = self._parse_response(raw)
                validated = self._validate_and_fix(parsed)
                return self._build_result(validated)
            except Exception as e:
                if attempt == MAX_RETRIES:
                    logger.warning(
                        "Classification failed after %d retries: %s",
                        MAX_RETRIES + 1, e,
                    )
                    return self._fallback_result(str(e))

        return self._fallback_result("max retries")

    # ── Prompt 构建 ──────────────────────────────────────────

    @staticmethod
    def _build_user_prompt(article: dict) -> str:
        """构建 User Prompt（文章标题+摘要+正文前段）。"""
        title = article.get("title", "")
        source = article.get("source", "")
        summary = (
            article.get("summary_cn", "")
            or article.get("summary", "")
            or "无"
        )
        content = (
            article.get("content_md", "")
            or article.get("summary", "")
            or ""
        )[:800]

        return USER_PROMPT_TEMPLATE.format(
            title=title,
            source=source,
            summary=summary,
            content=content or "（暂无正文）",
        )

    # ── 响应解析 ─────────────────────────────────────────────

    @staticmethod
    def _parse_response(text: str) -> dict:
        """从 LLM 响应中提取 JSON。

        支持的格式:
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

    # ── 结果校验 ─────────────────────────────────────────────

    @staticmethod
    def _validate_and_fix(parsed: dict) -> dict:
        """校验并修正分类结果。

        确保:
          - category 必须是6类之一
          - confidence 在 0-100 范围内
          - reason 不超长
        """
        result: dict = {}

        # category — 必须白名单校验（7个有效值：6类 + 不相关）
        category = str(parsed.get("category", "")).strip()
        valid_categories = CategoryV2.valid_values() | {CategoryV2.NOT_RELEVANT.value}
        if category not in valid_categories:
            logger.debug(
                "Invalid category '%s', falling back to default", category,
            )
            category = CategoryV2.default()
        result["category"] = category

        # confidence — 范围限制
        try:
            confidence = int(parsed.get("confidence", 50))
        except (ValueError, TypeError):
            confidence = 50
        result["confidence"] = max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, confidence))

        # reason — 截断
        result["reason"] = str(parsed.get("reason", ""))[:100]

        return result

    @staticmethod
    def _build_result(validated: dict) -> ClassifyResultV2:
        """从校验后的 dict 构造 ClassifyResultV2。"""
        return ClassifyResultV2(
            category=validated["category"],
            confidence=validated["confidence"],
            reason=validated["reason"],
            fallback=False,
        )

    @staticmethod
    def _fallback_result(error: str = "") -> ClassifyResultV2:
        """LLM 调用失败时的降级结果。"""
        return ClassifyResultV2(
            category=CategoryV2.default(),
            confidence=0,
            reason=f"分类失败(降级): {error[:80]}",
            fallback=True,
            error=error[:200],
        )
