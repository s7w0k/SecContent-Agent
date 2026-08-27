"""Evidence Collector - 从 Navigator 访问到的页面提取候选事实。

PR-06 产物：
  Visited Wiki Pages → EvidenceCollector → Candidate Facts → Source Grounding Verifier

设计约束：
  - Collector 只产出“候选事实”，置信度/是否进入 scoring 由 Verifier 决定
  - 页面无 source_refs 声明仍是候选（confidence 偏低），但不会成为 grounded evidence
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from agent.wiki.contracts import SourceRef, compute_claim_id
from agent.wiki.evidence import EvidenceItem
from agent.wiki.requirements import default_requirements
from agent.wiki.resolver import normalize_text

logger = logging.getLogger("backend.agent.wiki.evidence_collector")

_SOURCE_LINE = re.compile(r"^-\s*(.+?)\s*\[来源:\s*(.+?)\]$")


def _requirement_map(task_type: str) -> dict[str, list[str]]:
    """page_type → requirement_ids（§11 EvidenceCandidate.requirement_ids）。"""
    mapping: dict[str, list[str]] = {}
    for req in default_requirements(task_type):
        for pt in req.required_page_types:
            mapping.setdefault(pt, []).append(req.requirement_id)
    return mapping


class EvidenceCollector:
    """从已访问页面收集候选证据。"""

    def __init__(self, store: Any):
        self.store = store

    def collect(
        self,
        query: str,
        opened_pages: dict[str, Any],
        task_type: str = "score",
    ) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []
        q = query.lower()
        sofar = 0
        req_ids: dict[str, list[str]] = _requirement_map(task_type)
        seen: set[str] = set()  # claim_id / 归一化文本（有界语义去重，§11)
        for page_id, page in opened_pages.items():
            if not hasattr(page, "meta"):
                continue
            refs = page.meta.source_refs or []
            page_type = page.meta.page_type
            requirement_ids = list(req_ids.get(page_type, []))
            claim_facts = self._facts_from_claims(page)
            if claim_facts:
                # Phase 3/8：优先消费结构化 WikiClaim（claim 粒度 provenance）
                for claim_id, fact, claim_refs in claim_facts:
                    if not self._not_seen(seen, claim_id, fact):
                        continue
                    sofar += 1
                    items.append(
                        EvidenceItem(
                            evidence_id=f"ev-{sofar}",
                            fact=fact,
                            claim_id=claim_id,
                            page_id=page_id,
                            page_title=page.meta.title,
                            section_id=self._claim_section(page, claim_id),
                            requirement_ids=requirement_ids,
                            source_refs=list(claim_refs or refs),
                            relevance=self._relevance(fact, q),
                            confidence=0.0,  # 由 Verifier 填充
                            relation_to_task="potential_match",
                        )
                    )
                continue
            facts = self._extract_facts(page)
            if not facts:
                # 至少把 Summary 作为一条可选事实
                summary = page.summary()
                if summary:
                    facts = [summary]
            for fact in facts:
                if not self._not_seen(seen, "", fact):
                    continue
                sofar += 1
                items.append(
                    EvidenceItem(
                        evidence_id=f"ev-{sofar}",
                        fact=fact,
                        claim_id="",
                        page_id=page_id,
                        page_title=page.meta.title,
                        section_id=self._evidence_section_id(page),
                        requirement_ids=requirement_ids,
                        source_refs=[
                            SourceRef(
                                source_id=r.source_id,
                                relative_path=r.relative_path,
                                content_hash=r.content_hash,
                                heading=r.heading,
                                section_id=r.section_id,
                            )
                            for r in refs
                        ],
                        relevance=self._relevance(fact, q),
                        confidence=0.0,  # 由 Verifier 填充
                        relation_to_task="potential_match",
                    )
                )
        return items

    @staticmethod
    def _not_seen(seen: set[str], claim_id: str, fact: str) -> bool:
        """有界语义去重：优先 claim_id，否则归一化文本（§11）。
        避免近义改写被当成多份独立证据虚增 coverage/confidence。
        """
        key = claim_id or ("norm:" + normalize_text(fact))
        if key in seen:
            return False
        seen.add(key)
        return True

    @staticmethod
    def _claim_section(page, claim_id: str) -> str:
        section_id = ""
        for c in getattr(page.meta, "claims", None) or []:
            if (c.claim_id or "") == claim_id:
                section_id = getattr(c, "section_id", "") or ""
                break
        return section_id

    @staticmethod
    def _evidence_section_id(page) -> str:
        for sec in page.sections:
            title = sec.title.lower()
            if "evidence" in title or "source" in title or "来源" in title:
                return getattr(sec, "section_id", "") or sec.title
        return ""

    def _facts_from_claims(self, page) -> list[tuple[str, str, list[SourceRef]]]:
        """结构化 Claims → (claim_id, text, source_refs)。缺 claim_id 时确定性生成。"""
        out: list[tuple[str, str, list[SourceRef]]] = []
        claims = getattr(page.meta, "claims", None) or []
        product_id = page.meta.product_id or ""
        for c in claims:
            cid = c.claim_id or compute_claim_id(
                product_id=product_id,
                claim_type=c.claim_type,
                semantic_key=c.text,
            )
            out.append((cid, c.text[:300], list(c.source_refs)))
        return out

    def _extract_facts(self, page) -> list[str]:
        """从 Evidence & Sources 章节的行里抽取事实（去除 [来源: ...] 后缀）。"""
        facts: list[str] = []
        for sec in page.sections:
            title = sec.title.lower()
            if "evidence" not in title and "source" not in title and "来源" not in title:
                continue
            for line in sec.body.split("\n"):
                line = line.strip()
                m = _SOURCE_LINE.match(line)
                if m:
                    facts.append(m.group(1).strip()[:300])
                elif line and not line.startswith(("#", "- [")):
                    facts.append(line.strip()[:300])
        return _dedupe(facts)

    @staticmethod
    def _relevance(fact: str, query: str) -> float:
        if not query:
            return 0.5
        words = [w for w in re.split(r"[\s，。、？?；;]", query) if len(w) >= 2]
        if not words:
            return 0.5
        hits = sum(1 for w in words if w.lower() in fact.lower())
        return min(1.0, hits / max(1, len(words)) + 0.3)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = hashlib.sha256(it.encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
