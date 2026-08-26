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

from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceItem

logger = logging.getLogger("backend.agent.wiki.evidence_collector")

_SOURCE_LINE = re.compile(r"^-\s*(.+?)\s*\[来源:\s*(.+?)\]$")


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
        for page_id, page in opened_pages.items():
            if not hasattr(page, "meta"):
                continue
            refs = page.meta.source_refs or []
            facts = self._extract_facts(page)
            if not facts:
                # 至少把 Summary 作为一条可选事实
                summary = page.summary()
                if summary:
                    facts = [summary]
            for fact in facts:
                sofar += 1
                items.append(
                    EvidenceItem(
                        evidence_id=f"ev-{sofar}",
                        fact=fact,
                        page_id=page_id,
                        page_title=page.meta.title,
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
