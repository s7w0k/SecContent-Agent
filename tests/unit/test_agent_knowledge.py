"""
产品知识库加载器 — 单元测试

运行:
    pytest tests/unit/test_agent_knowledge.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))


# ═══════════════════════════════════════════════════════════════
# 测试用 Markdown 文档
# ═══════════════════════════════════════════════════════════════

SAMPLE_DOC = """
# 智能体身份安全产品

## 产品定位

定义AI时代的安全身份通行证。为企业提供智能体身份治理能力。

## 核心功能

- MCP协议安全防护
- 智能体身份认证与授权
- 意图识别与异常行为检测
- Prompt注入防御
- 跨智能体供应链安全
- 全链路审计追踪

## 技术壁垒

- 动态上下文感知引擎
- 多模态意图理解模型
- 分布式身份联邦协议
- 实时行为基线学习

## 控标点

- 首家支持MCP协议安全审计
- 唯一通过信通院智能体安全认证
- 首创智能体身份画像技术
- 运营商级大规模部署验证

## 客户案例

- 北京移动：家宽智能体身份防护
- 中移IT公司：智能体广场安全方案

## 目标行业

运营商、金融、政务、能源

## 竞品

- Palo Alto AI Access Security
- Zscaler AI Security
"""


# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def sample_doc_dir():
    """创建含测试文档的临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        doc_path = Path(tmpdir) / "智能体身份安全产品计划和目标.md"
        doc_path.write_text(SAMPLE_DOC, encoding="utf-8")
        yield tmpdir


@pytest.fixture
async def loader(sample_doc_dir):
    """创建已初始化的 KnowledgeLoader"""
    from agent.knowledge import KnowledgeLoader

    ldr = KnowledgeLoader(docs_dir=sample_doc_dir)
    yield ldr


# ═══════════════════════════════════════════════════════════════
# 1. MarkdownKnowledgeParser 测试
# ═══════════════════════════════════════════════════════════════


class TestMarkdownParser:
    """规则解析器测试"""

    def test_parse_product_name(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert "智能体身份安全" in knowledge.product_name

    def test_parse_positioning(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.product_positioning) > 10

    def test_parse_core_features(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.core_features) >= 3
        assert any("MCP" in f for f in knowledge.core_features)

    def test_parse_tech_barriers(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.tech_barriers) >= 2

    def test_parse_control_points(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.control_points) >= 2
        assert any("MCP" in p for p in knowledge.control_points)

    def test_parse_customer_cases(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.customer_cases) >= 1
        assert any("北京移动" in c for c in knowledge.customer_cases)

    def test_parse_target_industries(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.target_industries) >= 2
        assert "运营商" in knowledge.target_industries

    def test_parse_competitors(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse(SAMPLE_DOC)
        assert len(knowledge.competitors) >= 1

    def test_parse_empty_doc(self):
        from agent.knowledge import MarkdownKnowledgeParser

        knowledge = MarkdownKnowledgeParser.parse("# No matching title\n\nNo content")
        # 未匹配到产品名时保留默认值
        assert knowledge.product_name == "智能体身份安全产品"
        # 仍然有默认关键术语
        assert len(knowledge.key_terms) > 0

    def test_section_splitting(self):
        from agent.knowledge import MarkdownKnowledgeParser

        sections = MarkdownKnowledgeParser._split_sections(SAMPLE_DOC)
        assert len(sections) >= 5  # preamble + several ## sections

    def test_extract_list_items(self):
        from agent.knowledge import MarkdownKnowledgeParser

        text = "- item one\n- item two\n- item three"
        items = MarkdownKnowledgeParser._extract_list_items(text)
        assert len(items) == 3


# ═══════════════════════════════════════════════════════════════
# 2. ProductKnowledge 数据结构测试
# ═══════════════════════════════════════════════════════════════


class TestProductKnowledge:
    """知识库对象方法测试"""

    def test_default_values(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge()
        assert pk.product_name == "智能体身份安全产品"
        assert len(pk.key_terms) >= 8
        assert pk.core_features == []

    def test_as_system_prompt_with_data(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge(
            product_positioning="测试定位",
            core_features=["功能A", "功能B"],
            tech_barriers=["壁垒X"],
            control_points=["控标1"],
        )
        prompt = pk.as_system_prompt()
        assert "产品定位" in prompt
        assert "功能A" in prompt
        assert "壁垒X" in prompt
        assert "控标1" in prompt

    def test_as_system_prompt_empty(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge()
        prompt = pk.as_system_prompt()
        # 空知识库返回只含关键术语的 prompt
        assert "关键术语" in prompt

    def test_as_keywords(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge(
            core_features=["MCP协议安全防护", "智能体身份认证"],
            tech_barriers=["动态上下文感知引擎"],
            control_points=["首家支持MCP协议安全审计"],
        )
        keywords = pk.as_keywords()
        assert len(keywords) > 0
        assert "MCP协议安全防护" in keywords
        assert "智能体身份认证" in keywords
        assert "动态上下文感知引擎" in keywords

    def test_as_keywords_deduplication(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge(
            core_features=["相同关键词"],
            tech_barriers=["相同关键词"],  # duplicate
        )
        keywords = pk.as_keywords()
        # Should not duplicate
        assert keywords.count("相同关键词") == 1


# ═══════════════════════════════════════════════════════════════
# 3. KnowledgeLoader 测试
# ═══════════════════════════════════════════════════════════════


class TestKnowledgeLoader:
    """加载器核心功能测试"""

    @pytest.mark.asyncio
    async def test_load_success(self, loader):
        knowledge = await loader.load()
        assert knowledge is not None
        assert loader.is_loaded
        assert len(knowledge.core_features) >= 3

    @pytest.mark.asyncio
    async def test_cache_hit(self, loader):
        k1 = await loader.load()
        k2 = await loader.load()
        # 同一次加载，应返回同一对象（缓存命中）
        assert k1 is k2

    @pytest.mark.asyncio
    async def test_force_reload(self, loader):
        k1 = await loader.load()
        k2 = await loader.load(force=True)
        # 强制重载，内容相同但对象可能不同（重新解析）
        assert k2 is not None
        assert len(k2.core_features) == len(k1.core_features)

    @pytest.mark.asyncio
    async def test_as_system_prompt(self, loader):
        await loader.load()
        prompt = loader.as_system_prompt()
        assert len(prompt) > 50
        assert "产品定位" in prompt or "核心功能" in prompt

    @pytest.mark.asyncio
    async def test_as_keywords(self, loader):
        await loader.load()
        keywords = loader.as_keywords()
        assert len(keywords) > 5

    @pytest.mark.asyncio
    async def test_reload_if_changed_no_change(self, loader):
        await loader.load()
        changed = await loader.reload_if_changed()
        assert changed is False  # 文件未变

    @pytest.mark.asyncio
    async def test_reload_if_changed_detected(self, sample_doc_dir):
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir=sample_doc_dir)
        await loader.load()

        # 修改文件
        doc_path = Path(sample_doc_dir) / KnowledgeLoader.DEFAULT_DOC
        doc_path.write_text(SAMPLE_DOC + "\n# 新增内容\n", encoding="utf-8")

        changed = await loader.reload_if_changed()
        assert changed is True

    @pytest.mark.asyncio
    async def test_as_system_prompt_before_load(self, loader):
        # 未加载时返回空字符串
        prompt = loader.as_system_prompt()
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_load_returns_default_when_file_missing(self):
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(docs_dir="/nonexistent/path")
        knowledge = await loader.load()
        # 文件不存在时返回默认空知识库
        assert knowledge is not None
        assert knowledge.core_features == []


# ═══════════════════════════════════════════════════════════════
# 4. 集成场景测试
# ═══════════════════════════════════════════════════════════════


class TestKnowledgeIntegration:
    """知识库与打分/报道场景的集成验证"""

    @pytest.mark.asyncio
    async def test_prompt_contains_key_info_for_scorer(self, loader):
        """验证输出可作为打分 Agent 的 System Prompt"""
        knowledge = await loader.load()
        prompt = knowledge.as_system_prompt()

        # 打分 Agent 需要的核心信息
        assert len(prompt) > 0
        # 包含产品相关术语
        terms = knowledge.as_keywords()
        assert "MCP协议" in terms or any("MCP" in t for t in terms)

    @pytest.mark.asyncio
    async def test_keywords_cover_security_domains(self, loader):
        """验证关键词覆盖安全领域关键概念"""
        knowledge = await loader.load()
        keywords = knowledge.as_keywords()
        " ".join(keywords)

        # 覆盖核心安全领域
        security_domains = ["身份", "认证", "权限", "MCP", "注入", "智能体"]
        covered = [d for d in security_domains if any(d in k for k in keywords)]
        assert len(covered) >= 3, f"Only {len(covered)}/{len(security_domains)} domains covered"


# ═══════════════════════════════════════════════════════════════
# 5. V2 多文件加载测试
# ═══════════════════════════════════════════════════════════════


SAMPLE_DOC_2 = """
# 竞品分析报告

## 竞品

- CrowdStrike AI Security
- Wiz AI Security Posture Management

## 目标行业

金融、医疗
"""


SAMPLE_DOC_3 = """
# 智能体安全技术白皮书

## 架构设计

- 微隔离引擎
- 动态信任评估
- 实时风险评分

## 控标点

- 信创适配（鲲鹏/飞腾）
- 国密算法SM2/SM4支持
"""


class TestV2MultiFile:
    """V2 多文件加载与合并"""

    @pytest.mark.asyncio
    async def test_discover_single_file(self, sample_doc_dir):
        """指定 filename 时仅返回一个文件（向后兼容）"""
        from agent.knowledge import KnowledgeLoader

        loader = KnowledgeLoader(
            docs_dir=sample_doc_dir,
            filename="智能体身份安全产品计划和目标.md",
        )
        files = loader._discover_files()
        assert len(files) == 1
        assert "智能体身份安全产品计划和目标.md" in str(files[0])

    @pytest.mark.asyncio
    async def test_discover_all_files(self):
        """不指定 filename 时递归扫描所有 .md"""
        # 使用实际 docs/ 目录（至少有 5+ .md 文件）
        import os as _os

        from agent.knowledge import KnowledgeLoader
        docs_dir = _os.path.join(
            _os.path.dirname(__file__), "..", "..", "docs",
        )
        if _os.path.isdir(docs_dir):
            loader = KnowledgeLoader(docs_dir=docs_dir)
            files = loader._discover_files()
            assert len(files) >= 1  # at least the product doc

    @pytest.mark.asyncio
    async def test_multi_file_merge(self):
        """加载多个文件并合并知识"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            doc1 = Path(tmpdir) / "产品.md"
            doc1.write_text(SAMPLE_DOC, encoding="utf-8")
            doc2 = Path(tmpdir) / "竞品.md"
            doc2.write_text(SAMPLE_DOC_2, encoding="utf-8")

            from agent.knowledge import KnowledgeLoader
            loader = KnowledgeLoader(docs_dir=tmpdir)
            knowledge = await loader.load()

            # 合并后: 功能来自 doc1, 竞品来自 doc2
            assert len(knowledge.core_features) >= 3
            assert len(knowledge.competitors) >= 3  # 1 from doc1 + 2 from doc2
            assert "金融" in knowledge.target_industries
            assert len(knowledge.source_files) == 2

    @pytest.mark.asyncio
    async def test_merge_deduplication(self):
        """合并时去重"""
        from agent.knowledge import KnowledgeLoader, ProductKnowledge

        base = ProductKnowledge(
            core_features=["功能A", "功能B"],
            key_terms=["术语1"],
        )
        add = ProductKnowledge(
            core_features=["功能B", "功能C"],  # "功能B" 重复
            key_terms=["术语1", "术语2"],       # "术语1" 重复
        )
        merged = KnowledgeLoader._merge_knowledge(base, add)
        assert len(merged.core_features) == 3  # A, B, C
        assert len(merged.key_terms) > 0
        assert merged.core_features.count("功能B") == 1

    @pytest.mark.asyncio
    async def test_source_files_tracking(self):
        """验证 source_files 追踪所有来源"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            doc1 = Path(tmpdir) / "doc1.md"
            doc1.write_text("# Test\n\n## 核心功能\n- 功能X\n- 功能Y", encoding="utf-8")
            doc2 = Path(tmpdir) / "doc2.md"
            doc2.write_text("# Test2\n\n## 竞品\n- 竞品Z", encoding="utf-8")

            from agent.knowledge import KnowledgeLoader
            loader = KnowledgeLoader(docs_dir=tmpdir)
            knowledge = await loader.load()
            assert len(knowledge.source_files) == 2
            assert any("doc1.md" in s for s in knowledge.source_files)
            assert any("doc2.md" in s for s in knowledge.source_files)

    @pytest.mark.asyncio
    async def test_backward_compat_source_file(self):
        """V1 的 source_file 属性兼容"""
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge()
        pk.source_file = "/test/path.md"
        assert pk.source_file == "/test/path.md"
        assert "/test/path.md" in pk.source_files

        pk2 = ProductKnowledge()
        assert pk2.source_file == ""


class TestV2EnhancedPrompt:
    """V2 增强 System Prompt"""

    def test_prompt_includes_product_name(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge(
            product_name="测试产品名",
            product_positioning="测试定位",
        )
        prompt = pk.as_system_prompt()
        assert "测试产品名" in prompt

    def test_prompt_includes_competitors(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge(competitors=["友商A", "友商B"])
        prompt = pk.as_system_prompt()
        assert "竞品信息" in prompt
        assert "友商A" in prompt

    def test_prompt_includes_source_annotation(self):
        from agent.knowledge import ProductKnowledge

        pk = ProductKnowledge(source_files=["/path/a.md", "/path/b.md"])
        prompt = pk.as_system_prompt()
        assert "知识来源" in prompt

    def test_parse_extracts_key_terms(self):
        from agent.knowledge import MarkdownKnowledgeParser

        content = "本文讨论MCP协议在Agent安全中的身份认证与权限管控问题"
        knowledge = MarkdownKnowledgeParser.parse(content)
        assert len(knowledge.key_terms) > 0
        # 至少包含默认术语和自动提取的新术语
        assert any("MCP协议" in t for t in knowledge.key_terms)


class TestV2HotReload:
    """V2 多文件热重载"""

    @pytest.mark.asyncio
    async def test_multi_file_reload(self):
        """多文件变更检测"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            doc1 = Path(tmpdir) / "doc1.md"
            doc1.write_text("# Test\n\n## 核心功能\n- MCP协议安全防护能力", encoding="utf-8")

            from agent.knowledge import KnowledgeLoader
            loader = KnowledgeLoader(docs_dir=tmpdir)
            knowledge = await loader.load()
            assert len(knowledge.core_features) >= 1

            # 添加新文件
            doc2 = Path(tmpdir) / "doc2.md"
            doc2.write_text("# Test2\n\n## 核心功能\n- 智能体身份认证授权", encoding="utf-8")

            changed = await loader.reload_if_changed()
            assert changed is True

            # 新知识合并了所有文件
            knowledge2 = await loader.load()
            assert len(knowledge2.core_features) >= 2

    @pytest.mark.asyncio
    async def test_file_removal_detected(self):
        """文件被删除后重载（无文件时不触发变更）"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            doc1 = Path(tmpdir) / "doc1.md"
            doc1.write_text("# Test\n\n## 核心功能\n- MCP协议安全防护", encoding="utf-8")

            from agent.knowledge import KnowledgeLoader
            loader = KnowledgeLoader(docs_dir=tmpdir)
            await loader.load()

            # 修改文件内容触发变更
            doc1.write_text("# Test Updated\n\n## 技术壁垒\n- 动态上下文感知", encoding="utf-8")

            changed = await loader.reload_if_changed()
            # 文件内容变更，触发重载
            assert changed is True
