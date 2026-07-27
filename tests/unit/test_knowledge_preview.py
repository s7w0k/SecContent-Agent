"""临时知识库校验、Prompt预览与试打分服务单元测试。"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from knowledge_admin.preview import KnowledgePreviewService

# 真实知识库目录
KB_ROOT = Path(__file__).parent.parent.parent / "agent-security-briefs"

# 直接打分文件路径（受保护路径）
DIRECT_SCORING_FILE = "1-智能体身份安全/overview.md"
# 非直接打分文件路径（评分相关但不在 5 个核心文件中）
NON_DIRECT_SCORING_FILE = "0-产品全景/overview.md"


# ═══════════════════════════════════════════════════════════════
# create_temp_snapshot / cleanup_temp
# ═══════════════════════════════════════════════════════════════


class TestCreateTempSnapshot:
    """临时快照创建与清理。"""

    def test_creates_copy_and_replaces_draft_files(self):
        service = KnowledgePreviewService(KB_ROOT)
        draft_content = "# Replaced Content\n\nThis is test draft content."
        tmp_dir = service.create_temp_snapshot(
            [{"relative_path": DIRECT_SCORING_FILE, "content_md": draft_content}]
        )

        try:
            tmp_root = Path(tmp_dir)
            assert tmp_root.exists()

            # 被替换的文件应包含草稿内容
            replaced = tmp_root / DIRECT_SCORING_FILE
            assert replaced.read_text(encoding="utf-8") == draft_content

            # 未替换的文件应与正式文件一致
            other_rel = "1-智能体身份安全/market-brief.md"
            other_tmp = tmp_root / other_rel
            other_formal = KB_ROOT / other_rel
            assert other_tmp.read_text(encoding="utf-8") == other_formal.read_text(encoding="utf-8")
        finally:
            service.cleanup_temp(tmp_dir)

    def test_cleanup_removes_directory(self):
        service = KnowledgePreviewService(KB_ROOT)
        tmp_dir = service.create_temp_snapshot(
            [{"relative_path": DIRECT_SCORING_FILE, "content_md": "# Test"}]
        )

        assert Path(tmp_dir).exists()
        service.cleanup_temp(tmp_dir)
        assert not Path(tmp_dir).exists()


# ═══════════════════════════════════════════════════════════════
# validate_draft
# ═══════════════════════════════════════════════════════════════


class TestValidateDraft:
    """草稿校验。"""

    async def test_passed_for_valid_content(self):
        service = KnowledgePreviewService(KB_ROOT)
        content = "# Test Heading\n\nValid content for scoring."
        result = await service.validate_draft(DIRECT_SCORING_FILE, content)

        assert result["status"] == "passed"
        assert result["errors"] == []
        assert result["loader_file_count"] > 0
        assert result["loader_relevant_count"] > 0

    async def test_failed_for_empty_content(self):
        service = KnowledgePreviewService(KB_ROOT)
        result = await service.validate_draft(DIRECT_SCORING_FILE, "")

        assert result["status"] == "failed"
        assert any("内容不能为空" in e for e in result["errors"])

    async def test_failed_for_nul_characters(self):
        service = KnowledgePreviewService(KB_ROOT)
        result = await service.validate_draft(DIRECT_SCORING_FILE, "# Heading\x00bad")

        assert result["status"] == "failed"
        assert any("NUL" in e for e in result["errors"])

    async def test_warns_for_non_direct_scoring_files(self):
        service = KnowledgePreviewService(KB_ROOT)
        content = "# Test Heading\n\nValid content."
        result = await service.validate_draft(NON_DIRECT_SCORING_FILE, content)

        assert result["status"] == "passed"
        assert any("不直接参与 V2 打分 Prompt" in w for w in result["warnings"])

    async def test_errors_for_protected_path_without_heading(self):
        service = KnowledgePreviewService(KB_ROOT)
        content = "Some text without any markdown heading."
        result = await service.validate_draft(DIRECT_SCORING_FILE, content)

        assert result["status"] == "failed"
        assert any("标题" in e for e in result["errors"])


# ═══════════════════════════════════════════════════════════════
# preview_prompt
# ═══════════════════════════════════════════════════════════════


class TestPreviewPrompt:
    """Prompt 预览对比。"""

    async def test_returns_old_new_prompt_comparison(self):
        service = KnowledgePreviewService(KB_ROOT)
        content = "# Test Heading\n\nTest content for prompt preview."
        result = await service.preview_prompt(DIRECT_SCORING_FILE, content)

        assert "old_prompt" in result
        assert "new_prompt" in result
        assert "old_hash" in result
        assert "new_hash" in result
        assert "prompt_changed" in result
        assert "file_in_prompt" in result
        assert "char_count_old" in result
        assert "char_count_new" in result
        assert isinstance(result["old_prompt"], str)
        assert isinstance(result["new_prompt"], str)
        assert result["file_in_prompt"] is True

    async def test_detects_prompt_change_when_content_differs(self):
        service = KnowledgePreviewService(KB_ROOT)
        modified_content = "# Completely Different Heading\n\nDifferent test content."
        result = await service.preview_prompt(DIRECT_SCORING_FILE, modified_content)

        assert result["prompt_changed"] is True
        assert result["old_hash"] != result["new_hash"]

    async def test_detects_no_change_when_content_same(self):
        service = KnowledgePreviewService(KB_ROOT)
        # 读取正式文件的原始内容作为草稿内容
        formal_content = (KB_ROOT / DIRECT_SCORING_FILE).read_text(encoding="utf-8")
        result = await service.preview_prompt(DIRECT_SCORING_FILE, formal_content)

        assert result["prompt_changed"] is False
        assert result["old_hash"] == result["new_hash"]


# ═══════════════════════════════════════════════════════════════
# 正式文件不被修改
# ═══════════════════════════════════════════════════════════════


def _compute_file_hashes(root: Path) -> dict[str, str]:
    """计算知识库下所有 .md 文件的 SHA-256 哈希。"""
    hashes: dict[str, str] = {}
    for md_file in sorted(root.rglob("*.md")):
        if ".git" in md_file.parts:
            continue
        rel = str(md_file.relative_to(root)).replace("\\", "/")
        content = md_file.read_text(encoding="utf-8")
        hashes[rel] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hashes


class TestFormalFilesNotModified:
    """确保所有操作不修改正式文件。"""

    async def test_formal_files_unchanged_after_operations(self):
        service = KnowledgePreviewService(KB_ROOT)

        before = _compute_file_hashes(KB_ROOT)

        # 运行 create_temp_snapshot + cleanup
        tmp_dir = service.create_temp_snapshot(
            [{"relative_path": DIRECT_SCORING_FILE, "content_md": "# Modified\n\nTemp content."}]
        )
        service.cleanup_temp(tmp_dir)

        # 运行 validate_draft
        await service.validate_draft(DIRECT_SCORING_FILE, "# Test Heading\n\nValid content.")

        # 运行 preview_prompt
        await service.preview_prompt(DIRECT_SCORING_FILE, "# Different\n\nChanged content.")

        after = _compute_file_hashes(KB_ROOT)

        assert before == after
