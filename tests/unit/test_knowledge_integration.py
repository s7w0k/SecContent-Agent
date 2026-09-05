"""知识库管理集成测试 - 验证端到端流程。"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from knowledge_admin.catalog import KnowledgeCatalog
from knowledge_admin.file_store import KnowledgeFileStore
from knowledge_admin.usage_classifier import UsageClassifier

# ═══════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════


def _create_test_kb(tmpdir: Path) -> Path:
    """创建测试知识库目录。"""
    kb_root = tmpdir / "agent-security-briefs"
    kb_root.mkdir()

    # 创建核心打分文件
    (kb_root / "1-智能体身份安全").mkdir()
    (kb_root / "1-智能体身份安全" / "overview.md").write_text(
        "# 智能体身份安全\n\n## 产品定位\n\n提供身份治理能力。\n",
        encoding="utf-8",
    )
    (kb_root / "1-智能体身份安全" / "market-brief.md").write_text(
        "# 市场简报\n\n## 热点传播\n\nMCP安全是热点。\n",
        encoding="utf-8",
    )

    (kb_root / "3-AI-BOM").mkdir()
    (kb_root / "3-AI-BOM" / "overview.md").write_text(
        "# AI-BOM\n\n## 产品定位\n\nAI物料清单。\n",
        encoding="utf-8",
    )
    (kb_root / "3-AI-BOM" / "market-brief.md").write_text(
        "# AI-BOM 市场简报\n\n## 热点\n\n供应链安全。\n",
        encoding="utf-8",
    )

    (kb_root / "shared").mkdir()
    (kb_root / "shared" / "hot-event-playbook.md").write_text(
        "# 热点匹配规则\n\n## 匹配策略\n\n按关键词匹配。\n",
        encoding="utf-8",
    )

    # 创建其他文件
    (kb_root / "0-产品全景").mkdir()
    (kb_root / "0-产品全景" / "matrix.md").write_text(
        "# 产品矩阵\n\n## 概述\n\n产品关系图。\n", encoding="utf-8"
    )

    (kb_root / "CLAUDE.md").write_text("# CLAUDE\n\n入口文件。\n", encoding="utf-8")

    return kb_root


# ═══════════════════════════════════════════════════════════════
# 集成测试
# ═══════════════════════════════════════════════════════════════


class TestDraftToPublishWorkflow:
    """草稿到发布完整流程集成测试。"""

    def test_file_store_atomic_write_roundtrip(self, tmp_path):
        """原子写入后读取内容一致。"""
        kb_root = _create_test_kb(tmp_path)
        store = KnowledgeFileStore(kb_root)

        rel_path = "1-智能体身份安全/market-brief.md"
        original = store.read_file(rel_path)

        new_content = "# 市场简报\n\n## 热点传播\n\nMCP安全是热点。更新版本。\n"
        written_hash = store.atomic_write(rel_path, new_content)

        # Verify content changed
        written = store.read_file(rel_path)
        assert written == new_content
        assert written != original
        assert written_hash.startswith("sha256:")

        # Verify hash matches
        expected_hash = f"sha256:{hashlib.sha256(new_content.encode('utf-8')).hexdigest()}"
        assert written_hash == expected_hash

    def test_file_store_no_temp_residual(self, tmp_path):
        """原子写入后无临时文件残留。"""
        kb_root = _create_test_kb(tmp_path)
        store = KnowledgeFileStore(kb_root)

        rel_path = "1-智能体身份安全/overview.md"
        store.atomic_write(rel_path, "# Updated\n")

        # Check no .kbp-tmp files remain
        tmp_files = list(kb_root.rglob("*.kbp-tmp"))
        assert len(tmp_files) == 0

    def test_catalog_and_file_store_consistent_paths(self, tmp_path):
        """Catalog 和 FileStore 路径校验一致。"""
        kb_root = _create_test_kb(tmp_path)
        catalog = KnowledgeCatalog(kb_root)
        store = KnowledgeFileStore(kb_root)

        rel_path = "1-智能体身份安全/overview.md"

        # Both should read the same content
        doc = catalog.get_document(rel_path)
        content = store.read_file(rel_path)
        assert doc["content"] == content

        # Both should reject the same unsafe paths
        with pytest.raises(ValueError):
            catalog.get_document("../../../etc/passwd")
        with pytest.raises(ValueError):
            store.read_file("../../../etc/passwd")


class TestKnowledgeRuntimeRefresh:
    """知识运行时热刷新测试。"""

    @pytest.mark.asyncio
    async def test_refresh_detects_file_change(self, tmp_path):
        """文件变更后 refresh_if_changed 返回 True。"""
        from agent.knowledge import KnowledgeLoader
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        kb_root = _create_test_kb(tmp_path)

        # Create a mock app_state with a real loader
        loader = KnowledgeLoader(docs_dir=str(kb_root))
        await loader.load(force=True)

        mock_scorer = MagicMock()
        mock_scorer.refresh_prompt = MagicMock()

        app_state = MagicMock()
        app_state.knowledge_loader = loader
        app_state.scorer_v2 = mock_scorer
        app_state.db = None

        refresher = KnowledgeRuntimeRefresher(app_state)

        # No change yet
        changed = await refresher.refresh_if_changed()
        assert changed is False

        # Modify a file
        target = kb_root / "1-智能体身份安全" / "overview.md"
        target.write_text("# 智能体身份安全\n\n## 产品定位\n\n更新后的内容。\n", encoding="utf-8")

        # Should detect change
        changed = await refresher.refresh_if_changed()
        assert changed is True
        mock_scorer.refresh_prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_for_task_returns_hash(self, tmp_path):
        """prepare_for_task 返回当前知识哈希。"""
        from agent.knowledge import KnowledgeLoader
        from agent.knowledge_runtime import KnowledgeRuntimeRefresher

        kb_root = _create_test_kb(tmp_path)
        loader = KnowledgeLoader(docs_dir=str(kb_root))
        await loader.load(force=True)

        app_state = MagicMock()
        app_state.knowledge_loader = loader
        app_state.scorer_v2 = MagicMock()
        app_state.db = None

        refresher = KnowledgeRuntimeRefresher(app_state)
        hash_value = await refresher.prepare_for_task()
        assert hash_value == loader._last_hash


class TestPublicationLockIntegration:
    """发布锁集成测试。"""

    @pytest.mark.asyncio
    async def test_lock_prevents_concurrent_acquire(self):
        """锁已存在时无法再次获取。"""
        from knowledge_admin.publication import LOCK_KEY, KnowledgePublicationService

        db = MagicMock()
        locks_collection = AsyncMock()
        db.__getitem__ = MagicMock(return_value=locks_collection)

        # First insert succeeds
        locks_collection.insert_one = AsyncMock(return_value=None)
        service = KnowledgePublicationService(db, "/tmp")
        acquired = await service.acquire_lock("user-1", "pub-1")
        assert acquired is True

        # Second insert fails (duplicate key)
        locks_collection.insert_one = AsyncMock(side_effect=Exception("duplicate key"))
        locks_collection.find_one = AsyncMock(
            return_value={
                "lock_key": LOCK_KEY,
                "publication_id": "pub-1",
                "expires_at": datetime.now(UTC).replace(year=2099),
            }
        )
        acquired = await service.acquire_lock("user-2", "pub-2")
        assert acquired is False


class TestUsageClassifierConsistency:
    """用途分类与 Loader 一致性测试。"""

    def test_direct_scoring_files_match_loader(self, tmp_path):
        """UsageClassifier 的 direct_scoring_prompt 与 Loader 的 as_scoring_prompt 一致。"""
        kb_root = _create_test_kb(tmp_path)

        # Files that UsageClassifier says are direct scoring
        direct_files = [
            "1-智能体身份安全/overview.md",
            "1-智能体身份安全/market-brief.md",
            "3-AI-BOM/overview.md",
            "3-AI-BOM/market-brief.md",
            "shared/hot-event-playbook.md",
        ]

        for f in direct_files:
            assert UsageClassifier.is_direct_scoring_prompt(f), f"{f} should be direct scoring"

        # Verify these files actually exist in the test KB
        for f in direct_files:
            assert (kb_root / f).exists(), f"{f} should exist"

    def test_editable_files_excluded_correctly(self):
        """不可编辑文件正确排除。"""
        non_editable = [
            "CLAUDE.md",
            "AGENTS.md",
            "qa-log.md",
            "skills/common-rules.md",
            "_index/folder-routing.md",
        ]
        for f in non_editable:
            assert not UsageClassifier.is_editable(f), f"{f} should not be editable"

        editable = [
            "1-智能体身份安全/overview.md",
            "0-产品全景/matrix.md",
            "shared/hot-event-playbook.md",
        ]
        for f in editable:
            assert UsageClassifier.is_editable(f), f"{f} should be editable"
