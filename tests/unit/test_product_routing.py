"""阶段1：产品自动路由修复测试。

覆盖：
  S1-1 字段提取：summary_cn/content_md 参与路由、空白归一化
  S1-2 目录一致性：已发布产品有关键词、未发布产品不进入 auto 路由
  S1-3 置信度/歧义：产品名/别名/英文别名命中、通用词不形成高置信、歧义判定
  S1-4 LLM 重排：非法产品 ID 被拒绝、失败回退规则
  S1-5 统一解析：selected 不调用自动匹配、none 返回空、auto、用户产品按 user 隔离
"""

from __future__ import annotations

import pytest
from agent.product_catalog import ProductCatalogService
from agent.product_matcher import CONFIDENCE_HIGH, ProductMatch, ProductMatcher
from agent.product_routing import LLMProductReranker, ProductRoutingService
from models.generation_config import ProductTargetMode


@pytest.fixture()
def matcher():
    return ProductMatcher()


class TestS1_1FieldExtraction:
    """S1-1：文章真实字段参与路由 + 空白归一化。"""

    def test_summary_cn_content_md_participate(self, matcher):
        """summary_cn/content_md 应参与路由（此前只读 summary/content）。"""
        article = {
            "title": "AI-BOM 资产盘点",
            "summary_cn": "AI 资产台账登记与风险跟踪",
            "content_md": "围绕 AI 组件供应链安全展开",
        }
        matches = matcher.match_by_rules(article)
        assert any(m.product_id == "ai-bom" for m in matches)

    def test_space_insensitive_matching(self, matcher):
        """'AI 资产' 与 'AI资产' 应视为等价（空白归一化）。"""
        article = {
            "title": "AI 资产发现与自动盘点",
            "summary_cn": "自动发现并盘点组织内的 AI 资产",
            "content_md": "覆盖 AI 资产自动发现与盘点能力",
        }
        matches = matcher.match_by_rules(article)
        assert matches and matches[0].product_id == "ai-bom"

    def test_agent_runtime_space(self, matcher):
        """'agent runtime'（含空格）应对应 agent-security。"""
        article = {
            "title": "Agent Runtime 沙箱逃逸防护",
            "summary_cn": "智能体运行时沙箱隔离加固",
            "content_md": "围绕 agent runtime 沙箱隔离研究逃逸防护",
        }
        matches = matcher.match_by_rules(article)
        assert matches and matches[0].product_id == "agent-security"

    def test_content_length_capped(self, matcher):
        """超长正文应被截断，避免把全文直接交给路由器。"""
        huge = "智能体身份认证 " * 100_000
        article = {"title": "测试", "summary_cn": "", "content_md": huge}
        fields = ProductMatcher._field_texts(article)
        assert len(fields["content"]) <= 100_000  # 归一化后仍受控


class TestS1_2CatalogConsistency:
    """S1-2：目录一致性。"""

    def test_all_published_products_have_keywords(self):
        catalog = ProductCatalogService()
        for p in catalog.list_products(published_only=True):
            assert p.keywords, f"已发布产品 {p.product_id} 缺少关键词"

    def test_unpublished_not_in_auto_route(self, matcher):
        """未发布产品（agent-security-gateway / ans）不进入 auto 路由结果。"""
        article = {
            "title": "智能体安全网关流量管控方案",
            "summary_cn": "API网关与流量管控",
            "content_md": "安全网关管控 Agent 流量",
        }
        ids = [m.product_id for m in matcher.match_by_rules(article)]
        assert "agent-security-gateway" not in ids
        assert "ans" not in ids

    def test_catalog_keywords_are_preferred(self):
        """matcher 优先使用 catalog keywords（而非 legacy _PRODUCT_KEYWORDS）。"""
        catalog = ProductCatalogService()
        with_site = catalog.get_product("agent-identity-security")
        assert with_site.keywords  # catalog 已收敛关键词


class TestS1_3ConfidenceAmbiguity:
    """S1-3：置信度与歧义。"""

    def test_product_name_alias_hit(self, matcher):
        """产品名/中文别名命中应产生高置信（标题别名 + 摘要关键词累积到阈值）。"""
        article = {
            "title": "智能体身份安全新进展",
            "summary_cn": "探讨身份认证与最小权限授权方案",
            "content_md": "",
        }
        matches = matcher.match_by_rules(article)
        assert matches
        assert matches[0].product_id == "agent-identity-security"
        assert matches[0].match_score >= CONFIDENCE_HIGH

    def test_english_alias_hit(self, matcher):
        """英文别名命中（Agent Identity Security）。"""
        article = {
            "title": "Agent Identity Security research",
            "summary_cn": "",
            "content_md": "",
        }
        matches = matcher.match_by_rules(article)
        assert matches and matches[0].product_id == "agent-identity-security"

    def test_generic_word_not_high_confidence(self, matcher):
        """通用词'供应链安全'单独命中不应形成高置信。"""
        article = {
            "title": "供应链安全趋势",
            "summary_cn": "供应链安全受到关注",
            "content_md": "供应链安全",
        }
        matches = matcher.match_by_rules(article)
        # 即便命中，分数也应低于高置信阈值
        assert all(m.match_score < CONFIDENCE_HIGH for m in matches)

    def test_ambiguous_detection(self, matcher):
        """Top1 与 Top2 分差过小应判定为歧义。"""
        article = {
            "title": "身份认证与运行时防护的新进展",
            "summary_cn": "",
            "content_md": "",
        }
        matches = matcher.match_by_rules(article, top_n=2)
        assert len(matches) >= 2
        assert ProductMatcher.is_ambiguous(matches)

    def test_no_match_is_ambiguous(self, matcher):
        assert ProductMatcher.is_ambiguous([]) is True

    def test_noise_weak_match_filtered_out(self, matcher):
        """次优产品与最高分产品分差过大时应被过滤（避免命中禁止产品误隔离）。

        对应基线评测 route-003/005：主产品高分命中 + 次优弱词（约20分）命中，
        弱词噪声不应进入路由结果。
        """
        article = {
            "title": "运行时治理：智能体凭证生命周期管理",
            "summary_cn": "智能体运行时凭证的签发、轮换、吊销与审计",
            "content_md": "身份治理与权限边界",
        }
        matches = matcher.match_by_rules(article, top_n=2)
        ids = [m.product_id for m in matches]
        # 主产品 agent-identity-security 命中；弱词命中的 agent-security 应被过滤
        assert "agent-identity-security" in ids
        assert "agent-security" not in ids

    def test_single_low_score_match_kept(self, matcher):
        """仅一个产品命中（即使分数低）时仍应返回，作为唯一合法信号。

        对应基线评测 route-027/030：只命中 agent-security，分数低但不可过滤。
        """
        article = {
            "title": "RAG 系统的安全防护最佳实践",
            "summary_cn": "RAG 检索增强生成系统的安全防护与加固",
            "content_md": "给出 RAG 系统的智能体防护最佳实践",
        }
        matches = matcher.match_by_rules(article, top_n=2)
        ids = [m.product_id for m in matches]
        assert "agent-security" in ids


class TestS1_4LLMReranker:
    """S1-4：LLM 重排器。"""

    async def _reranker(self, llm_return):
        catalog = ProductCatalogService()
        return LLMProductReranker(catalog=catalog, llm_call=llm_return)

    async def test_valid_rerank(self):
        async def llm(prompt):
            return '{"ranked_product_ids": ["ai-bom", "agent-security"]}'

        reranker = await self._reranker(llm)
        candidates = [
            ProductMatch("agent-security", "智能体安全", 20, "x"),
            ProductMatch("ai-bom", "AI-BOM", 20, "x"),
        ]
        result = await reranker.rerank({}, candidates)
        assert [m.product_id for m in result] == ["ai-bom", "agent-security"]

    async def test_invalid_product_id_rejected(self):
        async def llm(prompt):
            return '{"ranked_product_ids": ["ai-bom", "not-a-product"]}'

        reranker = await self._reranker(llm)
        candidates = [ProductMatch("ai-bom", "AI-BOM", 20, "x")]
        result = await reranker.rerank({}, candidates)
        assert [m.product_id for m in result] == ["ai-bom"]

    async def test_unpublished_rejected(self):
        async def llm(prompt):
            return '{"ranked_product_ids": ["ans"]}'

        reranker = await self._reranker(llm)
        candidates = [ProductMatch("ans", "ANS", 20, "x")]
        # ans 未发布，被拒绝 → 回退原候选
        result = await reranker.rerank({}, candidates)
        assert [m.product_id for m in result] == ["ans"]  # 回退规则

    async def test_failure_falls_back_to_rules(self):
        async def llm(prompt):
            raise RuntimeError("llm down")

        reranker = await self._reranker(llm)
        candidates = [ProductMatch("ai-bom", "AI-BOM", 20, "x")]
        result = await reranker.rerank({}, candidates)
        assert [m.product_id for m in result] == ["ai-bom"]

    async def test_invalid_json_falls_back(self):
        async def llm(prompt):
            return "not json at all"

        reranker = await self._reranker(llm)
        candidates = [ProductMatch("ai-bom", "AI-BOM", 20, "x")]
        result = await reranker.rerank({}, candidates)
        assert [m.product_id for m in result] == ["ai-bom"]


class TestS1_5RoutingService:
    """S1-5：统一解析。"""

    @pytest.fixture()
    def service(self):
        return ProductRoutingService()

    async def test_selected_does_not_call_automatic(self, service):
        """selected 模式严格按用户选择，不调用自动匹配。"""
        snap = await service.resolve({}, ProductTargetMode.SELECTED.value, ["ai-bom"], "u-1")
        assert snap.mode == "selected"
        assert snap.product_ids == ["ai-bom"]
        assert snap.resolved_products[0].match_source == "user_selected"

    async def test_none_returns_empty(self, service):
        snap = await service.resolve({}, "none", [], "u-1")
        assert snap.mode == "none"
        assert snap.product_ids == []

    async def test_selected_invalid_id_raises_value_error(self, service):
        with pytest.raises(ValueError):
            await service.resolve({}, "selected", ["not-a-product"], "u-1")

    async def test_auto_returns_snapshot(self, service):
        article = {
            "title": "AI-BOM 资产清单管理",
            "summary_cn": "AI 资产台账",
            "content_md": "AI 组件供应链安全",
        }
        snap = await service.resolve(article, "auto", [], "u-1")
        assert snap.mode == "auto"
        assert snap.product_ids
        assert snap.routing_version.startswith("sha256:")

    async def test_user_products_isolated_by_user(self, matcher):
        """用户级产品仅在该用户列表传入时参与路由。"""
        article = {
            "title": "某公司发布新的数据安全平台",
            "summary_cn": "集成了数据治理和安全防护",
            "content_md": "支持多种数据源",
        }
        user_products = [
            {
                "product_id": "up-data-security",
                "name": "数据安全平台",
                "aliases": ["数据安全"],
                "keywords": ["数据治理", "安全防护"],
            }
        ]
        # 不传 user_products：不出现用户产品
        ids = [m.product_id for m in matcher.match_by_rules(article)]
        assert "up-data-security" not in ids
        # 传入 user_products：出现用户产品
        ids2 = [m.product_id for m in matcher.match_by_rules(article, user_products=user_products)]
        assert "up-data-security" in ids2
