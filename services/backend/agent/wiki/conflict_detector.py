"""Conflict Detector - 证据冲突检测（PR-11 / 文档 §12.4 conflict）。

职责：
  - 在已校验的 EvidenceItem 集合上检测"同一主题 + 语义相反"的证据
  - 产出 EvidenceConflict（topic / claims / source_refs）
  - 规则确定性回退：不依赖 LLM，可测试、可复现

边界约定：
  - 只读 EvidenceItem 列表，不直接访问 Wiki 根目录
  - 冲突结果进入 EvidenceBundle.conflicts → status 判定为 CONFLICTED
"""

from __future__ import annotations

import logging

from agent.wiki.evidence import EvidenceConflict, EvidenceItem

logger = logging.getLogger("backend.agent.wiki.conflict_detector")

# 语义相反的动词/短语集合（规则启发式）
_POSITIVE_TERMS = frozenset(
    {"支持", "可", "具备", "提供", "防护", "reduces", "mitigates", "capable", "enables"}
)
_NEGATIVE_TERMS = frozenset(
    {"不支持", "不可", "不具备", "无法", "局限", "vulnerable", "cannot", "no", "禁止"}
)

DEFAULT_TOPIC_THRESHOLD = 0.6


class ConflictDetector:
    """规则冲突检测器。"""

    def detect(
        self, evidence: list[EvidenceItem], topic_threshold: float = DEFAULT_TOPIC_THRESHOLD
    ) -> list[EvidenceConflict]:
        """检测证据冲突。默认按前 N 个字符分组的主题前缀启发式。"""
        _ = topic_threshold
        return detect_conflicts(evidence)


def detect_conflicts(
    evidence: list[EvidenceItem], topic_threshold: float = DEFAULT_TOPIC_THRESHOLD
) -> list[EvidenceConflict]:
    """规则检测冲突：同一主题出现语义相反的关键词。"""
    groups: dict[str, list[EvidenceItem]] = {}
    for e in evidence:
        topic = _topic_of(e.fact)
        groups.setdefault(topic, []).append(e)

    conflicts: list[EvidenceConflict] = []
    for topic, items in groups.items():
        if len(items) < 2:
            continue
        cloned = [i.model_copy() for i in items]
        polarities = [_polarity(it.fact) for it in cloned]
        have_pos = "positive" in polarities
        have_neg = "negative" in polarities
        if have_pos and have_neg:
            src_refs = [r for it in cloned for r in it.source_refs]
            conflicts.append(
                EvidenceConflict(
                    topic=topic,
                    claims=[it.fact for it in cloned],
                    source_refs=_dedupe_refs(src_refs),
                )
            )
            logger.info("检测到冲突 topic=%r claims=%d", topic, len(cloned))
    return conflicts


def _topic_of(fact: str) -> str:
    """主题前缀启发式：取事实前 12 个字符作为冲突聚类的 topic。"""
    return (fact or "").strip()[:12]


def _polarity(fact: str) -> str:
    text = (fact or "").lower()
    pos = any(w in text for w in _POSITIVE_TERMS)
    neg = any(w in text for w in _NEGATIVE_TERMS)
    if pos and not neg:
        return "positive"
    if neg:
        return "negative"
    return "neutral"


def _dedupe_refs(refs: list) -> list:
    seen: set[tuple] = set()
    out = []
    for r in refs:
        key = (getattr(r, "source_id", ""), getattr(r, "relative_path", ""), r.content_hash)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
