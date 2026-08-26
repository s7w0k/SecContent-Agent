"""PR-03 Wiki Linter 单元测试。"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef, WikiRelation
from agent.wiki.linter import WikiLinter
from helpers import make_page


def test_lint_pass_for_grounded_capability(store):
    page = make_page(
        "product.a.capability.x",
        product_id="a",
        source_refs=[SourceRef(source_id="s1", relative_path="a.md", content_hash="h")],
    )
    store.write_page(page)
    result = WikiLinter(store).lint()
    assert result.ok
    assert not result.errors


def test_lint_broken_link(store):
    page = make_page(
        "product.a.capability.x",
        product_id="a",
        relations=[WikiRelation(relation_type="related_to", target_page_id="concept.missing")],
        source_refs=[SourceRef(source_id="s1", relative_path="a.md", content_hash="h")],
    )
    store.write_page(page)
    result = WikiLinter(store).lint()
    assert any(e.startswith("broken_link[") for e in result.errors)


def test_lint_source_ref_invalid(store):
    page = make_page(
        "product.a.capability.x",
        product_id="a",
        source_refs=[SourceRef(source_id="", relative_path="a.md", content_hash="h")],
    )
    store.write_page(page)
    result = WikiLinter(store).lint()
    assert any(e.startswith("source_ref_valid[") for e in result.errors)


def test_lint_ungrounded_capability(store):
    page = make_page("product.a.capability.x", product_id="a")
    store.write_page(page)
    result = WikiLinter(store).lint()
    assert any(e.startswith("ungrounded_claim[") for e in result.errors)


def test_lint_duplicate_page_id(tmp_path):

    store = tmp_path / "wiki"
    store.mkdir()
    # 两个不同路径但相同 page_id 的文件
    from agent.wiki.store import WikiStore

    ws = WikiStore(store)
    ws.write_page(make_page("product.a", product_id="a"))
    # 手动再写一个相同 page_id 到其它路径

    md = make_page("product.a", product_id="a").render_markdown()
    dup = store / "products" / "a" / "dup.md"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_text(md, encoding="utf-8")

    result = WikiLinter(ws).lint()
    assert any(e.startswith("duplicate_page_id[") for e in result.errors)


def test_lint_orphan_warning(store):
    # 一个没有入链的 capability
    store.write_page(
        make_page(
            "product.a.capability.orphan",
            product_id="a",
            source_refs=[SourceRef(source_id="s1", relative_path="a.md", content_hash="h")],
        )
    )
    result = WikiLinter(store).lint()
    assert any("orphan_page" in w for w in result.warnings)


def test_lint_stale_source(source_root, wiki_root, store):
    from agent.wiki.source_registry import SourceRegistry
    from helpers import make_source_file

    make_source_file(source_root, "1-产品/overview.md", "# v1\n")
    registry = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    registry.sync()
    entry = registry.get_by_path("1-产品/overview.md")

    page = make_page(
        "product.a.capability.x",
        product_id="a",
        source_refs=[
            SourceRef(
                source_id=entry.source_id,
                relative_path="1-产品/overview.md",
                content_hash="sha256:wronghash",
            )
        ],
    )
    store.write_page(page)
    result = WikiLinter(store, registry=registry).lint()
    assert any(e.startswith("stale_source[") for e in result.errors)
