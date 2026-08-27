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


def test_diff_detects_rename_by_content_hash(source_root):
    """§7.1：同内容被移动文件 → RENAMED，而不是 DELETE + NEW。"""
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    make_source_file(source_root, "1-产品/a.md", "# v1")
    reg.sync()

    (source_root / "1-产品").mkdir(exist_ok=True)
    (source_root / "1-产品/a.md").replace(source_root / "1-产品/b.md")
    report = reg.diff()
    assert report.renamed == [("1-产品/a.md", "1-产品/b.md")]
    assert "1-产品/a.md" not in report.deleted
    assert "1-产品/b.md" not in report.new


def test_source_allowed_extensions_limit(source_root):
    """§7.3：只注册允许扩展名的文件。"""
    make_source_file(source_root, "1-产品/ok.md", "# v1")
    make_source_file(source_root, "1-产品/skip.txt", "plain text")
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    paths = reg.scan_relative_paths()
    assert "1-产品/ok.md" in paths
    assert "1-产品/skip.txt" not in paths


def test_source_size_limit_skips(source_root):
    """§19.5/§7.3：超过大小上限的源文件被跳过（DoS 防护）。"""
    make_source_file(source_root, "1-产品/big.md", "x" * 2048)
    reg = SourceRegistry(
        source_root,
        source_root / "_wiki" / "_meta" / "source-registry.json",
        max_source_bytes=1024,
    )
    state = reg.build_state()
    assert "1-产品/big.md" not in state


def test_secret_quarantine_marks_entry(source_root):
    """§19.4：源码命中 API Key → 标记 quarantined，不自动发布。"""
    make_source_file(source_root, "1-产品/overview.md", "# v1\napi_key: sk-live-1234567890abcdef\n")
    reg = SourceRegistry(source_root, source_root / "_wiki" / "_meta" / "source-registry.json")
    reg.sync()
    entry = reg.get_by_path("1-产品/overview.md")
    assert entry is not None
    assert entry.status == "quarantined"
    assert "api_key" in entry.secret_kinds


def test_secret_quarantine_disabled(source_root):
    make_source_file(source_root, "1-产品/overview.md", "password: hunter2")
    reg = SourceRegistry(
        source_root,
        source_root / "_wiki" / "_meta" / "source-registry.json",
        secret_quarantine=False,
    )
    reg.sync()
    assert reg.get_by_path("1-产品/overview.md").status == "active"


def test_resolve_source_path_rejects_unsafe(tmp_path):
    from agent.wiki.source_registry import WikiPathError, resolve_source_path

    for bad in ["../etc/passwd", "/etc/passwd", "a/../../b.md", ".."]:
        try:
            resolve_source_path(tmp_path, bad)
        except WikiPathError:
            continue
        raise AssertionError(f"应拒绝不安全路径: {bad!r}")
