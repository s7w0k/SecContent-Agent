"""PR-01 WikiStore 单元测试：frontmatter 解析、访问、路径安全、source ref 校验。"""

from __future__ import annotations

import pytest
from agent.wiki.contracts import SourceRef, WikiPageNotFound, WikiPathError
from agent.wiki.store import (
    WikiStore,
    parse_frontmatter,
    parse_wiki_page,
)
from helpers import make_page


def test_write_and_open_roundtrip(store: WikiStore):
    page = make_page("product.agent_identity.capability.identity_auth", product_id="agent_identity")
    store.write_page(page)
    reopened = store.open_page("product.agent_identity.capability.identity_auth")
    assert reopened.meta.page_id == page.meta.page_id
    assert reopened.meta.product_id == "agent_identity"
    assert reopened.meta.page_type == "capability"


def test_open_missing_page_raises(store: WikiStore):
    with pytest.raises(WikiPageNotFound):
        store.open_page("concept.does_not_exist")


def test_page_exists_after_write(store: WikiStore):
    assert not store.page_exists("product.px")
    store.write_page(make_page("product.px", product_id="px"))
    assert store.page_exists("product.px")


def test_list_page_ids(store: WikiStore):
    store.write_page(make_page("product.px", product_id="px"))
    store.write_page(make_page("concept.ab", page_type="concept"))
    ids = store.list_page_ids()
    assert "product.px" in ids
    assert "concept.ab" in ids


def test_parse_frontmatter_yaml_map():
    data = parse_frontmatter("page_id: product.px\ntitle: 产品\nsource_refs:\n  - source_id: s\n")
    assert data["page_id"] == "product.px"
    assert data["title"] == "产品"


def test_parse_frontmatter_json():
    data = parse_frontmatter('{"page_id": "product.px", "title": "x"}')
    assert data["page_id"] == "product.px"


def test_parse_frontmatter_unknown_keys_ignored():
    data = parse_frontmatter("page_id: product.px\nevil: __import__('os').system('x')\n")
    assert data["page_id"] == "product.px"


def test_parse_wiki_page_sections():
    md = (
        "---\npage_id: product.px\ntitle: 产品\n---\n"
        "# 产品\n\n## Summary\n\n概述内容\n\n## Limitations\n\n限制\n"
    )
    page = parse_wiki_page(md)
    titles = [s.title for s in page.sections]
    assert "Summary" in titles
    assert "Limitations" in titles


def test_check_source_ref_valid(tmp_path):
    page = make_page(
        "concept.identity",
        page_type="concept",
        source_refs=[SourceRef(source_id="s1", relative_path="a.md", content_hash="h")],
    )
    ok = tmp_path / "wiki"
    ok.mkdir()
    store = WikiStore(ok)
    store.write_page(page)
    errors = store.check_source_ref(page.meta.source_refs[0])
    assert errors == []


def test_check_source_ref_empty_source_id(tmp_path):
    page = make_page(
        "concept.identity",
        page_type="concept",
        source_refs=[SourceRef(source_id="", relative_path="a.md", content_hash="h")],
    )
    ok = tmp_path / "wiki"
    store = WikiStore(ok)
    store.write_page(page)
    errors = store.check_source_ref(page.meta.source_refs[0])
    assert any("EMPTY_SOURCE_ID" in e for e in errors)


def test_path_safety_rejects_traversal(tmp_path):
    safe = tmp_path / "wiki"
    safe.mkdir()
    store = WikiStore(safe)
    (tmp_path / "evil.md").write_text("# evil", encoding="utf-8")
    with pytest.raises((WikiPathError, Exception)):
        store.open_page("concept.agent_identity..evil")
