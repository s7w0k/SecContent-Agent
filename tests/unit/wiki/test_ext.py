"""Ext 模块测试：conflict_detector + telemetry + maintainer。"""

from __future__ import annotations

from pathlib import Path

from agent.wiki.conflict_detector import ConflictDetector, detect_conflicts
from agent.wiki.evidence import EvidenceItem
from agent.wiki.maintainer import WikiMaintainer
from agent.wiki.provider import assemble_bundle
from agent.wiki.store import WikiStore
from agent.wiki.telemetry import WikiTelemetry
from helpers import make_page, make_source_file, sha256_hex


def _source_ref(source_id: str, rel: str, content_hash: str):
    from agent.wiki.contracts import SourceRef

    return SourceRef(source_id=source_id, relative_path=rel, content_hash=content_hash)


class TestConflictDetector:
    def test_detects_opposing_claims(self):
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
        conflicts = ConflictDetector().detect(evidence)
        assert len(conflicts) >= 1

    def test_detect_conflicts_functional_match(self):
        evidence = [
            EvidenceItem(evidence_id="e1", fact="支持身份认证提供MCP防护与限流", page_id="p"),
            EvidenceItem(evidence_id="e2", fact="支持身份认证提供MCP防护但无法扩展", page_id="q"),
        ]
        assert len(detect_conflicts(evidence)) >= 1

    def test_no_conflict_when_aligned(self):
        evidence = [
            EvidenceItem(evidence_id="e1", fact="产品支持身份认证", page_id="p", confidence=0.9),
            EvidenceItem(evidence_id="e2", fact="产品支持 MCP 防护", page_id="p", confidence=0.9),
        ]
        assert detect_conflicts(evidence) == []

    def test_skips_single_claim(self):
        evidence = [EvidenceItem(evidence_id="e1", fact="产品支持身份认证", page_id="p")]
        assert detect_conflicts(evidence) == []


class TestTelemetry:
    def test_snapshot_empty(self):
        snap = WikiTelemetry().snapshot()
        assert snap["runs"] == 0

    def test_record_bundle_aggregates(self):
        from agent.wiki.provider import KnowledgeRequest

        t = WikiTelemetry()
        bundle = assemble_bundle(
            request=KnowledgeRequest(task_type="score", query="q", product_ids=["a"]),
            evidence=[
                EvidenceItem(evidence_id="e1", fact="f1", page_id="p", confidence=0.9),
                EvidenceItem(evidence_id="e2", fact="f2", page_id="q", confidence=0.9),
            ],
            visited_pages=["p", "q"],
            wiki_version="v1",
        )
        t.record_bundle(bundle, mode="wiki")
        snap = t.snapshot()
        assert snap["runs"] == 1
        assert snap["mode_distribution"] == {"wiki": 1}
        assert snap["avg_grounding_rate"] == 1.0
        assert snap["status_distribution"] == {"SUFFICIENT": 1}

    def test_record_shadow_and_reset(self):
        t = WikiTelemetry()
        t.record_shadow({"wiki_version": "v1", "legacy_evidence": 3, "wiki_evidence": 2})
        assert t.snapshot()["shadow_comparisons"] == 1
        t.reset()
        assert t.snapshot()["runs"] == 0
        assert t.snapshot()["shadow_comparisons"] == 0


class TestMaintainer:
    REL = "1-产品/overview.md"

    def _write_source(self, source_root: Path):
        rel, content_hash = make_source_file(source_root, self.REL, "# 产品\n支持智能体身份认证\n")
        return rel, content_hash

    def _write_page(self, store: WikiStore, rel: str, content_hash: str):
        from agent.wiki.source_registry import stable_source_id

        page = make_page(
            "product.a.capability.x",
            page_type="capability",
            product_id="a",
            source_refs=[_source_ref(stable_source_id(rel), rel, content_hash)],
        )
        store.write_page(page)

    def _registry(self, source_root: Path, store: WikiStore):
        from agent.wiki.source_registry import SourceRegistry

        return SourceRegistry(source_root, store.root / "_meta" / "source-registry.json")

    def test_analyze_reports_new_source_and_impacted_page(self, source_root, store):
        rel, content_hash = self._write_source(source_root)
        self._write_page(store, rel, content_hash)
        mt = WikiMaintainer(store, self._registry(source_root, store))
        report = mt.analyze()
        assert self.REL in report.new_sources
        assert "product.a.capability.x" in report.impacted_page_ids
        assert report.needs_action

    def test_analyze_no_action_when_unchanged(self, source_root, store):
        rel, content_hash = self._write_source(source_root)
        self._write_page(store, rel, content_hash)
        reg = self._registry(source_root, store)
        reg.sync()  # 已注册，磁盘无变化
        mt = WikiMaintainer(store, reg)
        report = mt.analyze()
        assert not report.needs_action
        assert report.impacted_page_ids == []

    async def test_regenerate_rebuilds_page(self, source_root, store):
        rel, content_hash = self._write_source(source_root)
        self._write_page(store, rel, content_hash)
        reg = self._registry(source_root, store)
        reg.sync()  # regenerate 需要 registry 能解析 source → 读取原文
        mt = WikiMaintainer(store, reg)
        rebuilt = await mt.regenerate_pages(["product.a.capability.x"], status="draft")
        assert rebuilt == ["product.a.capability.x"]
        page = store.open_page("product.a.capability.x")
        assert page.meta.status == "draft"
        assert any("来源" in s.title or "Evidence" in s.title for s in page.sections)

    def test_deleted_source_reported(self, source_root, store):
        rel, _ = self._write_source(source_root)
        self._write_page(store, rel, sha256_hex("# 产品\n支持智能体身份认证\n"))
        reg = self._registry(source_root, store)
        reg.sync()
        (source_root / self.REL).unlink()
        mt = WikiMaintainer(store, reg)
        report = mt.analyze()
        assert self.REL in report.deleted_sources
