"""PromptRegistry 单元测试 - T1 验收。

覆盖：
- 注册表所有键唯一
- 默认模板变量完整
- 必需变量属于允许变量
- 兼容映射
- 模型校验
"""

from __future__ import annotations

import pytest
from agent.prompt_registry import (
    PromptDefinition,
    get_registry,
    resolve_prompt_key,
)
from models.user_prompt import (
    EffectivePrompt,
    PromptRef,
    UserPromptRecord,
    UserPromptUpdate,
    UserPromptVersion,
    compute_content_hash,
)

# ── 注册表基础测试 ───────────────────────────────────────


class TestPromptRegistry:
    """PromptRegistry 核心功能。"""

    def test_all_keys_unique(self):
        """所有 prompt_key 唯一。"""
        registry = get_registry()
        all_defs = registry.list_all()
        keys = [d.prompt_key for d in all_defs]
        assert len(keys) == len(set(keys)), f"Duplicate keys: {keys}"

    def test_all_seven_prompts_registered(self):
        """七类用户业务提示词全部注册。"""
        registry = get_registry()
        expected_keys = {
            "classify_v2_business",
            "score_v2_business",
            "draft_generation_business",
            "chat_answer_business",
            "chat_revise_business",
            "chat_section_revise_business",
            "review_focus_business",
        }
        actual_keys = {d.prompt_key for d in registry.list_all()}
        assert expected_keys == actual_keys

    def test_required_placeholders_subset_of_allowed(self):
        """必需变量必须属于允许变量。"""
        registry = get_registry()
        for d in registry.list_all():
            for req in d.required_placeholders:
                assert req in d.allowed_placeholders, (
                    f"{d.prompt_key}: required '{req}' not in allowed {d.allowed_placeholders}"
                )

    def test_default_content_not_empty(self):
        """默认内容非空。"""
        registry = get_registry()
        for d in registry.list_all():
            assert d.default_content.strip(), f"{d.prompt_key}: default_content is empty"

    def test_default_content_renderable(self):
        """默认内容不包含未闭合的花括号。"""
        registry = get_registry()
        for d in registry.list_all():
            content = d.default_content
            # 花括号可以不成对（JSON 示例中可能有），但不应有未格式化的占位符
            assert "}}" not in content or "{{" in content, (
                f"{d.prompt_key}: possible unescaped braces"
            )

    def test_get_returns_definition(self):
        """get() 返回 PromptDefinition。"""
        registry = get_registry()
        d = registry.get("score_v2_business")
        assert d is not None
        assert d.prompt_key == "score_v2_business"
        assert d.display_name == "产品相关性与事件影响评分"

    def test_get_unknown_key_returns_none(self):
        """get() 对未知 key 返回 None。"""
        registry = get_registry()
        assert registry.get("nonexistent_key") is None

    def test_require_unknown_key_raises(self):
        """require() 对未知 key 抛出 KeyError。"""
        registry = get_registry()
        with pytest.raises(KeyError, match="Unsupported prompt key"):
            registry.require("nonexistent_key")

    def test_is_registered(self):
        """is_registered() 正确识别已注册和未注册的 key。"""
        registry = get_registry()
        assert registry.is_registered("score_v2_business")
        assert not registry.is_registered("nonexistent_key")

    def test_all_definitions_are_prompt_definition(self):
        """list_all() 返回的每个元素都是 PromptDefinition。"""
        registry = get_registry()
        for d in registry.list_all():
            assert isinstance(d, PromptDefinition)

    def test_all_editable(self):
        """第一版所有提示词都可编辑。"""
        registry = get_registry()
        for d in registry.list_all():
            assert d.editable is True, f"{d.prompt_key}: should be editable"

    def test_default_version_is_one(self):
        """第一版所有提示词的 default_version 为 1。"""
        registry = get_registry()
        for d in registry.list_all():
            assert d.default_version == 1, f"{d.prompt_key}: default_version should be 1"


# ── 兼容映射测试 ─────────────────────────────────────────


class TestCompatMapping:
    """旧 draft_system -> draft_generation_business 兼容映射。"""

    def test_draft_system_maps_to_draft_generation_business(self):
        """旧 draft_system 映射到 draft_generation_business。"""
        assert resolve_prompt_key("draft_system") == "draft_generation_business"

    def test_new_key_passes_through(self):
        """新 key 原样返回。"""
        assert resolve_prompt_key("score_v2_business") == "score_v2_business"

    def test_unknown_key_passes_through(self):
        """未知 key 原样返回（不映射）。"""
        assert resolve_prompt_key("unknown") == "unknown"

    def test_registry_resolves_compat_key(self):
        """注册表通过兼容映射找到定义。"""
        registry = get_registry()
        d = registry.get("draft_system")
        assert d is not None
        assert d.prompt_key == "draft_generation_business"

    def test_is_registered_with_compat_key(self):
        """is_registered 通过兼容映射识别旧 key。"""
        registry = get_registry()
        assert registry.is_registered("draft_system")


# ── 模型校验测试 ─────────────────────────────────────────


class TestUserPromptModels:
    """用户提示词模型校验。"""

    def test_user_prompt_update_valid_content(self):
        """合法内容通过校验。"""
        content = "x" * 50  # min_length=50
        update = UserPromptUpdate(content=content)
        assert update.content == content

    def test_user_prompt_update_rejects_short_content(self):
        """内容过短被拒绝。"""
        with pytest.raises(ValueError, match="at least 50 characters"):
            UserPromptUpdate(content="short")

    def test_user_prompt_update_rejects_blank_content(self):
        """空白内容被拒绝。"""
        with pytest.raises(ValueError, match="at least 50 characters"):
            UserPromptUpdate(content="   ")

    def test_user_prompt_update_accepts_expected_version(self):
        """expected_version 可选。"""
        update = UserPromptUpdate(content="x" * 50, expected_version=3)
        assert update.expected_version == 3

    def test_user_prompt_update_without_expected_version(self):
        """expected_version 可省略。"""
        update = UserPromptUpdate(content="x" * 50)
        assert update.expected_version is None

    def test_effective_prompt_defaults(self):
        """EffectivePrompt 默认值正确。"""
        prompt = EffectivePrompt(
            prompt_key="test",
            content="test content",
            is_custom=False,
        )
        assert prompt.source == "system"
        assert prompt.version is None
        assert prompt.default_version == 1
        assert prompt.required_placeholders == []
        assert prompt.allowed_placeholders == []

    def test_prompt_ref_model(self):
        """PromptRef 模型正确。"""
        ref = PromptRef(
            prompt_key="score_v2_business",
            source="user",
            version=3,
            content_hash="sha256:abc123",
        )
        assert ref.prompt_key == "score_v2_business"
        assert ref.source == "user"

    def test_user_prompt_version_model(self):
        """UserPromptVersion 模型正确。"""
        from datetime import UTC, datetime

        version = UserPromptVersion(
            version_id="v-1",
            user_id="u-1",
            prompt_key="score_v2_business",
            version=1,
            content="content",
            content_hash="sha256:abc",
            base_default_version=1,
            change_type="create",
            created_at=datetime.now(UTC),
        )
        assert version.change_type == "create"

    def test_user_prompt_record_defaults(self):
        """UserPromptRecord 默认值正确。"""
        record = UserPromptRecord(
            user_id="u-1",
            prompt_key="test",
            content="content",
        )
        assert record.version == 1
        assert record.base_default_version == 1
        assert record.enabled is True


# ── 哈希函数测试 ─────────────────────────────────────────


class TestContentHash:
    """compute_content_hash 函数。"""

    def test_hash_format(self):
        """哈希格式为 sha256: 开头。"""
        h = compute_content_hash("test content")
        assert h.startswith("sha256:")
        assert len(h) == 71  # "sha256:" + 64 hex chars

    def test_hash_deterministic(self):
        """相同内容生成相同哈希。"""
        h1 = compute_content_hash("test content")
        h2 = compute_content_hash("test content")
        assert h1 == h2

    def test_hash_different_for_different_content(self):
        """不同内容生成不同哈希。"""
        h1 = compute_content_hash("content A")
        h2 = compute_content_hash("content B")
        assert h1 != h2
