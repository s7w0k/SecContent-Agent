"""PromptResolver 和 PromptComposer 单元测试 - T2 验收。"""

from __future__ import annotations

from agent.prompt_composer import (
    compose_chat_prompt,
    compose_classifier_prompt,
    compose_draft_prompt,
    compose_prompt,
    compose_scoring_prompt,
)


class TestPromptComposer:
    """Prompt 组合器测试。"""

    def test_compose_basic(self):
        """基本组合：固定策略 + 用户业务 + 输出协议。"""
        result = compose_prompt(
            user_business_prompt="关注身份安全",
            output_contract="输出 JSON",
        )
        assert "系统固定指令" in result
        assert "用户业务配置" in result
        assert "关注身份安全" in result
        assert "输出 JSON" in result

    def test_compose_with_readonly_contexts(self):
        """组合包含只读上下文。"""
        result = compose_prompt(
            user_business_prompt="评分关注产品能力",
            readonly_contexts={"产品知识库": "产品A概述"},
        )
        assert "产品知识库（只读）" in result
        assert "产品A概述" in result

    def test_compose_empty_user_prompt(self):
        """用户业务层为空时不出现低信任边界标记。"""
        result = compose_prompt(user_business_prompt="")
        assert "系统固定指令" in result
        assert "低信任" not in result

    def test_compose_classifier(self):
        """分类提示词组合。"""
        result = compose_classifier_prompt(
            user_business_prompt="重点关注AI安全",
            article_context="文章标题和正文",
        )
        assert "文章上下文" in result
        assert "重点关注AI安全" in result

    def test_compose_scoring(self):
        """评分提示词组合。"""
        result = compose_scoring_prompt(
            user_business_prompt="评分口径",
            product_context="产品知识",
            article_context="文章内容",
            score_mode="product_event",
        )
        assert "产品知识库（只读）" in result
        assert "评分模式" in result
        assert "product_event" in result

    def test_compose_draft(self):
        """初稿生成提示词组合。"""
        result = compose_draft_prompt(
            user_business_prompt="写作偏好",
            knowledge_context="知识库内容",
            template_spec="模板规格",
            style_hints="风格提示",
        )
        assert "产品知识库（只读）" in result
        assert "PR模板规格（只读）" in result
        assert "风格偏好（只读）" in result

    def test_compose_chat(self):
        """对话提示词组合。"""
        result = compose_chat_prompt(
            user_business_prompt="改稿原则",
            article_context="文章",
            draft_context="草稿",
            selected_section="选中段落",
        )
        assert "文章上下文（只读）" in result
        assert "草稿上下文（只读）" in result
        assert "选中段落（只读）" in result

    def test_fixed_policy_always_present(self):
        """固定策略层始终存在。"""
        result = compose_prompt(user_business_prompt="")
        assert "不得虚构" in result
        assert "不得泄露" in result

    def test_user_content_marked_low_trust(self):
        """用户内容被低信任边界标记包裹。"""
        result = compose_prompt(user_business_prompt="用户配置内容")
        assert "低信任" in result
        assert "用户业务配置开始" in result
        assert "用户业务配置结束" in result
