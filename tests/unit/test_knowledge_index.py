"""阶段四 轻量文档索引 单元测试 - schema、发现、增量构建、原子写入、发布对接。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent.knowledge_index import (
    DEFAULT_INDEX_FILENAME,
    KnowledgeIndexBuilder,
    KnowledgeIndexer,
    KnowledgeIndexManifest,
)
from knowledge_admin.publication import KnowledgePublicationService
from models.knowledge_management import KnowledgeDraftStatus, KnowledgePublicationStatus

# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


def _compute_hash(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _make_product_dirs(root, product_dir: str, briefs: list[str] | None = None):
    """创建产品目录及 brief 文件。"""
    pdir = root / product_dir
    pdir.mkdir(parents=True, exist_ok=True)
    briefs = briefs or ["overview.md", "market-brief.md"]
    for brief in briefs:
        (pdir / brief).write_text(f"# 产品{product_dir}\n内容 {brief}", encoding="utf-8")
    return pdir


def _build_knowledge_base(root) -> None:
    """构造一个接近真实目录结构的模拟知识库。"""
    # 已发布产品
    _make_product_dirs(root, "1-智能体身份安全", ["overview.md", "market-brief.md", "sales-brief.md"])
    _make_product_dirs(root, "2-智能体安全", ["overview.md", "market-brief.md"])
    _make_product_dirs(root, "3-AI-BOM", ["overview.md", "market-brief.md"])
    # 未发布产品
    _make_product_dirs(root, "4-智能体安全网关", ["overview.md"])

    # 原始文档 → raw/fallback（内容较长以触发 LLM 分支）
    raw_dir = root / "2-智能体安全" / "原始文档"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "solution-428-完整梳理.md").write_text(
        "# 解决方案\n\n" + "详细方案参数说明。" * 200, encoding="utf-8"
    )

    # 共享目录
    shared = root / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "competitor-brief.md").write_text("# 竞品简报\n内容", encoding="utf-8")

    pan = root / "0-产品全景"
    pan.mkdir(parents=True, exist_ok=True)
    (pan / "overview.md").write_text("# 产品全景\n内容", encoding="utf-8")

    # 排除目录：skills / _index / 海外版
    skills = root / "skills" / "scoring-knowledge"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text("# 技能\n内容", encoding="utf-8")
    idx = root / "_index"
    idx.mkdir(parents=True)
    (idx / "maintenance-checklist.md").write_text("# 维护清单", encoding="utf-8")
    overseas = root / "海外版"
    overseas.mkdir(parents=True)
    (overseas / "overview.md").write_text("# 海外版", encoding="utf-8")

    # 根级管理文档
    for name in ("AGENTS.md", "CLAUDE.md", "README.md", "qa-log.md"):
        (root / name).write_text(f"# {name}", encoding="utf-8")


def _make_draft_doc(
    relative_path: str,
    content_md: str,
    base_content_hash: str,
    draft_id: str = "kbd-s4-001",
) -> dict[str, Any]:
    return {
        "_id": None,
        "draft_id": draft_id,
        "document_id": "doc-hash-s4",
        "relative_path": relative_path,
        "base_content_hash": base_content_hash,
        "content_md": content_md,
        "status": KnowledgeDraftStatus.EDITING,
        "validation": {"status": "pending", "errors": [], "warnings": []},
        "change_summary": "更新",
        "created_by": "u-admin",
        "updated_by": "u-admin",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


def _make_mock_db(collections: dict[str, MagicMock]) -> MagicMock:
    db = MagicMock()
    db.__getitem__.side_effect = lambda name: collections.get(name, MagicMock())
    return db


def _make_mock_loader(hash_value: str = "fake-knowledge-hash") -> MagicMock:
    loader = MagicMock()
    loader.load = AsyncMock()
    loader._last_hash = hash_value
    return loader


# ═══════════════════════════════════════════════════════════════
# KnowledgeIndexBuilder - 发现和分类（S4-2）
# ═══════════════════════════════════════════════════════════════


class TestDiscover:
    def test_raw_enters_index_with_fallback_tier(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        raw_docs = [d for d in manifest.docs if d.doc_type == "raw"]
        assert len(raw_docs) >= 1
        for d in raw_docs:
            assert d.tier == "fallback"
            assert d.doc_id.startswith("doc:")
            assert d.relative_path.endswith(".md")

    def test_skills_and_management_docs_excluded(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        paths = {d.relative_path for d in manifest.docs}
        # 管理文档不进入产品事实索引
        assert "AGENTS.md" not in paths
        assert "CLAUDE.md" not in paths
        assert "README.md" not in paths
        assert "qa-log.md" not in paths
        # skills 目录不进入
        assert not any(p.startswith("skills/") for p in paths)
        assert not any(p.startswith("_index/") for p in paths)
        assert not any(p.startswith("海外版/") for p in paths)

    def test_explicit_mapping_and_required_tier(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        overview = next(
            d for d in manifest.docs
            if d.relative_path == "1-智能体身份安全/overview.md"
        )
        assert overview.doc_type == "overview"
        assert overview.tier == "required"
        assert overview.product_id == "agent-identity-security"
        assert "score" in overview.purposes

    def test_unpublished_product_docs_have_published_false(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        gateway = next(
            d for d in manifest.docs
            if d.relative_path == "4-智能体安全网关/overview.md"
        )
        assert gateway.published is False

    def test_shared_docs_tier_shared(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        shared = [
            d for d in manifest.docs
            if d.relative_path in ("shared/competitor-brief.md", "0-产品全景/overview.md")
        ]
        assert len(shared) == 2
        for d in shared:
            assert d.tier == "shared"
            assert d.product_id is None

    def test_long_raw_docs_have_section_list(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        raw = next(d for d in manifest.docs if d.doc_type == "raw")
        assert len(raw.sections) >= 1
        assert all(s.section_id.startswith(raw.doc_id + ":") for s in raw.sections)


# ═══════════════════════════════════════════════════════════════
# KnowledgeIndexBuilder - 增量构建与确定性（S4-4）
# ═══════════════════════════════════════════════════════════════


class TestIncrementalBuild:
    def test_full_and_incremental_consistent(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        full = builder.build_manifest()
        incremental = builder.build_manifest(previous=full)
        assert incremental.doc_count == full.doc_count
        assert incremental.index_version == full.index_version

    def test_same_input_hash_stable(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        m1 = builder.build_manifest()
        m2 = builder.build_manifest()
        assert m1.index_version == m2.index_version
        assert m1.hash == m2.hash

    def test_file_change_only_rebuilds_corresponding_doc(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        first = builder.build_manifest()
        changed_rel = "1-智能体身份安全/overview.md"
        changed_doc_id = next(
            d.doc_id for d in first.docs if d.relative_path == changed_rel
        )
        first_updated = {
            d.relative_path: d.updated_at for d in first.docs
        }

        # 修改一个文档
        (tmp_path / changed_rel).write_text("# 产品新标题\n全新内容", encoding="utf-8")
        second = builder.build_manifest(previous=first)

        for d in second.docs:
            if d.doc_id == changed_doc_id:
                assert d.content_hash != next(
                    x.content_hash for x in first.docs if x.doc_id == d.doc_id
                )
                assert d.updated_at != first_updated[d.relative_path]
            else:
                # 未变化文档复用上一版元数据（updated_at 不变）
                assert d.updated_at == first_updated[d.relative_path]

    def test_removed_doc_dropped_on_incremental(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        first = builder.build_manifest()
        removed_rel = "2-智能体安全/market-brief.md"
        paths_before = {d.relative_path for d in first.docs}
        assert removed_rel in paths_before

        os.remove(tmp_path / removed_rel)
        second = builder.build_manifest(previous=first)
        paths_after = {d.relative_path for d in second.docs}
        assert removed_rel not in paths_after

    def test_doc_id_stable_across_content_change(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        first = builder.build_manifest()
        changed_rel = "3-AI-BOM/overview.md"
        id_before = next(d.doc_id for d in first.docs if d.relative_path == changed_rel)

        (tmp_path / changed_rel).write_text("# 完全不同的内容", encoding="utf-8")
        second = builder.build_manifest(previous=first)
        id_after = next(d.doc_id for d in second.docs if d.relative_path == changed_rel)
        assert id_before == id_after  # doc_id 基于路径哈希，跨内容稳定


# ═══════════════════════════════════════════════════════════════
# KnowledgeIndexBuilder - 校验与原子写入（S4-3 / S4-4）
# ═══════════════════════════════════════════════════════════════


class TestValidateAndWrite:
    def test_required_briefs_all_present_no_errors(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        assert builder.validate(manifest) == []

    def test_missing_required_brief_returns_error(self, tmp_path):
        _build_knowledge_base(tmp_path)
        os.remove(tmp_path / "1-智能体身份安全" / "overview.md")
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        errors = builder.validate(manifest)
        assert any("MISSING_REQUIRED_BRIEF" in e for e in errors)

    def test_path_traversal_rejected(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        assert not builder._is_safe_rel("../etc/passwd.md")
        assert not builder._is_safe_rel("/etc/passwd.md")
        assert not builder._is_safe_rel("1-智能体身份安全/../etc")
        assert not builder._is_safe_rel("%2e%2e/etc.md")
        assert builder._is_safe_rel("1-智能体身份安全/overview.md")

    def test_symlink_rejected(self, tmp_path):
        _build_knowledge_base(tmp_path)
        real = tmp_path / "outside.md"
        real.write_text("# Outside", encoding="utf-8")
        link = tmp_path / "shared" / "evil-link.md"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            pytest.skip("Symbolic links not supported on this platform")

        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        assert not any(d.relative_path == "shared/evil-link.md" for d in manifest.docs)

    def test_write_is_atomic_leaves_no_temp(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        builder.write(manifest)
        index_file = tmp_path / "_index" / DEFAULT_INDEX_FILENAME
        assert index_file.exists()
        # 不留临时文件
        temps = [p for p in (tmp_path / "_index").iterdir() if p.name.endswith(".tmp")]
        assert temps == []

    def test_build_failure_keeps_old_index(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        first = builder.build_manifest()
        builder.write(first)
        index_file = tmp_path / "_index" / DEFAULT_INDEX_FILENAME
        old_blob = index_file.read_text(encoding="utf-8")

        # 删除必需 brief 使校验失败
        os.remove(tmp_path / "1-智能体身份安全" / "overview.md")
        broken = builder.build_manifest()
        errors = builder.validate(broken)
        assert any("MISSING_REQUIRED_BRIEF" in e for e in errors)

        # 校验失败时不应覆盖旧索引
        assert index_file.read_text(encoding="utf-8") == old_blob
        assert json.loads(old_blob)["index_version"] == first.index_version

    def test_llm_summarizer_used_for_raw_long_docs(self, tmp_path):
        _build_knowledge_base(tmp_path)

        def fake_llm(content, title, section_titles):
            return {
                "description": "LLM描述",
                "summary": "LLM摘要",
                "sections": {t: f"章节摘要:{t}" for t in section_titles if t},
            }

        builder = KnowledgeIndexBuilder(tmp_path, llm_summarizer=fake_llm)
        manifest = builder.build_manifest()
        raw = next(d for d in manifest.docs if d.doc_type == "raw")
        assert raw.description == "LLM描述"
        assert raw.summary == "LLM摘要"
        assert any(s.summary.startswith("章节摘要") for s in raw.sections)


# ═══════════════════════════════════════════════════════════════
# KnowledgeIndexer - 运行时加载与兼容读取（S4-1）
# ═══════════════════════════════════════════════════════════════


class TestKnowledgeIndexer:
    def test_load_roundtrip(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        builder.write(manifest)
        index_file = tmp_path / "_index" / DEFAULT_INDEX_FILENAME

        indexer = KnowledgeIndexer(index_file)
        loaded = indexer.load()
        assert loaded is not None
        assert loaded.index_version == manifest.index_version
        assert len(indexer.for_product("agent-identity-security")) >= 1
        doc = next(
            d for d in indexer.for_product("agent-identity-security")
            if d.doc_type == "overview"
        )
        assert indexer.get(doc.doc_id) is not None

    def test_load_missing_returns_none(self, tmp_path):
        indexer = KnowledgeIndexer(tmp_path / "nope.json")
        assert indexer.load() is None
        assert indexer.index_version == ""

    def test_compatible_schema_read(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        builder.write(manifest)
        index_file = tmp_path / "_index" / DEFAULT_INDEX_FILENAME

        # 模拟更高 schema_version 的索引（仍可按结构读取）
        data = json.loads(index_file.read_text(encoding="utf-8"))
        data["schema_version"] = 2
        index_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        indexer = KnowledgeIndexer(index_file)
        loaded = indexer.load()
        assert loaded is not None
        assert loaded.doc_count == manifest.doc_count


# ═══════════════════════════════════════════════════════════════
# KnowledgeIndexManifest - 确定性序列化（S4-1）
# ═══════════════════════════════════════════════════════════════


class TestManifestSerialization:
    def test_serialization_roundtrip(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        manifest = builder.build_manifest()
        blob = manifest.model_dump_json(ensure_ascii=False)
        reparsed = KnowledgeIndexManifest.model_validate_json(blob)
        assert reparsed.index_version == manifest.index_version
        assert reparsed.doc_count == manifest.doc_count

    def test_deterministic_ordering(self, tmp_path):
        _build_knowledge_base(tmp_path)
        builder = KnowledgeIndexBuilder(tmp_path)
        m1 = builder.build_manifest()
        m2 = builder.build_manifest()

        def norm(m: KnowledgeIndexManifest) -> dict:
            d = m.model_dump()
            d["built_at"] = ""
            for doc in d["docs"]:
                doc["updated_at"] = ""
            return d

        # 排除时间戳后，内容字段完全一致（确定性序列化）
        assert norm(m1) == norm(m2)


# ═══════════════════════════════════════════════════════════════
# 发布流程对接（S4-5）
# ═══════════════════════════════════════════════════════════════


class TestPublicationIndexIntegration:
    def _make_service_and_db(self, tmp_path):
        locks = MagicMock()
        locks.insert_one = AsyncMock(return_value=MagicMock())
        locks.delete_one = AsyncMock()
        drafts = MagicMock()
        drafts.update_one = AsyncMock()
        publications = MagicMock()
        publications.insert_one = AsyncMock()
        publications.update_one = AsyncMock()
        revisions = MagicMock()
        revisions.insert_one = AsyncMock()
        audit_logs = MagicMock()
        audit_logs.insert_one = AsyncMock()
        db = _make_mock_db(
            {
                "knowledge_publish_locks": locks,
                "knowledge_drafts": drafts,
                "knowledge_publications": publications,
                "knowledge_revisions": revisions,
                "knowledge_audit_logs": audit_logs,
            }
        )
        return (
            KnowledgePublicationService(db, str(tmp_path)),
            {"locks": locks, "drafts": drafts, "publications": publications,
             "revisions": revisions, "audit_logs": audit_logs},
        )

    @pytest.mark.asyncio
    async def test_publish_writes_index_and_returns_version(self, tmp_path):
        _build_knowledge_base(tmp_path)
        # 草稿修改 overview.md
        original = (tmp_path / "1-智能体身份安全" / "overview.md").read_text(encoding="utf-8")
        new_content = "# 产品新概述\n\n核心卖点更新内容"

        drafts = MagicMock()
        drafts.find_one = AsyncMock(
            return_value=_make_draft_doc(
                relative_path="1-智能体身份安全/overview.md",
                content_md=new_content,
                base_content_hash=_compute_hash(original),
            )
        )
        drafts.update_one = AsyncMock()

        service, mocks = self._make_service_and_db(tmp_path)
        mocks["drafts"].find_one = drafts.find_one
        mocks["drafts"].update_one = drafts.update_one

        with patch(
            "knowledge_admin.publication.KnowledgeLoader",
            return_value=_make_mock_loader("fake-hash"),
        ):
            result = await service.publish(
                draft_ids=["kbd-s4-001"],
                version_name="v1.0",
                release_notes="",
                user_id="u-admin",
            )

        assert result["status"] == "published"
        assert result["index_version"] != ""

        index_file = tmp_path / "_index" / DEFAULT_INDEX_FILENAME
        assert index_file.exists()
        indexer = KnowledgeIndexer(index_file).load()
        assert indexer is not None
        assert indexer.index_version == result["index_version"]
        # 索引反映发布后的内容
        overview = next(
            d for d in indexer.docs if d.relative_path == "1-智能体身份安全/overview.md"
        )
        assert overview.content_hash == _compute_hash(new_content)

    @pytest.mark.asyncio
    async def test_publish_index_build_failure_aborts_before_writing_files(self, tmp_path):
        _build_knowledge_base(tmp_path)
        original = (tmp_path / "1-智能体身份安全" / "overview.md").read_text(encoding="utf-8")

        drafts = MagicMock()
        drafts.find_one = AsyncMock(
            return_value=_make_draft_doc(
                relative_path="1-智能体身份安全/overview.md",
                content_md="# 产品新概述\n\n新内容",
                base_content_hash=_compute_hash(original),
            )
        )

        service, mocks = self._make_service_and_db(tmp_path)
        mocks["drafts"].find_one = drafts.find_one

        with patch(
            "knowledge_admin.publication.KnowledgeLoader",
            return_value=_make_mock_loader("fake-hash"),
        ), patch(
            "knowledge_admin.publication.KnowledgeIndexBuilder.build_manifest",
            side_effect=RuntimeError("磁盘异常"),
        ), pytest.raises(RuntimeError, match="磁盘异常"):
            await service.publish(
                draft_ids=["kbd-s4-001"],
                version_name="v1.0",
                release_notes="",
                user_id="u-admin",
            )

        # 未覆盖线上知识文件
        current = (tmp_path / "1-智能体身份安全" / "overview.md").read_text(encoding="utf-8")
        assert current == original
        # 发布记录被标记失败（未进入 published），不写任何知识文件
        update_doc = mocks["publications"].update_one.call_args.args[1]
        assert update_doc["$set"]["status"] == KnowledgePublicationStatus.FAILED
