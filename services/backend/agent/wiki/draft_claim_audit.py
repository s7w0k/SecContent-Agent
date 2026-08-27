"""Draft Claim Audit（Phase 13 / §16.1）— 生成的草稿必须 Grounded。

Generated Draft
→ Claim Extractor
→ Product Factual Claims
→ Evidence Alignment（逐条对照 EvidenceBundle 的已验证 Fact/Claim）
→ unsupported claim 标记（供 Writer rewrite/remove）

本组件只做确定性对照，不调用 LLM，保证可测试、可回退。
对齐以"产品事实"为准：只有命中已验证产品证据的声明才算 supported；
标题、结构、叙事等非产品事实由 Writer 自行负责，不在此判定。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.draft_claim_audit")

# 中文句子分隔符（。！？…；以及换行/分号）
_SENTENCE_SPLIT = re.compile(r"[。！？…；]|(?<!\d)\.(?!\d)|[\n\r;]+")
# 去空格/去标点后保留的"事实性"关键片段最小长度
_MIN_CLAIM_CHARS = 6
# 判定 supported 所需的最少共同 bigram 数
_MIN_OVERLAP_BIGRAMS = 2

# 非产品事实的高频词（标题/行文词汇），不用于证据对照判定
_STOPWORDS = frozenset(
    {
        "产品",
        "支持",
        "我们",
        "具备",
        "提供",
        "实现",
        "拥有",
        "用于",
        "可以",
        "进行",
        "以及",
        "包括",
        "其",
        "在下",
        "作为",
        "通过",
        "基于",
        "能够",
        "核心",
        "能力",
        "帮助",
        "保障",
        "确保",
        "全面",
        "企业",
        "用户",
    }
)


class DraftClaim(BaseModel):
    """从草稿提取出的一条可核验产品事实声明。"""

    claim_id: str = Field(description="声明 ID（基于段落+文本 hash）")
    claim: str = Field(description="声明原文")
    paragraph_id: str = Field(default="", description="所属段落 ID")
    evidence_ids: list[str] = Field(default_factory=list, description="对齐到的已验证证据")
    supported: bool = Field(default=False, description="是否被产品证据 Grounded")
    overlap: int = Field(default=0, description="与最佳证据的共同 bigram 数")


class DraftClaimAudit:
    """把生成的草稿正文拆解为产品事实声明，并对照 EvidenceBundle 做对齐（§16.1）。

    用法：
        audit = DraftClaimAudit(bundle)
        result = audit.audit(draft_text)   # → AuditResult
    依赖 EvidenceBundle 的 verified() 即可（无需 Consumer 感知实现细节）。
    """

    def __init__(self, bundle: Any):
        # 只使用已通过 Source Grounding（confidence>=0.8）的证据事实与 claim_id
        self._evidence = [
            (e.fact or "", e.evidence_id, e.claim_id or "")
            for e in bundle.verified()
            if (e.fact or "").strip()
        ]

    def audit(self, text: str, *, paragraph_delimiter: str | None = None) -> dict:
        """拆句、提取产品事实声明并逐条对齐，返回审计结果。"""
        claims = self.extract_claims(text, paragraph_delimiter=paragraph_delimiter)
        aligned = []
        for claim in claims:
            claim_id, claim_text, paragraph_id = claim
            evidence_ids, overlap = self._align(claim_text)
            aligned.append(
                DraftClaim(
                    claim_id=claim_id,
                    claim=claim_text,
                    paragraph_id=paragraph_id,
                    evidence_ids=evidence_ids,
                    supported=bool(evidence_ids),
                    overlap=overlap,
                )
            )
        unsupported = [c for c in aligned if not c.supported]
        logger.info(
            "draft claim audit: %d claims, %d unsupported",
            len(aligned),
            len(unsupported),
        )
        return {
            "claims": aligned,
            "total": len(aligned),
            "supported": len(aligned) - len(unsupported),
            "unsupported": len(unsupported),
            "grounded_ratio": (len(aligned) - len(unsupported)) / len(aligned) if aligned else 0.0,
        }

    def extract_claims(
        self, text: str, *, paragraph_delimiter: str | None = None
    ) -> list[tuple[str, str, str]]:
        """按句切分并保留"看起来像产品事实"的声明。

        返回 [(claim_id, claim_text, paragraph_id)]。paragraph_delimiter 非空时按段落分组。
        """
        if not text:
            return []
        normalized = unicodedata.normalize("NFKC", text)
        claims: list[tuple[str, str, str]] = []
        paragraph_id = ""
        segments = (
            re.split(re.escape(paragraph_delimiter), normalized)
            if paragraph_delimiter
            else [normalized]
        )
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if paragraph_delimiter:
                paragraph_id = _short_hash(seg)[:8]
            for sentence in _SENTENCE_SPLIT.split(seg):
                claim_text = sentence.strip()
                if len(claim_text) < _MIN_CLAIM_CHARS:
                    continue
                claims.append(
                    (_short_hash(paragraph_id + "|" + claim_text), claim_text, paragraph_id)
                )
        return claims

    def _align(self, claim_text: str) -> tuple[list[str], int]:
        """贪心对齐：找共同 bigram 最多的已验证证据；重叠达到阈值则判定 supported。"""
        claim_bigrams = _cjk_bigrams(_significant(claim_text))
        if not claim_bigrams:
            return [], 0
        best_evidence: list[str] = []
        best_overlap = 0
        # lexicographic 确定性排序，避免读取顺序影响结果
        for fact, evidence_id, claim_id in sorted(self._evidence):
            fact_bigrams = _cjk_bigrams(_significant(fact))
            if not fact_bigrams:
                continue
            overlap = len(claim_bigrams & fact_bigrams)
            if overlap > best_overlap:
                best_overlap = overlap
                best_evidence = [evidence_id] + ([claim_id] if claim_id else [])
        if best_overlap >= _MIN_OVERLAP_BIGRAMS:
            return best_evidence, best_overlap
        return [], best_overlap


def _significant(text: str) -> str:
    """去标点、去停用词、去空白，保留用于对照的实质内容。"""
    stripped = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    out = []
    index = 0
    while index < len(stripped):
        if stripped[index] in _STOPWORDS:
            index += 1
            continue
        out.append(stripped[index])
        index += 1
    return "".join(out)


def _cjk_bigrams(text: str) -> set[str]:
    """对去尾后文本生成 CJK/Latin bigram 集合（用于是否强调的证据重叠判定）。"""
    if len(text) < 2:
        return set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _short_hash(text: str) -> str:
    import hashlib

    return "claim_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
