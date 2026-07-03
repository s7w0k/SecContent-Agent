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
