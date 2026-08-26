"""PR-02 Source Registry 单元测试。"""

from __future__ import annotations

from agent.wiki.source_registry import (
    DiffReport,
    SourceRegistry,
    content_hash,
    stable_source_id,
)
from helpers import make_source_file


def test_stable_source_id_is_path_based():
    assert stable_source_id("a/b.md") == stable_source_id("a/b.md")
    assert stable_source_id("a/b.md") != stable_source_id("a/c.md")


def test_content_hash_changes_with_content():
    assert content_hash("abc") != content_hash("abd")


def test_scan_excludes_wiki_and_skills(source_root):
    make_source_file(source_root, "1-产品/overview.md", "# 产品")
    make_source_file(source_root, "_wiki/products/x/index.md", "# wiki")
    make_source_file(source_root, "skills/x.md", "# skill")
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    paths = reg.scan_relative_paths()
    assert "1-产品/overview.md" in paths
    assert not any("_wiki" in p for p in paths)
    assert not any("skills" in p for p in paths)


def test_sync_detects_new_files(source_root):
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/overview.md", "# 产品")
    report = reg.sync()
    assert report.new == ["1-产品/overview.md"]


def test_sync_detects_changed(source_root):
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/overview.md", "# v1")
    reg.sync()
    make_source_file(source_root, "1-产品/overview.md", "# v2")
    report = reg.sync()
    assert report.changed == ["1-产品/overview.md"]


def test_sync_detects_deleted(source_root):
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/overview.md", "# v1")
    reg.sync()
    (source_root / "1-产品/overview.md").unlink()
    report = reg.sync()
    assert "1-产品/overview.md" in report.deleted


def test_sync_persists_and_reloads(source_root):
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/overview.md", "# v1")
    reg.sync()

    reg2 = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    entry = reg2.get_by_path("1-产品/overview.md")
    assert entry is not None
    assert entry.status == "active"


def test_diff_report_summary():
    d = DiffReport(new=["a"], changed=[], unchanged=["b"], deleted=[])
    assert "NEW: 1" in d.summary
