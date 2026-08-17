"""事实引用元数据与生成后事实审查（阶段六 S6-4 / S6-5）。

职责：
  - S6-4 事实引用元数据：渲染/解析 [KNOWLEDGE_SOURCE ...]...[KNOWLEDGE_SOURCE] 块，
          草稿应保存 knowledge_citations，后台可审计，最终 PR 不必展示技术性 doc_id
  - S6-5 生成后事实审查：提取产品事实候选（数字/版本/部署/竞品/案例），
          检查是否有知识来源；缺证据时标记需确认/弱化

仅做确定性规则审查，不调用 LLM；LLM 级审查由 draft_reviewer 既有能力承担。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("backend.agent.fact_citation")

# [KNOWLEDGE_SOURCE doc_id=... section_id=... needs_confirmation=true|false]
_CITATION_OPEN = re.compile(
    r"\[KNOWLEDGE_SOURCE\s+doc_id=(\S+)\s+section_id=(\S+)\s+needs_confirmation=(true|false)\s*\]"
)
# 完整引用块（含正文）
_CITATION_BLOCK = re.compile(
    r"\[KNOWLEDGE_SOURCE\s+doc_id=(\S+)\s+section_id=(\S+)\s+needs_confirmation=(true|false)\s*\]\s*\n?(.*?)\s*\[/KNOWLEDGE_SOURCE\]",
    re.DOTALL,
)

# 需要来源的事实类别（正则）
_FACT_PATTERNS: dict[str, re.Pattern] = {
    "number": re.compile(
        r"\d+(?:\.\d+)?\s*[%％万]|\d+(?:\.\d+)?\s*(?:ms|s|GB|TB|MHz|GHz|Gbps|Tbps|QPS|TPS)\b"
    ),
    "version": re.compile(r"[vV]\d+(?:\.\d+)+|\b\d+\.\d+\.\d+\b"),
    "deployment": re.compile(r"部署|上线|商用|落地|发布|灰度"),
    "competitor": re.compile(r"超越|领先|碾压|取代|竞品|比[A-Za-z\u4e00-\u9fa5]+"),
    "case": re.compile(r"客户|案例|试点|落地|某[一-龥]{1,8}(?:银行|政府|金融|运营商)"),
}

# 高审查等级类别：缺证据时必须标记
_HIGH_RISK_CATEGORIES: frozenset[str] = frozenset(
    {"number", "version", "case", "deployment"}
)


@dataclass(frozen=True)
class KnowledgeCitation:
    """一条事实引用元数据（S6-4）。"""

    doc_id: str
    section_id: str
    needs_confirmation: bool = False
    quote: str = ""


@dataclass(frozen=True)
class FactAuditIssue:
    """生成后事实审查发现的问题（S6-5）。"""

    category: str
    quote: str
    reason: str
    severity: str = "medium"


@dataclass(frozen=True)
class FactAuditResult:
    """事实审查结果。"""

    issues: list[FactAuditIssue] = field(default_factory=list)
    cited_clauses: int = 0
    fact_clauses: int = 0


# ── S6-4 引用渲染/解析 ─────────────────────────────────────


def render_citation(
    doc_id: str,
    section_id: str,
    needs_confirmation: bool = False,
    quote: str = "",
) -> str:
    """渲染一个 [KNOWLEDGE_SOURCE ...] 引用块。"""
    flag = "true" if needs_confirmation else "false"
    tag = (
        f"[KNOWLEDGE_SOURCE doc_id={doc_id} section_id={section_id} "
        f"needs_confirmation={flag}]"
    )
    if quote and quote.strip():
        return f"{tag}\n{quote.strip()}\n[/KNOWLEDGE_SOURCE]"
    return f"{tag}\n[/KNOWLEDGE_SOURCE]"


def parse_citations(content: str) -> list[KnowledgeCitation]:
    """从文本中解析所有 KNOWLEDGE_SOURCE 引用块。"""
    citations: list[KnowledgeCitation] = []
    for m in _CITATION_BLOCK.finditer(content):
        citations.append(
            KnowledgeCitation(
                doc_id=m.group(1),
                section_id=m.group(2),
                needs_confirmation=m.group(3) == "true",
                quote=(m.group(4) or "").strip(),
            )
        )
    return citations


def strip_citation_blocks(content: str) -> str:
    """移除引用块，返回可展示文本（最终 PR 不展示技术性 doc_id）。"""
    return _CITATION_BLOCK.sub("", content).strip()


# ── S6-5 生成后事实审查 ────────────────────────────────────


def classify_fact(text: str) -> list[str]:
    """识别文本中包含的需要来源的事实类别。"""
    cats: list[str] = []
    for cat, pattern in _FACT_PATTERNS.items():
        if pattern.search(text):
            cats.append(cat)
    return cats


def _clauses(text: str) -> list[str]:
    """按中文/英文句子分隔符切分句。"""
    return [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
        if part.strip()
    ]


def audit_fact_citations(
    draft_content: str,
    citations: list[KnowledgeCitation] | None = None,
) -> FactAuditResult:
    """确定性事实审查：检查缺少知识来源的高风险事实候选。

    Rules:
      - 解析草稿中的引用块作为"已引用"范围
      - 对每个含高风险事实（数字/版本/案例/部署/竞品）的句子，
        若其未被引用块覆盖则标记 missing_citation
    """
    content = draft_content or ""
    citations = (
        citations if citations is not None else parse_citations(content)
    )
    cited_ranges = [
        (m.start(), m.end()) for m in _CITATION_BLOCK.finditer(content)
    ]

    def _is_cited(pos: int) -> bool:
        return any(cs <= pos <= ce for cs, ce in cited_ranges)

    issues: list[FactAuditIssue] = []
    cited_clauses = 0
    fact_clauses = 0

    for clause in _clauses(content):
        cats = classify_fact(clause)
        if not cats:
            continue
        fact_clauses += 1
        pos = content.find(clause)
        if pos == -1:
            continue
        if _is_cited(pos):
            cited_clauses += 1
            continue
        if any(c in _HIGH_RISK_CATEGORIES for c in cats):
            issues.append(
                FactAuditIssue(
                    category="missing_citation",
                    quote=clause,
                    reason=f"包含 {'/'.join(cats)} 事实但缺少知识来源",
                    severity="high",
                )
            )
        else:
            issues.append(
                FactAuditIssue(
                    category="missing_citation",
                    quote=clause,
                    reason=f"包含 {'/'.join(cats)} 声明缺少知识来源",
                    severity="medium",
                )
            )

    return FactAuditResult(
        issues=issues,
        cited_clauses=cited_clauses,
        fact_clauses=fact_clauses,
    )
