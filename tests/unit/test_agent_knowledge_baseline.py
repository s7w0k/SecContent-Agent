"""
产品知识库基线锁定测试 (K.0)

锁定 KnowledgeLoader 当前行为，作为回归基线。
使用真实的 agent-security-briefs/ 知识库目录。

运行:
    pytest tests/unit/test_agent_knowledge_baseline.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

# 知识库根目录（相对于测试文件向上三级）
KB_DIR = Path(__file__).parent.parent.parent / "agent-security-briefs"

# 5 个核心打分文件
CORE_SCORING_FILES = [
    "1-智能体身份安全/overview.md",
    "1-智能体身份安全/market-brief.md",
    "3-AI-BOM/overview.md",
    "3-AI-BOM/market-brief.md",
    "shared/hot-event-playbook.md",
]

# 被排除的文件名
EXCLUDED_FILENAMES = {"CLAUDE.md", "AGENTS.md", "README.md", "qa-log.md", "tasks.md"}

# 截断阈值（与 KnowledgeLoader.as_scoring_prompt 保持一致）
TRUNCATION_LIMIT = 2500


class TestKnowledgeBaseline:
    """基线锁定测试：锁定 KnowledgeLoader 当前行为。"""

    # ── a) 文件发现 ──────────────────────────────────────────

    def test_discover_finds_core_files(self):
        """_discover_files() 返回 5 个核心打分文件。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        rel_paths = [str(f.relative_to(KB_DIR)).replace("\\", "/") for f in files]

        for core in CORE_SCORING_FILES:
            assert core in rel_paths, f"核心文件缺失: {core}"

    def test_discover_all_markdown(self):
        """所有发现的文件都是 .md。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        assert all(f.suffix == ".md" for f in files)

    def test_discover_sorted_by_path(self):
        """文件按路径排序。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        assert files == sorted(files)

    def test_discover_excludes_blocked_files(self):
        """排除 CLAUDE.md / AGENTS.md / README.md / qa-log.md / tasks.md。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        for f in files:
            assert f.name not in EXCLUDED_FILENAMES, f"应被排除: {f.name}"

    def test_discover_excludes_blocked_dirs(self):
        """排除 原始文档/ 和 海外版/ 目录。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        for f in files:
            rel = str(f.relative_to(KB_DIR)).replace("\\", "/")
            assert "原始文档" not in rel, f"应排除原始文档: {rel}"
            assert "海外版" not in rel, f"应排除海外版: {rel}"

    def test_discover_excludes_non_shared_architecture_brief(self):
        """architecture-brief.md 仅 shared/ 下保留，其余排除。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        for f in files:
            if f.name == "architecture-brief.md":
                rel = str(f.relative_to(KB_DIR)).replace("\\", "/")
                assert rel.startswith("shared/"), (
                    f"非 shared 下的 architecture-brief 应排除: {rel}"
                )

    # ── b) 打分 Prompt 稳定性 ────────────────────────────────

    @pytest.mark.asyncio
    async def test_scoring_prompt_non_empty(self):
        """as_scoring_prompt() 返回非空。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        await loader.load()
        prompt = loader.as_scoring_prompt()
        assert len(prompt) > 0

    @pytest.mark.asyncio
    async def test_scoring_prompt_sha256_stable(self):
        """as_scoring_prompt() 两次调用 SHA-256 一致。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        await loader.load()
        prompt1 = loader.as_scoring_prompt()
        prompt2 = loader.as_scoring_prompt()
        h1 = hashlib.sha256(prompt1.encode("utf-8")).hexdigest()
        h2 = hashlib.sha256(prompt2.encode("utf-8")).hexdigest()
        assert h1 == h2
        assert len(h1) == 64

    @pytest.mark.asyncio
    async def test_scoring_prompt_built_from_files(self):
        """prompt 由文件拼接而非 system prompt 兜底。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        await loader.load()
        prompt = loader.as_scoring_prompt()

        # 文件拼接使用 "---" 分隔，兜底 system prompt 以 "## 产品" 开头
        assert "---" in prompt, "prompt 应包含文件分隔符 ---"

        # 核心文件的首行应出现在 prompt 中（截断只影响 2500 字符之后的内容）
        for rel_path in CORE_SCORING_FILES:
            fp = KB_DIR / rel_path
            first_line = fp.read_text(encoding="utf-8").split("\n", 1)[0]
            assert first_line in prompt, f"文件首行未出现: {rel_path} -> {first_line}"

    @pytest.mark.asyncio
    async def test_scoring_prompt_truncation(self):
        """超过 2500 字符的文件被截断。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        await loader.load()
        prompt = loader.as_scoring_prompt()

        # 核心文件中存在超过 2500 字符的文件，应出现截断标记
        has_long_file = any(
            len((KB_DIR / rel).read_text(encoding="utf-8")) > TRUNCATION_LIMIT
            for rel in CORE_SCORING_FILES
            if (KB_DIR / rel).exists()
        )
        if has_long_file:
            assert "(truncated)" in prompt, "应包含截断标记"

        # 验证截断后的内容不包含原文超过 2500 字符之后的部分
        for rel_path in CORE_SCORING_FILES:
            fp = KB_DIR / rel_path
            if not fp.exists():
                continue
            content = fp.read_text(encoding="utf-8")
            if len(content) > TRUNCATION_LIMIT:
                beyond = content[TRUNCATION_LIMIT:TRUNCATION_LIMIT + 20]
                if beyond.strip():
                    assert beyond not in prompt, f"文件 {rel_path} 未正确截断"

    # ── c) 关键词 ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_keywords_non_empty(self):
        """as_keywords() 返回非空列表。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        knowledge = await loader.load()
        keywords = knowledge.as_keywords()
        assert len(keywords) > 0

    @pytest.mark.asyncio
    async def test_keywords_contains_expected_terms(self):
        """关键词包含安全领域核心术语。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        knowledge = await loader.load()
        keywords = knowledge.as_keywords()
        # 至少包含 MCP 相关术语
        assert any("MCP" in k for k in keywords), "应包含 MCP 相关术语"
        # 至少包含智能体相关术语
        assert any("智能体" in k or "Agent" in k for k in keywords), "应包含智能体相关术语"

    # ── d) 内容哈希 ──────────────────────────────────────────

    def test_content_hash_stable(self):
        """_compute_hash() 两次调用结果一致。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        h1 = loader._compute_hash(files)
        h2 = loader._compute_hash(files)
        assert h1 == h2

    def test_content_hash_is_md5(self):
        """_compute_hash() 返回 32 位 MD5 十六进制。"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=str(KB_DIR))
        files = loader._discover_files()
        h = loader._compute_hash(files)
        assert len(h) == 32
        assert all(c in "0123456789abcdef" for c in h)

    def test_content_hash_changes_with_content(self):
        """内容变化时哈希变化。"""
        from agent.knowledge import KnowledgeLoader

        with tempfile.TemporaryDirectory() as tmpdir:
            fp = Path(tmpdir) / "test.md"
            fp.write_text("# 内容A", encoding="utf-8")
            loader = KnowledgeLoader(docs_dir=tmpdir)
            files = loader._discover_files()
            h1 = loader._compute_hash(files)

            fp.write_text("# 内容B", encoding="utf-8")
            h2 = loader._compute_hash(files)
            assert h1 != h2
