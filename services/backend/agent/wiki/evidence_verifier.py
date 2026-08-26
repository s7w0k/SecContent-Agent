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


class EvidenceVerifier:
    """对候选证据做 Source Grounding 校验，并设定置信度。"""

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
        """校验候选证据，返回带置信度的新 EvidenceItem 列表。"""
        verified: list[EvidenceItem] = []
        for item in items:
            checks = self._check(item)
            confidence = self._confidence(checks)
            verified.append(
                EvidenceItem(
                    evidence_id=item.evidence_id,
                    fact=item.fact,
                    page_id=item.page_id,
                    page_title=item.page_title,
                    source_refs=item.source_refs,
                    relevance=item.relevance,
                    confidence=confidence,
                    relation_to_task=item.relation_to_task,
                )
            )
            logger.debug(
                "verify %s confidence=%.2f checks=%s",
                item.evidence_id,
                confidence,
                checks,
            )
        return verified

    def verified_evidence(
        self, items: list[EvidenceItem], min_confidence: float | None = None
    ) -> list[EvidenceItem]:
        """只保留置信度达到阈值（默认 0.8）的证据。"""
        threshold = min_confidence if min_confidence is not None else self.threshold
        return [e for e in self.verify(items) if e.confidence >= threshold]

    # ── 检查项 ──────────────────────────────────────────────

    def _check(self, item: EvidenceItem) -> dict[str, bool]:
        page_ok = self.store.page_exists(item.page_id)
        refs = item.source_refs
        return {
            "page_exists": page_ok,
            "source_exists": bool(refs) and all(self._source_exists(r) for r in refs),
            "source_hash_matches": bool(refs) and all(self._hash_matches(r) for r in refs),
            "source_section_exists": bool(refs) and all(self._section_exists(r) for r in refs),
            "fact_supported": self._fact_supported(item.fact, refs),
            "source_not_stale": bool(refs) and all(self._not_stale(r) for r in refs),
        }

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

    # ── 置信度合成 ──────────────────────────────────────────

    @staticmethod
    def _confidence(checks: dict[str, bool]) -> float:
        core = [
            checks["page_exists"],
            checks["source_exists"],
        ]
        if all(core):
            if checks["source_hash_matches"] and checks["source_not_stale"]:
                if checks["source_section_exists"] and checks["fact_supported"]:
                    return VERIFIED_CONFIDENCE
                return PARTIAL_CONFIDENCE
            return PARTIAL_CONFIDENCE
        return UNVERIFIED_CONFIDENCE


def _safe_rel(rel: str) -> str:
    from agent.wiki.contracts import is_path_safe

    if not is_path_safe(rel):
        return ""
    return rel.replace("\\", "/")


def _tokenize(fact: str) -> list[str]:
    import re

    return [t for t in re.split(r"\s+", fact) if t]
