"""
AI 安全文章分类器 — 使用 DeepSeek LLM 对文章进行安全话题分类。

分类维度:
  - is_ai_security: 是否 AI 安全相关
  - is_agent_security: 是否 Agent 安全相关
  - category: 具体分类标签（MCP协议漏洞 / 提示注入 / ...）
  - ai_relevance_score: AI 相关度评分 (0-100)
  - summary_cn: 中文摘要 (80-150字)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from enum import StrEnum

from crawler import NewsArticle
from openai import AsyncOpenAI

logger = logging.getLogger("mcp-crawl.classifier")


# ═══════════════════════════════════════════════════════════
# 分类标签
# ═══════════════════════════════════════════════════════════

class ArticleCategory(StrEnum):
    AI_SECURITY = "AI安全"
    AGENT_SECURITY = "Agent安全"
    MCP_PROTOCOL = "MCP协议漏洞"
    PROMPT_INJECTION = "提示注入"
    MODEL_ATTACK = "模型攻击"
    SUPPLY_CHAIN = "供应链安全"
    DATA_PRIVACY = "数据隐私"
    POLICY_COMPLIANCE = "政策合规"
    COMPETITOR = "竞品动态"
    TOOL_SECURITY = "工具安全"
    IDENTITY_AUTH = "身份认证"
    OTHER = "其他"


# ═══════════════════════════════════════════════════════════
# 分类结果
# ═══════════════════════════════════════════════════════════

class ClassifiedArticle:
    """分类后的文章"""

    __slots__ = (
        "ai_reason",
        "ai_relevance_score",
        "category",
        "classified_at",
        "content_md",
        "is_agent_security",
        "is_ai_security",
        "published_at",
        "source",
        "source_type",
        "summary",
        "summary_cn",
        "title",
        "url",
        "url_hash",
    )

    def __init__(
        self,
        *,
        title: str = "",
        url: str = "",
        url_hash: str = "",
        source: str = "",
        source_type: str = "overseas_news",
        published_at: str = "",
        summary: str = "",
        content_md: str = "",
        is_ai_security: bool = False,
        is_agent_security: bool = False,
        category: str = "",
        ai_relevance_score: int = 0,
        ai_reason: str = "",
        summary_cn: str = "",
        classified_at: str | None = None,
    ):
        self.title = title
        self.url = url
        self.url_hash = url_hash
        self.source = source
        self.source_type = source_type
        self.published_at = published_at
        self.summary = summary
        self.content_md = content_md
        self.is_ai_security = is_ai_security
        self.is_agent_security = is_agent_security
        self.category = category
        self.ai_relevance_score = ai_relevance_score
        self.ai_reason = ai_reason
        self.summary_cn = summary_cn
        self.classified_at = classified_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def from_article(cls, article: NewsArticle) -> ClassifiedArticle:
        """从未分类的原文创建（默认值）"""
        return cls(
            title=article.title,
            url=article.url,
            url_hash=article.url_hash,
            source=article.source,
            source_type=article.source_type,
            published_at=article.published_at.strftime("%Y-%m-%d") if article.published_at else "",
            summary=article.summary,
            content_md=article.content_md,
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "url_hash": self.url_hash,
            "source": self.source,
            "source_type": self.source_type,
            "published_at": self.published_at,
            "summary": self.summary,
            "content_md": self.content_md,
            "is_ai_security": self.is_ai_security,
            "is_agent_security": self.is_agent_security,
            "category": self.category,
            "ai_relevance_score": self.ai_relevance_score,
            "ai_reason": self.ai_reason,
            "summary_cn": self.summary_cn,
            "classified_at": self.classified_at,
        }


# ═══════════════════════════════════════════════════════════
# 分类器
# ═══════════════════════════════════════════════════════════

class AISecurityClassifier:
    """使用 DeepSeek LLM 进行 AI/Agent 安全话题分类"""

    CLASSIFY_PROMPT = """你是一个网络安全内容分类专家。请判断以下安全新闻是否属于 **AI安全** 或 **智能体(Agent)安全** 话题。

## AI安全 / Agent安全的定义

AI安全包括：
- AI模型安全（对抗攻击、数据投毒、模型窃取、越狱/Prompt注入）
- AI基础设施安全（向量数据库暴露、GPU集群安全、训练管道安全）
- AI供应链安全（模型供应链攻击、开源模型后门、HuggingFace投毒）
- AI应用安全（AI驱动的攻击、深度伪造滥用、AI辅助的社会工程）
- AI治理与合规（AI法规、AI红队测试、AI安全框架）
- AI安全工具/平台（AI漏洞发现、AI驱动的防御系统）

Agent安全包括：
- Agent身份与权限（Agent越权、身份冒用、凭证泄露、MCP认证缺陷）
- Agent工具调用安全（工具滥用、工具投毒、间接Prompt注入）
- Agent自主行为风险（未授权操作、资源滥用、目标偏离）
- Agent协议与供应链安全（MCP/A2A协议漏洞、Agent插件安全）

## 排除项（以下不算AI/Agent安全）：
- AI公司的通用融资/人事/合作新闻（不涉及具体安全议题）
- 用AI做传统安全检测（AI for Security，非AI Security）
- 传统勒索软件、钓鱼、漏洞攻击（不涉及AI组件或AI目标）

## 输出格式
严格只输出 JSON 数组，每篇文章需包含中文摘要（summary_cn，80-150字）：

```json
[
  {"index":0, "is_ai_security":true, "is_agent_security":true, "category":"MCP协议漏洞", "ai_relevance_score":92, "reason":"MCP服务器认证缺陷属于Agent基础设施安全", "summary_cn":"安全研究人员发现MCP协议存在身份验证绕过漏洞..."},
  {"index":1, "is_ai_security":false, "is_agent_security":false, "category":"", "ai_relevance_score":0, "reason":"传统勒索软件，不涉及AI", "summary_cn":""}
]
```"""

    _UNCATEGORIZED_CATEGORIES: tuple[str, ...] = ("其他", "未分类", "")

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
    ):
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def classify(
        self, articles: list[NewsArticle], batch_size: int = 25
    ) -> list[ClassifiedArticle]:
        """批量异步分类文章"""
        results: list[ClassifiedArticle] = []

        for i in range(0, len(articles), batch_size):
            batch = articles[i : i + batch_size]
            batch_num = i // batch_size + 1
            logger.info("Classifying batch %d (%d articles)...", batch_num, len(batch))

            # 构建批次文本
            text = ""
            for idx, art in enumerate(batch):
                d = art.published_at.strftime("%Y-%m-%d") if art.published_at else "?"
                text += f"[{idx}] {art.title}\n    来源:{art.source} 日期:{d}\n    摘要:{art.summary[:200]}\n\n"

            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.CLASSIFY_PROMPT},
                        {"role": "user", "content": f"请分类以下安全新闻：\n\n{text}"},
                    ],
                    temperature=0.1,
                )
            except Exception as e:
                logger.error("  API error: %s", e)
                for art in batch:
                    results.append(ClassifiedArticle.from_article(art))
                continue

            content = resp.choices[0].message.content or ""
            tokens = resp.usage.total_tokens if resp.usage else 0
            logger.info("  %d tokens", tokens)

            cls_map = self._parse_json(content)
            for idx, art in enumerate(batch):
                c = cls_map.get(idx, {})
                results.append(
                    ClassifiedArticle(
                        title=art.title,
                        url=art.url,
                        url_hash=art.url_hash,
                        source=art.source,
                        source_type=art.source_type,
                        published_at=art.published_at.strftime("%Y-%m-%d") if art.published_at else "",
                        summary=art.summary,
                        content_md=art.content_md,
                        is_ai_security=c.get("is_ai_security", False),
                        is_agent_security=c.get("is_agent_security", False),
                        category=c.get("category", ""),
                        ai_relevance_score=c.get("ai_relevance_score", 0),
                        ai_reason=c.get("reason", ""),
                        summary_cn=c.get("summary_cn", ""),
                    )
                )

        ai_n = sum(1 for r in results if r.is_ai_security)
        ag_n = sum(1 for r in results if r.is_agent_security)
        logger.info("Done: %d total, %d AI security, %d Agent security", len(results), ai_n, ag_n)
        return results

    @staticmethod
    def _parse_json(text: str) -> dict[int, dict]:
        """从 LLM 响应中提取 JSON 数组，转为 {index: item} 字典"""
        # 优先匹配 ```json ... ``` 代码块
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, list):
                    return {item.pop("index", i): item for i, item in enumerate(data)}
            except json.JSONDecodeError:
                pass

        # 回退：纯文本中的 JSON 数组
        text = text.strip()
        if text.startswith("["):
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    return {item.pop("index", i): item for i, item in enumerate(data)}
            except json.JSONDecodeError:
                pass

        logger.warning("  JSON parse failed")
        return {}
