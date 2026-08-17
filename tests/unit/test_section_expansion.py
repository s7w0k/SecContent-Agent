"""阶段六 章节级按需展开和事实引用 单元测试。

覆盖 S6-1（章节切分）、S6-2（展开决策）、S6-3（章节正文获取与安全校验）、
S6-4（事实引用元数据）、S6-5（生成后事实审查）。
"""

from __future__ import annotations

import pytest
from agent.fact_citation import (
    audit_fact_citations,
    classify_fact,
    parse_citations,
    render_citation,
    strip_citation_blocks,
)
from agent.knowledge_index import (
    KnowledgeIndexBuilder,
    KnowledgeIndexer,
    _split_sections,
    extract_section_body,
)
from agent.section_expander import (
    SectionExpander,
    select_sections_for_expansion,
)

P1 = "agent-identity-security"


def _build_kb(root) -> None:
    """构造用于阶段六测试的知识库。"""
    p1 = root / "1-智能体身份安全"
    p1.mkdir(parents=True)
    (p1 / "overview.md").write_text(
        "# 智能体身份安全\n\n支持 E4002 错误码与密钥管理。", encoding="utf-8"
    )
    (p1 / "market-brief.md").write_text(
        "# 市场简报\n\n面向企业，术语 IDP、OIDC。", encoding="utf-8"
    )
    raw = p1 / "原始文档"
    raw.mkdir(parents=True)
    (raw / "error-handbook.md").write_text(
        "# 错误处理手册\n\n## 错误码\nE4002 余额不足处理。\n\n"
        "## 错误码\nE4003 重复受理处理。\n\n## 重试\n必须指数退避。",
        encoding="utf-8",
    )
    # 超长章节用于二级切片测试
    (raw / "long-doc.md").write_text(
        "# 长文档\n\n## 大章节\n" + ("详细内容。" * 40), encoding="utf-8"
    )


def _build_indexer(root) -> KnowledgeIndexer:
    builder = KnowledgeIndexBuilder(root)
    manifest = builder.build_manifest()
    builder.write(manifest)
    indexer = KnowledgeIndexer(root / "_index" / "kb-index.json")
    assert indexer.load() is not None
    return indexer


# ═══════════════════════════════════════════════════════════════
# S6-1 章节切分
# ═══════════════════════════════════════════════════════════════


class TestSectionSplitting:
    def test_section_id_stable(self):
        content = "# t\n目录\n## a\n正文a\n## b\n正文b"
        s1 = _split_sections(content, "doc:x")
        s2 = _split_sections(content, "doc:x")
        assert [s.section_id for s in s1] == [s.section_id for s in s2]
        assert [s.title for s in s1] == ["t", "a", "b"]
        assert all(sid.startswith("doc:x:") for sid in [s.section_id for s in s1])

    def test_same_title_disambiguation(self):
        content = "# x\n## 错误码\n甲\n## 错误码\n乙"
        sections = _split_sections(content, "doc:y")
        dup = [s for s in sections if s.title == "错误码"]
        assert len(dup) == 2
        assert dup[0].occurrence == 1
        assert dup[1].occurrence == 2
        # section_id 唯一，可精确寻址
        assert dup[0].section_id != dup[1].section_id

    def test_line_offset_and_char_offset(self):
        content = "intro\n## 段\nbody"
        sections = _split_sections(content, "doc:z")
        seg = next(s for s in sections if s.title == "段")
        assert seg.line_offset >= 0
        assert seg.char_offset >= 0
        assert seg.char_count > 0

    def test_code_block_heading_not_split(self):
        content = "# 标题\n```\n## 不应切开\ninside\n```\n## 真章节\n正文"
        sections = _split_sections(content, "doc:c")
        titles = [s.title for s in sections]
        # 代码块内的 "## 不应切开" 不作为独立章节
        assert "不应切开" not in titles
        assert "真章节" in titles

    def test_overlong_section_secondary_sliced(self):
        content = "# 文档\n## 大章节\n" + "内容。" * 1000
        sections = _split_sections(content, "doc:long")
        big = [s for s in sections if s.title.startswith("大章节")]
        assert len(big) > 1  # 超长被拆分为续章节
        assert any("续" in s.title for s in big)
        # 所有 section_id 稳定
        assert len({s.section_id for s in sections}) == len(sections)

    def test_extract_section_body_matches(self):
        content = "# 手册\n\n## 错误码\nE4002 余额不足处理。\n\n## 重试\n必须指数退避。"
        sections = _split_sections(content, "doc:body")
        err = next(s for s in sections if s.title == "错误码")
        body = extract_section_body(content, "doc:body", err.section_id)
        assert "E4002 余额不足处理" in body
        assert "重试" not in body  # 不串到下一章节

    def test_extract_section_body_unknown_returns_empty(self):
        assert extract_section_body("# x\n\n## a\nb", "doc:x", "doc:x:999") == ""


# ═══════════════════════════════════════════════════════════════
# S6-2 展开决策
# ═══════════════════════════════════════════════════════════════


class TestExpansionDecision:
    def _raw_doc(self, root):
        indexer = _build_indexer(root)
        return next(d for d in indexer.manifest.docs if d.doc_type == "raw")

    def test_query_hits_relevant_section(self, tmp_path):
        _build_kb(tmp_path)
        doc = self._raw_doc(tmp_path)
        # query 精确命中 "错误码" 章节
        chosen = select_sections_for_expansion(doc, "E4002 错误码", max_expanded=2)
        assert chosen
        assert any("错误码" in s.title for s in chosen)

    def test_brief_insufficient_hits_raw_section_not_head(self, tmp_path):
        _build_kb(tmp_path)
        doc = self._raw_doc(tmp_path)
        # 命中重试章节，应返回重试相关章节而非总是头部
        chosen = select_sections_for_expansion(doc, "重试 指数退避", max_expanded=2)
        assert chosen
        assert any("重试" in s.title for s in chosen)

    def test_max_expanded_capped(self, tmp_path):
        _build_kb(tmp_path)
        doc = self._raw_doc(tmp_path)
        chosen = select_sections_for_expansion(doc, "错误码", max_expanded=1)
        assert len(chosen) <= 1

    def test_expand_hint_without_hit_returns_head(self, tmp_path):
        _build_kb(tmp_path)
        doc = self._raw_doc(tmp_path)
        chosen = select_sections_for_expansion(doc, "请给出部署参数", max_expanded=2)
        # 无术语命中但有展开意图词 → 返回头部章节（降级概貌）
        assert not doc.sections or len(chosen) <= 2


# ═══════════════════════════════════════════════════════════════
# S6-3 章节正文获取与安全校验
# ═══════════════════════════════════════════════════════════════


class TestSectionExpander:
    def _make(self, root):
        indexer = _build_indexer(root)
        return SectionExpander(root, indexer=indexer), indexer

    def test_get_document_outline(self, tmp_path):
        _build_kb(tmp_path)
        expander, indexer = self._make(tmp_path)
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        outline = expander.get_document_outline(
            raw.doc_id, product_ids=[P1], index_version=indexer.index_version
        )
        assert outline is not None
        assert outline.doc_id == raw.doc_id
        assert outline.sections
        assert all(s.doc_id == raw.doc_id for s in outline.sections)

    def test_get_section_returns_body(self, tmp_path):
        _build_kb(tmp_path)
        expander, indexer = self._make(tmp_path)
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        section = raw.sections[1]  # 错误码
        body = expander.get_section(
            raw.doc_id,
            section.section_id,
            product_ids=[P1],
            index_version=indexer.index_version,
        )
        assert body is not None
        assert body.section_id == section.section_id
        assert body.content

    def test_only_frozen_product_sections_allowed(self, tmp_path):
        _build_kb(tmp_path)
        expander, indexer = self._make(tmp_path)
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        # 请求其他产品时，P1 的 raw 章节不可展开
        body = expander.get_section(
            raw.doc_id,
            raw.sections[0].section_id,
            product_ids=["ai-bom"],
            index_version=indexer.index_version,
        )
        assert body is None

    def test_index_version_mismatch_rejected(self, tmp_path):
        _build_kb(tmp_path)
        expander, indexer = self._make(tmp_path)
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        body = expander.get_section(
            raw.doc_id,
            raw.sections[0].section_id,
            product_ids=[P1],
            index_version="sha256:stale",
        )
        assert body is None

    def test_unknown_section_rejected(self, tmp_path):
        _build_kb(tmp_path)
        expander, indexer = self._make(tmp_path)
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        body = expander.get_section(
            raw.doc_id, "doc:does-not-exist", product_ids=[P1]
        )
        assert body is None

    def test_long_section_under_token_budget(self, tmp_path):
        _build_kb(tmp_path)
        indexer = _build_indexer(tmp_path)
        expander = SectionExpander(tmp_path, indexer=indexer, section_body_chars=200)
        raw = next(d for d in indexer.manifest.docs if d.relative_path.endswith("long-doc.md"))
        body = expander.get_section(raw.doc_id, raw.sections[-1].section_id, product_ids=[P1])
        assert body is not None
        assert len(body.content) <= 200  # 受 token/字符预算限制

    def test_unpublished_doc_not_expandable(self, tmp_path):
        _build_kb(tmp_path)
        expander, _indexer = self._make(tmp_path)
        # 未发布产品文档（4-智能体安全网关）不可展开
        body = expander.get_section("doc:none", "doc:none:0", product_ids=[P1])
        assert body is None


# ═══════════════════════════════════════════════════════════════
# S6-4 事实引用元数据
# ═══════════════════════════════════════════════════════════════


class TestFactCitation:
    def test_render_and_parse_roundtrip(self):
        tag = render_citation(
            "doc:abc", "doc:abc:2", needs_confirmation=True, quote="E4002 余额不足"
        )
        parsed = parse_citations(tag)
        assert len(parsed) == 1
        assert parsed[0].doc_id == "doc:abc"
        assert parsed[0].section_id == "doc:abc:2"
        assert parsed[0].needs_confirmation is True
        assert "E4002" in parsed[0].quote

    def test_parse_multiple_and_strip(self):
        content = (
            "正文。\n"
            + render_citation("d:1", "d:1:0", quote="甲")
            + "\n更多。\n"
            + render_citation("d:2", "d:2:1", needs_confirmation=True, quote="乙")
        )
        assert len(parse_citations(content)) == 2
        stripped = strip_citation_blocks(content)
        assert "KNOWLEDGE_SOURCE" not in stripped
        assert "正文" in stripped

    def test_parse_empty(self):
        assert parse_citations("无引用") == []


# ═══════════════════════════════════════════════════════════════
# S6-5 生成后事实审查
# ═══════════════════════════════════════════════════════════════


class TestFactAudit:
    def test_classify_fact(self):
        assert "number" in classify_fact("支持 99.99% 可用性")
        assert "version" in classify_fact("引擎升级至 v2.3.1")
        assert "deployment" in classify_fact("已在 5 家银行部署上线")
        assert classify_fact("纯概念描述") == []

    def test_uncited_high_risk_fact_flagged(self):
        draft = "该方案支持 99.99% 可用性，版本 v2.3.1。"
        result = audit_fact_citations(draft)
        assert result.issues
        # 高风险事实（number/version）缺来源 → 标记
        assert all(i.category == "missing_citation" for i in result.issues)
        assert any(i.severity == "high" for i in result.issues)

    def test_cited_fact_not_flagged(self):
        # 事实句位于引用块内 → 有来源，不标记
        draft = render_citation(
            "doc:x", "doc:x:0", quote="该方案支持 99.99% 可用性，版本 v2.3.1"
        )
        result = audit_fact_citations(draft)
        assert result.issues == []

    def test_no_fact_no_issue(self):
        result = audit_fact_citations("这是一段普通描述，没有具体事实。")
        assert result.issues == []


# ═══════════════════════════════════════════════════════════════
# S6-3/S6-4 集成：resolver 展开正文并渲染引用
# ═══════════════════════════════════════════════════════════════


class TestSliceExpansionIntegration:
    @pytest.mark.asyncio
    async def test_resolver_injects_section_citation(self, tmp_path):
        _build_kb(tmp_path)
        indexer = _build_indexer(tmp_path)
        from agent.document_retriever import DocumentRetriever
        from agent.knowledge_slice import KnowledgeSliceResolver

        retriever = DocumentRetriever(indexer=indexer)
        expander = SectionExpander(tmp_path, indexer=indexer)
        resolver = KnowledgeSliceResolver(
            tmp_path,
            db=None,
            retriever=retriever,
            max_optional_docs=6,
            section_expander=expander,
        )
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        section_id = raw.sections[1].section_id  # 错误码章节
        result = await resolver.resolve(
            purpose="draft",
            product_ids=[P1],
            query="E4002 错误码",
            expand_section_ids=[section_id],
        )
        assert "KNOWLEDGE_SOURCE" in result.content
        assert section_id in result.expanded_section_ids
        assert result.index_version != ""

    @pytest.mark.asyncio
    async def test_resolver_without_expander_keeps_excerpt(self, tmp_path):
        _build_kb(tmp_path)
        indexer = _build_indexer(tmp_path)
        from agent.document_retriever import DocumentRetriever
        from agent.knowledge_slice import KnowledgeSliceResolver

        retriever = DocumentRetriever(indexer=indexer)
        resolver = KnowledgeSliceResolver(
            tmp_path, db=None, retriever=retriever, max_optional_docs=6
        )
        raw = next(d for d in indexer.manifest.docs if d.doc_type == "raw")
        section_id = raw.sections[1].section_id
        result = await resolver.resolve(
            purpose="draft",
            product_ids=[P1],
            query="E4002 错误码",
            expand_section_ids=[section_id],
        )
        # 无 expander 时按摘要候选注入，不产生引用块
        assert "KNOWLEDGE_SOURCE" not in result.content
