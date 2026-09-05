"""知识库目录读取与查询 API 测试。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from knowledge_admin.catalog import KnowledgeCatalog
from knowledge_admin.usage_classifier import UsageClassifier

# 真实知识库目录
KB_ROOT = Path(__file__).parent.parent.parent / "agent-security-briefs"


# ═══════════════════════════════════════════════════════════════
# KnowledgeCatalog - 路径安全
# ═══════════════════════════════════════════════════════════════


class TestPathSafety:
    """路径穿越和安全校验。"""

    def setup_method(self):
        self.catalog = KnowledgeCatalog(KB_ROOT)

    def test_reject_parent_traversal(self):
        with pytest.raises(ValueError, match="\\.\\."):
            self.catalog.get_document("../../../etc/passwd")

    def test_reject_absolute_path(self):
        with pytest.raises(ValueError):
            self.catalog.get_document("/etc/passwd")

    def test_reject_non_md_file(self):
        with pytest.raises(ValueError, match="Markdown"):
            self.catalog.get_document("CLAUDE.txt")

    def test_reject_empty_path(self):
        with pytest.raises(ValueError, match="空"):
            self.catalog.get_document("")


# ═══════════════════════════════════════════════════════════════
# KnowledgeCatalog - 目录树
# ═══════════════════════════════════════════════════════════════


class TestDirectoryTree:
    """目录树构建。"""

    def setup_method(self):
        self.catalog = KnowledgeCatalog(KB_ROOT)

    def test_tree_has_root_name(self):
        tree = self.catalog.build_tree()
        assert tree["root_name"] == "agent-security-briefs"

    def test_tree_has_children(self):
        tree = self.catalog.build_tree()
        assert len(tree["children"]) > 0

    def test_tree_contains_file_nodes(self):
        tree = self.catalog.build_tree()
        files = [n for n in tree["children"] if n["node_type"] == "file"]
        assert len(files) > 0

    def test_tree_excludes_git(self):
        tree = self.catalog.build_tree()
        names = [n["name"] for n in tree["children"]]
        assert ".git" not in names

    def test_tree_exclude_raw_dir(self):
        tree = self.catalog.build_tree(include_raw=False)
        all_nodes = _flatten_tree(tree["children"])
        assert not any("原始文档" in n["path"] for n in all_nodes if n["node_type"] == "dir")

    def test_tree_exclude_empty_dirs(self):
        tree = self.catalog.build_tree(include_empty=False)
        for node in _flatten_tree(tree["children"]):
            if node["node_type"] == "dir":
                assert len(node.get("children", [])) > 0


# ═══════════════════════════════════════════════════════════════
# KnowledgeCatalog - 文档 ID
# ═══════════════════════════════════════════════════════════════


class TestDocumentId:
    """文档 ID 映射。"""

    def test_stable_hash(self):
        id1 = KnowledgeCatalog.get_document_id("1-智能体身份安全/overview.md")
        id2 = KnowledgeCatalog.get_document_id("1-智能体身份安全/overview.md")
        assert id1 == id2

    def test_different_paths_different_ids(self):
        id1 = KnowledgeCatalog.get_document_id("1-智能体身份安全/overview.md")
        id2 = KnowledgeCatalog.get_document_id("3-AI-BOM/overview.md")
        assert id1 != id2

    def test_normalize_backslash(self):
        id1 = KnowledgeCatalog.get_document_id("1-智能体身份安全/overview.md")
        id2 = KnowledgeCatalog.get_document_id("1-智能体身份安全\\overview.md")
        assert id1 == id2

    def test_resolve_roundtrip(self):
        catalog = KnowledgeCatalog(KB_ROOT)
        rel_path = "1-智能体身份安全/overview.md"
        doc_id = KnowledgeCatalog.get_document_id(rel_path)
        resolved = catalog.resolve_document_id(doc_id)
        assert resolved == rel_path


# ═══════════════════════════════════════════════════════════════
# KnowledgeCatalog - 文档读取
# ═══════════════════════════════════════════════════════════════


class TestDocumentRead:
    """文档读取。"""

    def setup_method(self):
        self.catalog = KnowledgeCatalog(KB_ROOT)

    def test_read_existing_file(self):
        doc = self.catalog.get_document("1-智能体身份安全/overview.md")
        assert doc["relative_path"] == "1-智能体身份安全/overview.md"
        assert len(doc["content"]) > 0
        assert doc["content_hash"].startswith("sha256:")
        assert doc["size"] > 0

    def test_read_nonexistent_file(self):
        with pytest.raises(ValueError, match="不存在"):
            self.catalog.get_document("nonexistent/file.md")


# ═══════════════════════════════════════════════════════════════
# KnowledgeCatalog - 搜索
# ═══════════════════════════════════════════════════════════════


class TestSearch:
    """搜索功能。"""

    def setup_method(self):
        self.catalog = KnowledgeCatalog(KB_ROOT)

    def test_search_by_filename(self):
        results = self.catalog.search("overview")
        assert len(results) > 0
        assert any("overview" in r["name"].lower() for r in results)

    def test_search_by_content(self):
        results = self.catalog.search("智能体")
        assert len(results) > 0

    def test_search_no_match(self):
        results = self.catalog.search("zzz_nonexistent_zzz")
        assert len(results) == 0


# ═══════════════════════════════════════════════════════════════
# UsageClassifier - 用途分类
# ═══════════════════════════════════════════════════════════════


class TestUsageClassifier:
    """文件用途分类。"""

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("CLAUDE.md", "entry_router"),
            ("AGENTS.md", "entry_router"),
            ("_index/folder-routing.md", "folder_router"),
            ("skills/common-rules.md", "role_workflow"),
            ("0-产品全景/matrix.md", "product_map"),
            ("1-智能体身份安全/overview.md", "product_fact"),
            ("1-智能体身份安全/market-brief.md", "market_brief"),
            ("1-智能体身份安全/sales-brief.md", "sales_brief"),
            ("shared/hot-event-playbook.md", "shared_fact"),
            ("qa-log.md", "maintenance_log"),
        ],
    )
    def test_classify(self, path, expected):
        assert UsageClassifier.classify(path) == expected

    def test_direct_scoring_prompt_files(self):
        assert UsageClassifier.is_direct_scoring_prompt("1-智能体身份安全/overview.md")
        assert UsageClassifier.is_direct_scoring_prompt("3-AI-BOM/market-brief.md")
        assert not UsageClassifier.is_direct_scoring_prompt("0-产品全景/matrix.md")

    def test_protected_path_matches_direct_scoring(self):
        assert UsageClassifier.is_protected_path("1-智能体身份安全/overview.md")
        assert not UsageClassifier.is_protected_path("0-产品全景/matrix.md")

    def test_editable(self):
        assert UsageClassifier.is_editable("1-智能体身份安全/overview.md")
        assert not UsageClassifier.is_editable("skills/common-rules.md")
        assert not UsageClassifier.is_editable("CLAUDE.md")

    def test_loader_relevant(self):
        root = KB_ROOT
        assert UsageClassifier.is_loader_relevant(root, "1-智能体身份安全/overview.md")
        assert not UsageClassifier.is_loader_relevant(root, "CLAUDE.md")

    def test_get_file_metadata(self):
        root = KB_ROOT
        metadata = UsageClassifier.get_file_metadata(
            "1-智能体身份安全/overview.md",
            "# Test content",
            root_dir=root,
        )
        assert metadata["knowledge_role"] == "product_fact"
        assert metadata["direct_scoring_prompt"] is True
        assert metadata["editable"] is True
        assert metadata["protected_path"] is True
        assert metadata["content_hash"].startswith("sha256:")
        assert metadata["document_id"]

    def test_usage_legend(self):
        legend = UsageClassifier.get_usage_legend()
        assert len(legend) > 0
        assert all("role" in item and "label" in item for item in legend)


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


def _flatten_tree(nodes: list[dict]) -> list[dict]:
    """递归展平目录树。"""
    result: list[dict] = []
    for node in nodes:
        result.append(node)
        if node.get("children"):
            result.extend(_flatten_tree(node["children"]))
    return result
