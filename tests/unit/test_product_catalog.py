"""ProductCatalogService 和 KnowledgeSliceResolver 单元测试 - T3 验收。"""

from __future__ import annotations

import pytest
from agent.knowledge_slice import KnowledgeSliceResolver
from agent.product_catalog import ProductCatalogService
from agent.product_matcher import ProductMatcher

# ── ProductCatalogService 测试 ──────────────────────────


class TestProductCatalogService:
    """产品目录服务测试。"""

    def setup_method(self):
        self.catalog = ProductCatalogService("/tmp/nonexistent")

    def test_list_published_products(self):
        """只返回已发布产品。"""
        products = self.catalog.list_products(published_only=True)
        ids = {p.product_id for p in products}
        assert "agent-identity-security" in ids
        assert "agent-security" in ids
        assert "ai-bom" in ids
        # 未发布的不出现
        assert "agent-security-gateway" not in ids
        assert "ans" not in ids

    def test_list_all_products(self):
        """包含未发布产品。"""
        products = self.catalog.list_products(published_only=False)
        assert len(products) == 5

    def test_list_by_purpose(self):
        """按用途筛选。"""
        products = self.catalog.list_products(purpose="score")
        for p in products:
            assert "score" in p.allowed_purposes

    def test_get_product(self):
        """获取单个产品。"""
        product = self.catalog.get_product("ai-bom")
        assert product is not None
        assert product.name == "AI-BOM"

    def test_get_unknown_product(self):
        """获取不存在的产品返回 None。"""
        assert self.catalog.get_product("nonexistent") is None

    def test_validate_product_id_success(self):
        """校验合法产品 ID。"""
        product = self.catalog.validate_product_id("agent-identity-security")
        assert product.product_id == "agent-identity-security"

    def test_validate_product_id_unknown(self):
        """校验不存在的产品。"""
        with pytest.raises(ValueError, match="PRODUCT_UNAVAILABLE"):
            self.catalog.validate_product_id("nonexistent")

    def test_validate_product_id_unpublished(self):
        """校验未发布产品。"""
        with pytest.raises(ValueError, match="PRODUCT_UNAVAILABLE"):
            self.catalog.validate_product_id("agent-security-gateway")

    def test_validate_product_ids_max_count(self):
        """超过最大数量。"""
        with pytest.raises(ValueError, match="INVALID_PRODUCT_SELECTION"):
            self.catalog.validate_product_ids(
                ["agent-identity-security", "agent-security", "ai-bom"] * 2
            )

    def test_validate_product_ids_empty(self):
        """空列表。"""
        with pytest.raises(ValueError, match="INVALID_PRODUCT_SELECTION"):
            self.catalog.validate_product_ids([])

    def test_validate_product_ids_success(self):
        """批量校验成功。"""
        products = self.catalog.validate_product_ids(
            ["agent-identity-security", "ai-bom"]
        )
        assert len(products) == 2

    def test_is_path_safe_rejects_dot_dot(self):
        """拒绝路径穿越。"""
        assert not ProductCatalogService.is_path_safe("../etc/passwd")
        assert not ProductCatalogService.is_path_safe("1-智能体身份安全/../etc")

    def test_is_path_safe_rejects_absolute(self):
        """拒绝绝对路径。"""
        assert not ProductCatalogService.is_path_safe("/etc/passwd")

    def test_is_path_safe_rejects_encoded(self):
        """拒绝 URL 编码路径。"""
        assert not ProductCatalogService.is_path_safe("%2e%2e/etc")
        assert not ProductCatalogService.is_path_safe("%2fetc")

    def test_is_path_safe_rejects_excluded(self):
        """拒绝排除路径。"""
        assert not ProductCatalogService.is_path_safe("原始文档/file.md")
        assert not ProductCatalogService.is_path_safe("tasks.md")

    def test_is_path_safe_accepts_valid(self):
        """接受合法路径。"""
        assert ProductCatalogService.is_path_safe("1-智能体身份安全/overview.md")

    def test_catalog_hash_stable(self):
        """目录哈希稳定。"""
        h1 = self.catalog.catalog_hash()
        h2 = self.catalog.catalog_hash()
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_to_api_response(self):
        """API 响应格式。"""
        response = self.catalog.to_api_response()
        assert "items" in response
        assert "knowledge_hash" in response
        assert len(response["items"]) == 3  # 3 个已发布

    def test_products_sorted_by_sort_order(self):
        """产品按 sort_order 排序。"""
        products = self.catalog.list_products()
        orders = [p.sort_order for p in products]
        assert orders == sorted(orders)


# ── KnowledgeSliceResolver 测试 ─────────────────────────


class TestKnowledgeSliceResolver:
    """知识切片解析器测试。"""

    def test_resolve_none(self):
        """none 模式返回空切片。"""
        resolver = KnowledgeSliceResolver("/tmp/nonexistent")
        result = resolver.resolve_none()
        assert result.content == ""
        assert result.product_ids == []
        assert result.content_hash == "sha256:none"

    @pytest.mark.asyncio
    async def test_resolve_empty_product_ids(self):
        """空产品列表返回空内容。"""
        resolver = KnowledgeSliceResolver("/tmp/nonexistent")
        result = await resolver.resolve(
            purpose="score",
            product_ids=[],
        )
        # 没有产品时仍可能包含共享文件，但文件不存在所以应为空
        assert result.char_count == 0 or result.content == ""

    @pytest.mark.asyncio
    async def test_resolve_invalid_product(self):
        """无效产品 ID 抛出异常。"""
        resolver = KnowledgeSliceResolver("/tmp/nonexistent")
        with pytest.raises(ValueError, match="PRODUCT_UNAVAILABLE"):
            await resolver.resolve(
                purpose="score",
                product_ids=["nonexistent-product"],
            )

    @pytest.mark.asyncio
    async def test_resolve_with_mocked_files(self, tmp_path):
        """使用模拟文件测试切片。"""
        # 创建模拟知识库
        product_dir = tmp_path / "1-智能体身份安全"
        product_dir.mkdir(parents=True)
        (product_dir / "overview.md").write_text("# 智能体身份安全\n产品概述", encoding="utf-8")
        (product_dir / "market-brief.md").write_text("市场简报内容", encoding="utf-8")

        shared_dir = tmp_path / "shared"
        shared_dir.mkdir(parents=True)
        (shared_dir / "hot-event-playbook.md").write_text("热点事件策略", encoding="utf-8")

        pan_dir = tmp_path / "0-产品全景"
        pan_dir.mkdir(parents=True)
        (pan_dir / "overview.md").write_text("产品全景概述", encoding="utf-8")

        resolver = KnowledgeSliceResolver(tmp_path)
        result = await resolver.resolve(
            purpose="score",
            product_ids=["agent-identity-security"],
            include_shared=True,
        )

        assert "智能体身份安全" in result.content
        assert "产品概述" in result.content
        assert result.product_ids == ["agent-identity-security"]
        assert len(result.source_document_ids) > 0
        assert result.content_hash.startswith("sha256:")
        assert result.char_count > 0

    @pytest.mark.asyncio
    async def test_resolve_truncation(self, tmp_path):
        """字符预算超限时截断。"""
        product_dir = tmp_path / "1-智能体身份安全"
        product_dir.mkdir(parents=True)
        (product_dir / "overview.md").write_text("x" * 5000, encoding="utf-8")
        (product_dir / "market-brief.md").write_text("y" * 5000, encoding="utf-8")

        resolver = KnowledgeSliceResolver(tmp_path, max_chars=100)
        result = await resolver.resolve(
            purpose="draft",
            product_ids=["agent-identity-security"],
            include_shared=False,
        )
        assert result.truncated is True


# ── ProductMatcher 测试 ─────────────────────────────────


class TestProductMatcher:
    """产品匹配器测试。"""

    def setup_method(self):
        self.matcher = ProductMatcher()

    def test_match_identity_security(self):
        """匹配智能体身份安全。"""
        article = {
            "title": "智能体身份认证漏洞披露",
            "summary": "Agent身份安全存在越权风险",
            "content": "智能体身份认证机制存在严重漏洞",
        }
        matches = self.matcher.match_by_rules(article)
        assert len(matches) > 0
        assert matches[0].product_id == "agent-identity-security"
        assert matches[0].match_score > 0

    def test_match_ai_bom(self):
        """匹配 AI-BOM。"""
        article = {
            "title": "AI-BOM 供应链安全新标准",
            "summary": "AI物料清单管理日益重要",
            "content": "AI组件供应链安全需要AI-BOM",
        }
        matches = self.matcher.match_by_rules(article)
        assert len(matches) > 0
        assert any(m.product_id == "ai-bom" for m in matches)

    def test_match_no_keywords(self):
        """无关键词命中返回空。"""
        article = {
            "title": "天气预报",
            "summary": "明天晴天",
            "content": "气温30度",
        }
        matches = self.matcher.match_by_rules(article)
        assert len(matches) == 0

    def test_match_top_n(self):
        """Top N 限制。"""
        article = {
            "title": "智能体身份与AI-BOM安全",
            "summary": "Agent身份安全和AI物料清单",
            "content": "智能体身份认证 AI-BOM 供应链安全",
        }
        matches = self.matcher.match_by_rules(article, top_n=1)
        assert len(matches) == 1

    def test_to_snapshot(self):
        """快照格式转换。"""
        article = {
            "title": "智能体身份认证",
            "content": "Agent身份安全",
        }
        matches = self.matcher.match_by_rules(article)
        snapshot = self.matcher.to_snapshot(matches)
        assert isinstance(snapshot, list)
        if snapshot:
            assert "product_id" in snapshot[0]
            assert "match_score" in snapshot[0]
            assert "match_reason" in snapshot[0]
