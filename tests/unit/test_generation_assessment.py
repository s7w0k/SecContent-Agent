"""T4+T5 单元测试：生成偏好、评估服务和评分模式。"""

from __future__ import annotations

import pytest
from agent.assessment_service import AssessmentService
from models.article_assessment import (
    ClassificationAssessment,
    ScoringAssessment,
    compute_input_fingerprint,
)
from models.generation_config import (
    GenerationOptions,
    ProductTargetMode,
    ScoreMode,
    UserGenerationPreferences,
    compute_config_fingerprint,
    merge_options_with_preferences,
)

# ── 生成偏好模型测试 ─────────────────────────────────────


class TestGenerationOptions:
    """GenerationOptions 模型校验。"""

    def test_none_mode_disables_relevance(self):
        """none 模式自动关闭产品相关性。"""
        opts = GenerationOptions(
            product_target_mode=ProductTargetMode.NONE,
        )
        assert opts.product_relevance_enabled is False

    def test_none_mode_with_relevance_enabled_raises(self):
        """none 模式不允许启用产品相关性。"""
        with pytest.raises(ValueError, match="none 模式不允许启用产品相关性"):
            GenerationOptions(
                product_target_mode=ProductTargetMode.NONE,
                product_relevance_enabled=True,
            )

    def test_selected_mode_empty_products_raises(self):
        """selected 模式必须指定产品。"""
        with pytest.raises(ValueError, match="selected 模式必须指定至少一个产品"):
            GenerationOptions(
                product_target_mode=ProductTargetMode.SELECTED,
                selected_product_ids=[],
            )

    def test_selected_mode_with_products_ok(self):
        """selected 模式有产品列表通过。"""
        opts = GenerationOptions(
            product_target_mode=ProductTargetMode.SELECTED,
            selected_product_ids=["agent-identity-security"],
        )
        assert opts.selected_product_ids == ["agent-identity-security"]

    def test_defaults_are_none(self):
        """默认值为 None（使用账号级偏好）。"""
        opts = GenerationOptions()
        assert opts.product_relevance_enabled is None
        assert opts.product_target_mode is None
        assert opts.selected_product_ids == []
        assert opts.force_generate is False


class TestUserGenerationPreferences:
    """账号级偏好模型。"""

    def test_defaults(self):
        """系统默认值。"""
        prefs = UserGenerationPreferences(user_id="u-1")
        assert prefs.product_relevance_enabled is True
        assert prefs.product_target_mode == ProductTargetMode.AUTO
        assert prefs.selected_product_ids == []
        assert prefs.product_event_threshold == 80
        assert prefs.event_only_threshold == 60

    def test_none_mode_validation(self):
        """none 模式校验。"""
        with pytest.raises(ValueError, match="none 模式不允许启用产品相关性"):
            UserGenerationPreferences(
                user_id="u-1",
                product_relevance_enabled=True,
                product_target_mode=ProductTargetMode.NONE,
            )

    def test_selected_mode_validation(self):
        """selected 模式校验。"""
        with pytest.raises(ValueError, match="selected 模式必须指定至少一个产品"):
            UserGenerationPreferences(
                user_id="u-1",
                product_target_mode=ProductTargetMode.SELECTED,
                selected_product_ids=[],
            )


class TestMergeOptions:
    """单次请求与账号偏好合并。"""

    def test_no_options_uses_preferences(self):
        """无单次选项时使用偏好。"""
        prefs = UserGenerationPreferences(
            user_id="u-1",
            product_target_mode=ProductTargetMode.SELECTED,
            selected_product_ids=["ai-bom"],
        )
        relevance, mode, products = merge_options_with_preferences(None, prefs)
        assert relevance is True
        assert mode == ProductTargetMode.SELECTED
        assert products == ["ai-bom"]

    def test_no_options_no_preferences_uses_defaults(self):
        """无单次选项和偏好时使用系统默认。"""
        relevance, mode, products = merge_options_with_preferences(None, None)
        assert relevance is True
        assert mode == ProductTargetMode.AUTO
        assert products == []

    def test_options_override_preferences(self):
        """单次选项覆盖偏好。"""
        prefs = UserGenerationPreferences(
            user_id="u-1",
            product_target_mode=ProductTargetMode.AUTO,
        )
        opts = GenerationOptions(
            product_target_mode=ProductTargetMode.SELECTED,
            selected_product_ids=["agent-identity-security"],
        )
        _relevance, mode, products = merge_options_with_preferences(opts, prefs)
        assert mode == ProductTargetMode.SELECTED
        assert products == ["agent-identity-security"]

    def test_none_mode_forces_relevance_off(self):
        """none 模式强制关闭产品相关性。"""
        opts = GenerationOptions(product_target_mode=ProductTargetMode.NONE)
        relevance, mode, _products = merge_options_with_preferences(opts, None)
        assert relevance is False
        assert mode == ProductTargetMode.NONE


class TestConfigFingerprint:
    """配置指纹计算。"""

    def test_stable(self):
        """相同输入产生相同指纹。"""
        kwargs = {
            "product_relevance_enabled": True,
            "product_target_mode": "selected",
            "selected_product_ids": ["a", "b"],
            "knowledge_hash": "sha256:abc",
            "prompt_refs": [{"prompt_key": "score_v2_business", "version": 1}],
        }
        h1 = compute_config_fingerprint(**kwargs)
        h2 = compute_config_fingerprint(**kwargs)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_input_different_hash(self):
        """不同输入产生不同指纹。"""
        base = {
            "product_relevance_enabled": True,
            "product_target_mode": "auto",
            "selected_product_ids": [],
            "knowledge_hash": "sha256:abc",
            "prompt_refs": [],
        }
        h1 = compute_config_fingerprint(**base)
        h2 = compute_config_fingerprint(**{**base, "product_target_mode": "selected"})
        assert h1 != h2

    def test_product_order_independent(self):
        """产品 ID 顺序不影响指纹。"""
        h1 = compute_config_fingerprint(
            product_relevance_enabled=True,
            product_target_mode="selected",
            selected_product_ids=["a", "b"],
            knowledge_hash="sha256:abc",
            prompt_refs=[],
        )
        h2 = compute_config_fingerprint(
            product_relevance_enabled=True,
            product_target_mode="selected",
            selected_product_ids=["b", "a"],
            knowledge_hash="sha256:abc",
            prompt_refs=[],
        )
        assert h1 == h2


# ── 评分模式测试 ─────────────────────────────────────────


class TestScoreMode:
    """ScoreMode 枚举。"""

    def test_values(self):
        assert ScoreMode.PRODUCT_EVENT.value == "product_event"
        assert ScoreMode.EVENT_ONLY.value == "event_only"


# ── 评估服务测试 ─────────────────────────────────────────


class TestAssessmentService:
    """AssessmentService 评分结果计算。"""

    def test_product_event_mode(self):
        """product_event 模式：candidate_score = pr + ei (0-200)。"""
        result = AssessmentService.compute_scoring_result(
            product_relevance=85,
            event_impact=70,
            product_relevance_enabled=True,
            threshold=80,
        )
        assert result.score_mode == "product_event"
        assert result.product_relevance == 85
        assert result.event_impact == 70
        assert result.candidate_score == 155
        assert result.candidate_threshold == 80
        assert result.is_pr_candidate is True

    def test_event_only_mode(self):
        """event_only 模式：candidate_score = ei (0-100)，pr=null。"""
        result = AssessmentService.compute_scoring_result(
            product_relevance=85,
            event_impact=70,
            product_relevance_enabled=False,
            threshold=60,
        )
        assert result.score_mode == "event_only"
        assert result.product_relevance is None
        assert result.event_impact == 70
        assert result.candidate_score == 70
        assert result.candidate_threshold == 60
        assert result.is_pr_candidate is True

    def test_event_only_below_threshold(self):
        """event_only 低于阈值。"""
        result = AssessmentService.compute_scoring_result(
            product_relevance=85,
            event_impact=50,
            product_relevance_enabled=False,
            threshold=60,
        )
        assert result.is_pr_candidate is False

    def test_product_relevance_null_not_zero(self):
        """关闭产品相关性时返回 None 不是 0。"""
        result = AssessmentService.compute_scoring_result(
            product_relevance=None,
            event_impact=75,
            product_relevance_enabled=False,
            threshold=60,
        )
        assert result.product_relevance is None
        assert result.candidate_score == 75

    def test_product_event_below_threshold(self):
        """product_event 低于阈值。"""
        result = AssessmentService.compute_scoring_result(
            product_relevance=30,
            event_impact=40,
            product_relevance_enabled=True,
            threshold=80,
        )
        assert result.candidate_score == 70
        assert result.is_pr_candidate is False


# ── 输入指纹测试 ─────────────────────────────────────────


class TestInputFingerprint:
    """输入指纹计算。"""

    def test_stable(self):
        """相同输入相同指纹。"""
        kwargs = {
            "user_id": "u-1",
            "article_url_hash": "md5:abc",
            "prompt_refs": [
                {
                    "prompt_key": "score_v2",
                    "version": 1,
                    "source": "user",
                    "content_hash": "sha256:x",
                }
            ],
            "product_snapshot": {
                "mode": "selected",
                "requested_product_ids": ["a"],
                "resolved_products": [],
                "knowledge_hash": "sha256:k",
            },
            "knowledge_hash": "sha256:k",
        }
        h1 = compute_input_fingerprint(**kwargs)
        h2 = compute_input_fingerprint(**kwargs)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_different_user_different_hash(self):
        """不同用户不同指纹。"""
        base = {
            "user_id": "u-1",
            "article_url_hash": "md5:abc",
            "prompt_refs": [],
            "product_snapshot": {
                "mode": "auto",
                "requested_product_ids": [],
                "resolved_products": [],
                "knowledge_hash": "",
            },
            "knowledge_hash": "",
        }
        h1 = compute_input_fingerprint(**base)
        h2 = compute_input_fingerprint(**{**base, "user_id": "u-2"})
        assert h1 != h2

    def test_different_product_different_hash(self):
        """不同产品选择不同指纹。"""
        base = {
            "user_id": "u-1",
            "article_url_hash": "md5:abc",
            "prompt_refs": [],
            "product_snapshot": {
                "mode": "selected",
                "requested_product_ids": ["a"],
                "resolved_products": [],
                "knowledge_hash": "sha256:k",
            },
            "knowledge_hash": "sha256:k",
        }
        h1 = compute_input_fingerprint(**base)
        h2 = compute_input_fingerprint(
            **{
                **base,
                "product_snapshot": {**base["product_snapshot"], "requested_product_ids": ["b"]},
            }
        )
        assert h1 != h2


# ── 评估模型测试 ─────────────────────────────────────────


class TestAssessmentModels:
    """评估模型结构。"""

    def test_classification_defaults(self):
        c = ClassificationAssessment()
        assert c.category_v2 == ""
        assert c.confidence == 0
        assert c.is_pr_eligible is False

    def test_scoring_defaults(self):
        s = ScoringAssessment()
        assert s.score_mode == "product_event"
        assert s.product_relevance is None
        assert s.event_impact == 0
        assert s.candidate_score == 0
        assert s.is_pr_candidate is False
