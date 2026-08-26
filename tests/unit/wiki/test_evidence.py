"""PR-06 Evidence / Collector / Verifier / Bundle 单元测试。"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceBundle, EvidenceItem
from agent.wiki.evidence_collector import EvidenceCollector
from agent.wiki.evidence_verifier import EvidenceVerifier
from agent.wiki.provider import KnowledgeRequest, assemble_bundle, detect_conflicts
from helpers import make_page, make_source_file


def _source_ref(source_id: str, rel: str, content_hash: str) -> SourceRef:
    return SourceRef(source_id=source_id, relative_path=rel, content_hash=content_hash)


class TestEvidenceContracts:
    def test_verified_filters_by_confidence(self):
        bundle = EvidenceBundle(
            status="SUFFICIENT",
            evidence=[
                EvidenceItem(evidence_id="e1", fact="a", page_id="p", confidence=0.9),
                EvidenceItem(evidence_id="e2", fact="b", page_id="p", confidence=0.5),
            ],
        )
        assert [e.evidence_id for e in bundle.verified()] == ["e1"]
        assert bundle.is_sufficient()

    def test_rejects_out_of_range_confidence(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EvidenceItem(evidence_id="e", fact="a", page_id="p", confidence=1.5)


class TestCollector:
    def test_collect_from_pages(self, store):
        page = make_page(
            "product.a.capability.x",
            product_id="a",
            source_refs=[_source_ref("s1", "a.md", "h")],
        )
        results = EvidenceCollector(store).collect("身份认证", {"product.a.capability.x": page})
        assert results
        assert results[0].confidence == 0.0  # verifier 决定置信度
        assert results[0].fact

    def test_relevance_nonzero(self, store):
        page = make_page("concept.x", page_type="concept")
        items = EvidenceCollector(store).collect("身份", {"concept.x": page})
        assert all(0.0 <= i.relevance <= 1.0 for i in items)


class TestVerifier:
    def test_verify_grounded_source_high_confidence(self, source_root, wiki_root):
        rel, _h = make_source_file(
            source_root, "1-产品/overview.md", "# 产品\n支持智能体身份认证\n提供 MCP 协议防护\n"
        )
        from agent.wiki.source_registry import SourceRegistry

        registry = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
        registry.sync()
        entry = registry.get_by_path(rel)
        assert entry is not None

        # 页面 source_ref 指向真实源，且 content_hash 匹配 registry 的 sha256
        page = make_page(
            "product.a.capability.x",
            product_id="a",
            source_refs=[_source_ref(entry.source_id, rel, entry.sha256)],
        )
        from agent.wiki.store import WikiStore

        store = WikiStore(wiki_root)
        store.write_page(page)
        items = EvidenceCollector(store).collect("身份", {"product.a.capability.x": page})
        verifier = EvidenceVerifier(store, source_registry=registry, source_root=source_root)
        verified = verifier.verify(items)
        assert all(v.confidence >= 0.8 for v in verified)

    def test_verify_missing_page_low_confidence(self, store):
        # 页面不存在于 store 时，即使 item 存在也低置信
        item = EvidenceItem(
            evidence_id="e1",
            fact="事实",
            page_id="concept.missing",
            source_refs=[_source_ref("s1", "a.md", "h")],
            confidence=0.0,
        )
        verifier = EvidenceVerifier(store)
        out = verifier.verify([item])
        assert out[0].confidence < 0.8


class TestBundleStatus:
    def test_failed_when_no_visited_pages(self):
        b = assemble_bundle(
            request=KnowledgeRequest(task_type="score", query="q", product_ids=[]),
            evidence=[],
            visited_pages=[],
            wiki_version="",
        )
        assert b.status == "FAILED"

    def test_insufficient_when_low_coverage(self):
        b = assemble_bundle(
            request=KnowledgeRequest(task_type="score", query="q", product_ids=[]),
            evidence=[],
            visited_pages=["p1"],
            wiki_version="v",
        )
        assert b.status == "INSUFFICIENT_EVIDENCE"

    def test_sufficient_when_evidence(self):
        evidence = [
            EvidenceItem(evidence_id="e1", fact="支持身份认证", page_id="p", confidence=0.9)
        ]
        b = assemble_bundle(
            request=KnowledgeRequest(task_type="score", query="q", product_ids=[]),
            evidence=evidence,
            visited_pages=["p1", "p2", "p3", "p4"],
            wiki_version="v",
        )
        assert b.status == "SUFFICIENT"


class TestConflictDetection:
    def test_detect_conflicting_claims(self):
        # 两条声明共享相同的前 12 字符 topic 前缀，但语义相反
        evidence = [
            EvidenceItem(
                evidence_id="e1", fact="支持身份认证提供MCP防护与限流", page_id="p", confidence=0.9
            ),
            EvidenceItem(
                evidence_id="e2",
                fact="支持身份认证提供MCP防护但无法扩展",
                page_id="q",
                confidence=0.9,
            ),
        ]
        conflicts = detect_conflicts(evidence, topic_threshold=0.2)
        assert len(conflicts) >= 1

    def test_no_conflict_when_aligned(self):
        evidence = [
            EvidenceItem(evidence_id="e1", fact="产品支持身份认证", page_id="p", confidence=0.9),
            EvidenceItem(evidence_id="e2", fact="产品支持 MCP 防护", page_id="p", confidence=0.9),
        ]
        assert detect_conflicts(evidence) == []
