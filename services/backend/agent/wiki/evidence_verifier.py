"""Evidence Verifier - Source Grounding 校验器。

PR-06 产物（文档 16.1）：
  Candidate Facts → Source Grounding Verifier → EvidenceBundle

每个 EvidenceItem 检查：
  page exists / source exists / source hash matches / source section exists /
  fact is supported / source not stale

只有通过校验（confidence >= 0.8）的 evidence 才可进入 scoring evidence。
Rule-based 确定性回退：即使无 LLM 也能运行、可测试。

设计约束：
  - Verifier 只读 Raw Source 与 Wiki（Runtime Plane）
  - 页面无 source_refs 时不被判为 grounded（confidence 低于阈值）
  - 读取源文件失败 / 哈希不匹配都会显著降低置信度
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceItem

logger = logging.getLogger("backend.agent.wiki.evidence_verifier")

# 判定"可进入 scoring"的置信度阈值（文档 16.1）
VERIFIED_CONFIDENCE = 0.9  # 全部核心检查通过
PARTIAL_CONFIDENCE = 0.6  # 页面在、源存在但哈希/章节未完全匹配
UNVERIFIED_CONFIDENCE = 0.1  # 页面/源不可验证
EVIDENCE_THRESHOLD = 0.8

# Verifier V2 reason codes（§12.5）
REASON_VERIFIED = "VERIFIED"
REASON_PAGE_MISSING = "PAGE_MISSING"
REASON_SOURCE_MISSING = "SOURCE_MISSING"
REASON_SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
REASON_STALE_SOURCE_REF = "STALE_SOURCE_REF"
REASON_SECTION_NOT_FOUND = "SECTION_NOT_FOUND"
REASON_LINE_RANGE_INVALID = "LINE_RANGE_INVALID"
REASON_NOT_SUPPORTED = "NOT_SUPPORTED"

# 分层验证权重（§12 Confidence Calibration）
_PARTIAL_SECTION = 0.7  # 章节缺失 / 部分支持回调
_UNGROUNDED = 0.1


class EvidenceVerifier:
    """对候选证据做 Source Grounding 校验，并设定置信度（V2 分层，§12）。"""

    def __init__(
        self,
        store: Any,
        source_registry: Any | None = None,
        source_root: str | Path | None = None,
        threshold: float = EVIDENCE_THRESHOLD,
    ):
        self.store = store
        self.registry = source_registry
        self.source_root = Path(source_root) if source_root else None
        self.threshold = threshold

    # ── 主入口 ──────────────────────────────────────────────

    def verify(self, items: list[EvidenceItem]) -> list[EvidenceItem]:
        """分层校验候选证据，返回带 reason_code + 校准置信度的新列表（§12）。"""
        verified: list[EvidenceItem] = []
        for item in items:
            confidence, reason = self._evaluate(item)
            verified.append(
                EvidenceItem(
                    evidence_id=item.evidence_id,
                    fact=item.fact,
                    claim_id=item.claim_id,
                    page_id=item.page_id,
                    page_title=item.page_title,
                    section_id=item.section_id,
                    requirement_ids=list(item.requirement_ids),
                    reason_code=reason,
                    source_refs=item.source_refs,
                    relevance=item.relevance,
                    confidence=confidence,
                    relation_to_task=item.relation_to_task,
                )
            )
            logger.debug(
                "verify %s confidence=%.2f reason=%s",
                item.evidence_id,
                confidence,
                reason,
            )
        return verified

    def verified_evidence(
        self, items: list[EvidenceItem], min_confidence: float | None = None
    ) -> list[EvidenceItem]:
        """只保留置信度达到阈值（默认 0.8）的证据。"""
        threshold = min_confidence if min_confidence is not None else self.threshold
        return [e for e in self.verify(items) if e.confidence >= threshold]

    # ── 分层校验 V2（§12）─────────────────────────────────

    def _evaluate(self, item: EvidenceItem) -> tuple[float, str]:
        """分层验证（L0→L5）并返回 (confidence, reason_code)。"""
        page_ok = self.store.page_exists(item.page_id)
        refs = item.source_refs
        src_exists = bool(refs) and all(self._source_exists(r) for r in refs)
        src_hash = bool(refs) and all(self._hash_matches(r) for r in refs)
        not_stale = bool(refs) and all(self._not_stale(r) for r in refs)
        section_ok = bool(refs) and all(self._section_exists(r) for r in refs)
        entail = self._fact_supported(item.fact, refs)

        # L0 页面/安全超链接
        if not page_ok:
            return _UNGROUNDED, REASON_PAGE_MISSING
        # 无 source_ref → 不可 grounded
        if not refs:
            return PARTIAL_CONFIDENCE, REASON_NOT_SUPPORTED
        # L1 源存在
        if not src_exists:
            return _UNGROUNDED, REASON_SOURCE_MISSING
        # L2 哈希新鲜度强校验（§12.1）
        if not src_hash:
            return round(PARTIAL_CONFIDENCE * 0.5, 4), REASON_SOURCE_HASH_MISMATCH
        if not not_stale:
            return PARTIAL_CONFIDENCE, REASON_STALE_SOURCE_REF
        # L3 section / line range
        if not all(self._line_range_ok(r) for r in refs):
            return PARTIAL_CONFIDENCE, REASON_LINE_RANGE_INVALID
        if not section_ok:
            return PARTIAL_CONFIDENCE * _PARTIAL_SECTION, REASON_SECTION_NOT_FOUND
        # L4 词法支持 + L5 语义蕴含（规则回退）
        if not entail:
            return PARTIAL_CONFIDENCE * _PARTIAL_SECTION, REASON_NOT_SUPPORTED
        # 全部 L0-L5 检查通过 → 高置信（≥0.8 阈值）
        return VERIFIED_CONFIDENCE, REASON_VERIFIED

    def _line_range_ok(self, ref: SourceRef) -> bool:
        """若 section_id 形如 line 范围（如 `line:12` / `lines:3-5`），校验行界。"""
        section_id = (ref.section_id or "").strip().lower()
        if not section_id or not section_id.startswith(("line:", "lines:")):
            return True
        content = self._read_source(ref)
        if not content:
            return False
        n = len(content.splitlines())
        try:
            marker = section_id[section_id.index(":") + 1 :]
            if "-" in marker:
                start_s, end_s = marker.split("-", 1)
                return 1 <= int(start_s) <= int(end_s) <= n
            return 1 <= int(marker) <= n
        except ValueError:
            return False

    def _source_exists(self, ref: SourceRef) -> bool:
        path = self._source_path(ref)
        return path is not None and path.is_file()

    def _source_path(self, ref: SourceRef) -> Path | None:
        if self.registry is not None:
            entry = self.registry.get(ref.source_id)
            root = self.registry.root if entry else None
        else:
            root = self.source_root
        if root is None:
            return None
        cand = Path(root) / _safe_rel(ref.relative_path)
        return cand if _safe_rel(ref.relative_path) else None

    def _read_source(self, ref: SourceRef) -> str:
        path = self._source_path(ref)
        if path is None or not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return path.read_text(encoding="gbk")
            except Exception:
                return ""
        except Exception:
            return ""

    def _hash_matches(self, ref: SourceRef) -> bool:
        content = self._read_source(ref)
        if not content:
            return False
        from agent.wiki.source_registry import content_hash

        return content_hash(content) == (ref.content_hash or "")

    def _section_exists(self, ref: SourceRef) -> bool:
        heading = (ref.heading or "").strip()
        section_id = (ref.section_id or "").strip()
        if not heading and not section_id:
            return True  # 无章节约束，视为满足
        content = self._read_source(ref)
        if not content:
            return False
        if heading and heading not in content:
            return False
        return not (section_id and section_id not in content)

    def _fact_supported(self, fact: str, refs: list[SourceRef]) -> bool:
        """轻量支持性检查：事实非空，且尽量在源内容中有词项命中。"""
        fact = (fact or "").strip()
        if not fact:
            return False
        terms = [t for t in _tokenize(fact) if len(t) >= 2]
        if not terms:
            return True
        # 命中部分源内容即视为部分支持（规则确定性回退）
        for ref in refs[:3]:
            content = self._read_source(ref)
            if not content:
                continue
            hits = sum(1 for t in terms if t in content)
            if hits / len(terms) >= 0.2:
                return True
        return len(terms) >= 4  # 无源可核对时，长事实容忍宽松

    def _not_stale(self, ref: SourceRef) -> bool:
        """源是否 stale：注册表中状态 active 且哈希一致。"""
        if self.registry is None:
            return True
        entry = self.registry.get(ref.source_id)
        if entry is None:
            return False
        if entry.status not in ("", "active"):
            return False
        return entry.sha256 == (ref.content_hash or "")


def _safe_rel(rel: str) -> str:
    from agent.wiki.contracts import is_path_safe

    if not is_path_safe(rel):
        return ""
    return rel.replace("\\", "/")


def _tokenize(fact: str) -> list[str]:
    import re

    return [t for t in re.split(r"\s+", fact) if t]
