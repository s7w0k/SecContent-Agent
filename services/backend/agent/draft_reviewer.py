"""只对照输入原文检查稿件事实与宣传话术。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from models.draft_review import ISSUE_SEVERITIES, DraftReview, DraftReviewIssue

logger = logging.getLogger("backend.agent.draft_reviewer")

ABSOLUTE_WORDS: tuple[str, ...] = (
    "业内第一",
    "行业第一",
    "唯一",
    "最强",
    "最佳",
    "顶级",
    "首创",
    "遥遥领先",
    "全国领先",
    "全球领先",
)

COMPARISON_WORDS: tuple[str, ...] = (
    "比",
    "领先",
    "超越",
    "取代",
    "碾压",
    "吊打",
)

GUARANTEE_WORDS: tuple[str, ...] = (
    "100%",
    "百分之百",
    "零风险",
    "彻底杜绝",
    "永久防护",
    "绝不失效",
    "完全解决",
    "所有攻击",
)

SEVERITY_ORDER = {severity: index for index, severity in enumerate(ISSUE_SEVERITIES)}
MAX_SOURCE_LENGTH = 12000
MAX_DRAFT_LENGTH = 12000
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_RETRIES = 0

SYSTEM_PROMPT = """你是稿件内容与宣传话术检查助手。你只检查并列出问题，不作法律判断、发布判断或审批结论。

必须遵守：
1. 事实判断只能对照用户输入中的原文和原文摘要，不得使用模型记忆或外部知识补充事实。
2. 原文没有依据的新结论标记为 unsupported_claim，不能声称外部事实一定错误。
3. 检查人物、公司、机构、产品、时间、地点、版本、漏洞编号、数字、比例、金额、数量和性能指标是否一致。
4. 检查稿件是否把"可能、预计、或许"等不确定表述改成确定事实。
5. 检查第一、唯一、最强、领先、竞品比较或贬损、100%、零风险、保证性和夸大性话术。
6. 每条问题的 quote 必须逐字引用稿件中的原句；推荐改写不得引入新数字、客户、能力或结论。
7. 没有问题时返回空 issues，不得凑数。
8. 原文正文不可用时，不判断外部事实真伪，只检查稿件自身矛盾和宣传话术。
9. 只返回一个 JSON 对象，不返回 Markdown 或解释。

category 字段只能取以下英文值（不得使用中文）：
- fact_mismatch：稿件中的事实与原文不一致
- unsupported_claim：稿件中的结论在原文中找不到依据
- internal_conflict：稿件内部前后矛盾
- absolute_claim：使用"第一、唯一、最强"等绝对化用语
- competitor_comparison：与竞品进行不当比较
- competitor_disparagement：使用贬低性词语评价竞品
- guarantee_claim：使用"100%、零风险、彻底杜绝"等保证性话术
- unsupported_data：稿件中的数据在原文中找不到出处
- exaggerated_claim：夸大产品能力或效果
- ambiguous_expression：表述含糊、容易引起误解

JSON 格式：
{
  "summary": "检查汇总",
  "issues": [
    {
      "issue_id": "issue-001",
      "category": "fact_mismatch|unsupported_claim|internal_conflict|absolute_claim|competitor_comparison|competitor_disparagement|guarantee_claim|unsupported_data|exaggerated_claim|ambiguous_expression",
      "severity": "high|medium|low",
      "quote": "稿件原句",
      "reason": "问题原因",
      "suggestion": "修改方向",
      "suggested_rewrite": "可选的推荐改写"
    }
  ]
}
"""


def _repair_json(text: str) -> str:
    """修复 LLM 生成的常见 JSON 语法问题。"""
    # 移除尾逗号：}, ] 前的逗号
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # 单引号 -> 双引号（仅匹配键和值的引号）
    text = re.sub(r"'([^']*)'(\s*:)", r'"\1"\2', text)
    text = re.sub(r":\s*'([^']*)'", r': "\1"', text)
    # 移除注释
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def normalize_draft_content(content: str) -> str:
    """统一换行并去除正文首尾空白，不改写正文语义。"""

    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def compute_content_hash(content: str) -> str:
    """计算用于识别审核结果是否过期的稳定 SHA-256。"""

    normalized = normalize_draft_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sentences(content: str) -> list[str]:
    """按中文标点和换行提取可回引的稿件原句。"""

    return [part.strip() for part in re.split(r"(?<=[。！？!?；;])|\n+", content) if part.strip()]


def scan_keyword_candidates(content: str) -> list[dict[str, str | None]]:
    """扫描明显宣传话术，仅作为 LLM 检查候选和故障降级结果。"""

    candidates: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()

    groups = (
        (
            ABSOLUTE_WORDS,
            "absolute_claim",
            "medium",
            "存在第一、唯一、最强或领先等绝对化表达，缺少明确范围和依据",
            "删除绝对化词语，改为描述可核实的具体能力和适用范围",
        ),
        (
            COMPARISON_WORDS,
            "competitor_comparison",
            "medium",
            "存在竞品比较表达，但比较对象、条件或数据依据可能不完整",
            "优先描述自身能力；确需比较时补充相同条件下的可核实依据",
        ),
        (
            GUARANTEE_WORDS,
            "guarantee_claim",
            "high",
            "对安全效果或覆盖范围作出绝对保证",
            "改为描述风险降低能力、适用场景和能力边界",
        ),
    )
    for sentence in _sentences(content):
        for words, category, severity, reason, suggestion in groups:
            matched = next((word for word in words if word in sentence), None)
            if matched is None:
                continue
            actual_category = category
            actual_severity = severity
            actual_reason = reason
            if category == "competitor_comparison" and matched in {"碾压", "吊打"}:
                actual_category = "competitor_disparagement"
                actual_severity = "high"
                actual_reason = "使用贬低性词语评价竞品或比较对象"
            key = (actual_category, sentence)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "category": actual_category,
                    "severity": actual_severity,
                    "quote": sentence,
                    "reason": actual_reason,
                    "suggestion": suggestion,
                    "suggested_rewrite": None,
                }
            )
    return candidates


class DraftReviewer:
    """结合确定性候选扫描与 LLM 对照检查稿件。"""

    def __init__(
        self,
        llm: Any,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.llm = llm
        # 启用 JSON 模式，强制 DeepSeek 返回合法 JSON
        self.json_llm = llm.bind(response_format={"type": "json_object"}) if hasattr(llm, "bind") else llm
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)

    async def review(
        self,
        article: dict[str, Any],
        draft: dict[str, Any],
        *,
        user_focus_items: str | None = None,
    ) -> DraftReview:
        """检查单篇稿件；审核失败不会修改或丢弃稿件。

        Args:
            article: 文章数据
            draft: 稿稿数据
            user_focus_items: 用户自定义审核关注项（追加到固定红线之后）
        """

        draft_title = str(draft.get("title") or "")
        draft_content = str(draft.get("content_md") or "")
        content_hash = compute_content_hash(draft_content)
        source_content = str(article.get("content_md") or "").strip()
        source_summary = str(article.get("summary_cn") or article.get("summary") or "").strip()
        fact_check_available = bool(source_content)
        candidate_content = "\n".join(part for part in (draft_title, draft_content) if part)
        candidates = scan_keyword_candidates(candidate_content)
        prompt = self._build_prompt(
            article=article,
            draft_title=draft_title,
            draft_content=draft_content,
            source_content=source_content,
            source_summary=source_summary,
            candidates=candidates,
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                # 构建系统提示词：固定红线 + 用户关注项（只增不减）
                effective_system_prompt = SYSTEM_PROMPT
                if user_focus_items and user_focus_items.strip():
                    effective_system_prompt += f"\n\n## 当前用户额外关注项\n{user_focus_items.strip()}"
                response = await asyncio.wait_for(
                    self.json_llm.ainvoke(
                        [SystemMessage(content=effective_system_prompt), HumanMessage(content=prompt)]
                    ),
                    timeout=self.timeout_seconds,
                )
                issues, model_summary = self._parse_response(response, candidate_content)
                issues = self._deduplicate_and_sort(issues)
                status = "completed" if fact_check_available else "partial"
                summary = model_summary.strip() or self._build_summary(issues)
                if not fact_check_available:
                    summary = f"事实检查不完整：缺少原文内容。{summary}"
                return self._result(
                    status=status,
                    content_hash=content_hash,
                    summary=summary,
                    issues=issues,
                    fact_check_available=fact_check_available,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Draft review attempt %d/%d failed: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )

        error_text = str(last_error or "").strip()
        if not error_text:
            error_text = (
                type(last_error).__name__ if last_error is not None else "unknown review error"
            )
        fallback_issues = [
            DraftReviewIssue(issue_id=f"issue-{index:03d}", **candidate)
            for index, candidate in enumerate(candidates, start=1)
        ]
        if fallback_issues:
            summary = f"智能检查失败；已返回 {len(fallback_issues)} 个规则命中的话术问题"
            if not fact_check_available:
                summary = f"事实检查不完整：缺少原文内容。{summary}"
            return self._result(
                status="partial",
                content_hash=content_hash,
                summary=summary,
                issues=self._deduplicate_and_sort(fallback_issues),
                fact_check_available=fact_check_available,
                error=error_text,
            )
        return self._result(
            status="failed",
            content_hash=content_hash,
            summary="稿件检查失败",
            issues=[],
            fact_check_available=fact_check_available,
            error=error_text,
        )

    @staticmethod
    def _build_prompt(
        *,
        article: dict[str, Any],
        draft_title: str,
        draft_content: str,
        source_content: str,
        source_summary: str,
        candidates: list[dict[str, str | None]],
    ) -> str:
        source_block = source_content[:MAX_SOURCE_LENGTH] or "（原文正文不可用）"
        return f"""请检查以下稿件。

## 原文标题
{article.get("title", "")}

## 原文摘要
{source_summary or "（无）"}

## 原文正文
{source_block}

## 稿件标题
{draft_title}

## 稿件正文
{draft_content[:MAX_DRAFT_LENGTH]}

## 规则候选（仅供复核，不代表最终问题）
{json.dumps(candidates, ensure_ascii=False)}
"""

    @classmethod
    def _parse_response(
        cls, response: Any, draft_content: str
    ) -> tuple[list[DraftReviewIssue], str]:
        raw = response.content if hasattr(response, "content") else response
        if isinstance(raw, list):
            raw = "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in raw
            )
        if not isinstance(raw, str):
            raw = str(raw)
        payload = cls._extract_json(raw)
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, list):
            raise ValueError("review response issues must be a list")

        issues: list[DraftReviewIssue] = []
        for index, raw_issue in enumerate(raw_issues, start=1):
            if not isinstance(raw_issue, dict):
                raise ValueError("review issue must be an object")
            issue_data = dict(raw_issue)
            issue_data["issue_id"] = str(issue_data.get("issue_id") or f"issue-{index:03d}")
            issue = DraftReviewIssue.model_validate(issue_data)
            # 精确匹配 -> 归一化匹配 -> 跳过
            if issue.quote not in draft_content:
                normalized_quote = re.sub(r"\s+", "", issue.quote)
                normalized_draft = re.sub(r"\s+", "", draft_content)
                if normalized_quote in normalized_draft:
                    # 归一化后匹配，保留原文中的实际句子
                    issue = issue.model_copy(update={"quote": issue.quote.strip()})
                else:
                    logger.warning(
                        "review issue quote not found in draft, skipping: %s",
                        issue.quote[:100],
                    )
                    continue
            issues.append(issue)
        return issues, str(payload.get("summary") or "")

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        # 剥离 ```json ... ``` 代码块
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block:
            text = code_block.group(1).strip()
        # 尝试直接解析
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # 回退：提取文本中第一个 JSON 对象（贪婪匹配以支持嵌套）
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if match is None:
                logger.warning(
                    "review response contains no JSON object, raw (first 500 chars): %s",
                    text[:500],
                )
                raise ValueError("review response is not valid JSON") from None
            candidate = match.group(0)
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                # 二次回退：修复常见 LLM JSON 语法问题后重试
                repaired = _repair_json(candidate)
                try:
                    payload = json.loads(repaired)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "review response JSON parse failed after repair: %s, raw (first 500 chars): %s",
                        exc,
                        candidate[:500],
                    )
                    raise ValueError("review response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("review response must be a JSON object")
        return payload

    @staticmethod
    def _deduplicate_and_sort(issues: list[DraftReviewIssue]) -> list[DraftReviewIssue]:
        unique: dict[tuple[str, str], DraftReviewIssue] = {}
        for issue in issues:
            key = (issue.category, normalize_draft_content(issue.quote))
            existing = unique.get(key)
            if (
                existing is None
                or SEVERITY_ORDER[issue.severity] < SEVERITY_ORDER[existing.severity]
            ):
                unique[key] = issue
        ordered = sorted(unique.values(), key=lambda issue: SEVERITY_ORDER[issue.severity])
        return [
            issue.model_copy(update={"issue_id": f"issue-{index:03d}"})
            for index, issue in enumerate(ordered, 1)
        ]

    @staticmethod
    def _build_summary(issues: list[DraftReviewIssue]) -> str:
        if not issues:
            return "未发现需要修改的问题"
        counts = {
            severity: sum(issue.severity == severity for issue in issues)
            for severity in ISSUE_SEVERITIES
        }
        return (
            f"发现 {counts['high']} 个必须修改问题、"
            f"{counts['medium']} 个建议修改问题、{counts['low']} 个表达优化问题"
        )

    @staticmethod
    def _result(
        *,
        status: str,
        content_hash: str,
        summary: str,
        issues: list[DraftReviewIssue],
        fact_check_available: bool,
        error: str | None = None,
    ) -> DraftReview:
        counts = {
            severity: sum(issue.severity == severity for issue in issues)
            for severity in ISSUE_SEVERITIES
        }
        return DraftReview(
            status=status,
            content_hash=content_hash,
            summary=summary,
            issues=issues,
            counts=counts,
            fact_check_available=fact_check_available,
            error=error,
        )
