"""T9 迁移脚本和 Feature Flags 测试。"""

from __future__ import annotations

from config import Settings


class TestFeatureFlags:
    """Feature Flags 配置测试。"""

    def test_defaults_all_enabled(self):
        """默认全部启用。"""
        settings = Settings()
        assert settings.USER_PROMPT_V2_ENABLED is True
        assert settings.PRODUCT_CATALOG_ENABLED is True
        assert settings.GENERATION_PREFERENCES_ENABLED is True
        assert settings.USER_ASSESSMENT_ENABLED is True
        assert settings.PIPELINE_CONFIG_FREEZE_ENABLED is True
        assert settings.LEGACY_GLOBAL_SCORE_AS_FALLBACK is True

    def test_can_disable_via_env(self, monkeypatch):
        """可通过环境变量关闭。"""
        monkeypatch.setenv("USER_PROMPT_V2_ENABLED", "false")
        monkeypatch.setenv("LEGACY_GLOBAL_SCORE_AS_FALLBACK", "false")
        settings = Settings()
        assert settings.USER_PROMPT_V2_ENABLED is False
        assert settings.LEGACY_GLOBAL_SCORE_AS_FALLBACK is False

    def test_can_selectively_enable(self, monkeypatch):
        """可选择性启用。"""
        monkeypatch.setenv("USER_PROMPT_V2_ENABLED", "true")
        monkeypatch.setenv("USER_ASSESSMENT_ENABLED", "false")
        settings = Settings()
        assert settings.USER_PROMPT_V2_ENABLED is True
        assert settings.USER_ASSESSMENT_ENABLED is False


class TestMigrationScript:
    """迁移脚本结构测试。"""

    def test_migrate_functions_exist(self):
        """迁移函数存在。"""
        from scripts.migrate_stage14 import (
            mark_legacy_scores,
            migrate_draft_system,
            patch_old_drafts,
        )

        assert callable(migrate_draft_system)
        assert callable(mark_legacy_scores)
        assert callable(patch_old_drafts)
