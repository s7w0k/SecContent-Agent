"""T6 单元测试：PipelineConfigFreezer 和 Agent 参数接入。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.pipeline_config import PipelineConfigFreezer
from models.generation_config import (
    GenerationOptions,
    ProductTargetMode,
    ScoreMode,
)


class TestPipelineConfigFreezer:
    """PipelineConfigFreezer 测试。"""

    @pytest.mark.asyncio
    async def test_freeze_none_mode(self):
        """none 模式：关闭产品相关性，不注入产品知识。"""
        mock_db = AsyncMock()
        mock_db["user_generation_preferences"].find_one = AsyncMock(return_value=None)
        mock_db["user_prompts"].find_one = AsyncMock(return_value=None)
        mock_db["user_prompt_versions"].find_one = AsyncMock(return_value=None)

        resolver = MagicMock()
        resolver._db = mock_db
        resolver.freeze_many = AsyncMock(return_value=[])

        freezer = PipelineConfigFreezer(resolver)
        snapshot = await freezer.freeze(
            user_id="u-1",
            options=GenerationOptions(product_target_mode=ProductTargetMode.NONE),
        )

        assert snapshot.product_relevance_enabled is False
        assert snapshot.score_mode == ScoreMode.EVENT_ONLY
        assert snapshot.product_target_mode == ProductTargetMode.NONE
        assert snapshot.resolved_products == []
        assert snapshot.knowledge_hash == "sha256:none"
        assert snapshot.config_fingerprint.startswith("sha256:")

    @pytest.mark.asyncio
    async def test_freeze_auto_mode_without_article(self):
        """auto 模式无文章：使用目录哈希。"""
        mock_db = AsyncMock()
        mock_db["user_generation_preferences"].find_one = AsyncMock(return_value=None)
        mock_db["user_prompts"].find_one = AsyncMock(return_value=None)
        mock_db["user_prompt_versions"].find_one = AsyncMock(return_value=None)

        resolver = MagicMock()
        resolver._db = mock_db
        resolver.freeze_many = AsyncMock(return_value=[])

        freezer = PipelineConfigFreezer(resolver)
        snapshot = await freezer.freeze(
            user_id="u-1",
            options=None,  # 使用系统默认（auto）
        )

        assert snapshot.product_relevance_enabled is True
        assert snapshot.score_mode == ScoreMode.PRODUCT_EVENT
        assert snapshot.product_target_mode == ProductTargetMode.AUTO
        assert snapshot.config_fingerprint.startswith("sha256:")

    @pytest.mark.asyncio
    async def test_freeze_force_generate(self):
        """force_generate 被正确传递。"""
        mock_db = AsyncMock()
        mock_db["user_generation_preferences"].find_one = AsyncMock(return_value=None)
        mock_db["user_prompts"].find_one = AsyncMock(return_value=None)
        mock_db["user_prompt_versions"].find_one = AsyncMock(return_value=None)

        resolver = MagicMock()
        resolver._db = mock_db
        resolver.freeze_many = AsyncMock(return_value=[])

        freezer = PipelineConfigFreezer(resolver)
        snapshot = await freezer.freeze(
            user_id="u-1",
            options=GenerationOptions(force_generate=True),
        )

        assert snapshot.force_generate is True


class TestDraftGeneratorKnowledgeSlice:
    """DraftGenerator 知识切片接入测试。"""

    def test_build_system_prompt_with_knowledge_slice(self):
        """知识切片替代全局知识库。"""
        from agent.draft_generator import DraftGenerator

        mock_llm = MagicMock()
        mock_knowledge = MagicMock()
        mock_knowledge.as_system_prompt.return_value = "全局知识库内容"

        generator = DraftGenerator(llm=mock_llm, knowledge=mock_knowledge)

        # 不传 knowledge_slice -> 使用全局知识
        generator._build_system_prompt.__wrapped__ if hasattr(generator._build_system_prompt, '__wrapped__') else None

        # 直接测试方法
        from agent.pr_templates import PRTemplate
        template = PRTemplate(
            name="测试模板",
            category="爆点事件",
            title_template="测试标题",
            sections=[],
            perspectives=["角度1"],
        )

        prompt_default = generator._build_system_prompt(template, "角度1")
        assert "全局知识库内容" in prompt_default

        prompt_slice = generator._build_system_prompt(
            template, "角度1", knowledge_slice="切片知识内容"
        )
        assert "切片知识内容" in prompt_slice
        assert "全局知识库内容" not in prompt_slice

    def test_build_system_prompt_with_user_business_prompt(self):
        """用户业务提示词被追加。"""
        from agent.draft_generator import DraftGenerator
        from agent.pr_templates import PRTemplate

        mock_llm = MagicMock()
        mock_knowledge = MagicMock()
        mock_knowledge.as_system_prompt.return_value = "全局知识库"

        generator = DraftGenerator(llm=mock_llm, knowledge=mock_knowledge)
        template = PRTemplate(
            name="测试",
            category="爆点事件",
            title_template="标题",
            sections=[],
            perspectives=["角度"],
        )

        prompt = generator._build_system_prompt(
            template, "角度",
            user_business_prompt="用户自定义业务说明",
        )
        assert "用户业务配置" in prompt
        assert "用户自定义业务说明" in prompt


class TestDraftReviewerUserFocus:
    """DraftReviewer 用户关注项接入测试。"""

    def test_review_accepts_user_focus_items(self):
        """review 方法接受 user_focus_items 参数。"""
        import inspect

        from agent.draft_reviewer import DraftReviewer

        sig = inspect.signature(DraftReviewer.review)
        assert "user_focus_items" in sig.parameters
        assert sig.parameters["user_focus_items"].default is None
