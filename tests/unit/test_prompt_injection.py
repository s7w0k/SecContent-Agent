"""Task 10.4 custom draft System Prompt rendering tests."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.draft_generator import SYSTEM_PROMPT_TEMPLATE, DraftGenerator
from agent.pr_templates import PR_TEMPLATES


@pytest.fixture
def generator():
    llm = MagicMock()
    llm.temperature = None
    knowledge = MagicMock()
    knowledge.as_system_prompt.return_value = "产品知识内容"
    return DraftGenerator(llm=llm, knowledge=knowledge)


def test_custom_prompt_renders_current_placeholders(generator):
    template = PR_TEMPLATES["爆点事件"][0]
    custom = "专属写作规则\n{knowledge_context}\n{template_spec}\n{style_hints}"

    rendered = generator._build_system_prompt(
        template,
        "技术分析视角",
        "偏好简洁表达",
        template_override=custom,
    )

    assert rendered.startswith("专属写作规则")
    assert "产品知识内容" in rendered
    assert "爆点A" in rendered
    assert "偏好简洁表达" in rendered


def test_default_prompt_behavior_is_unchanged(generator):
    template = PR_TEMPLATES["爆点事件"][0]

    rendered = generator._build_system_prompt(template, "技术分析视角")
    expected = SYSTEM_PROMPT_TEMPLATE.format(
        knowledge_context="产品知识内容",
        template_spec=generator._build_template_spec(template, "技术分析视角"),
        style_hints="\n",
    )

    assert rendered == expected


def test_invalid_custom_placeholder_falls_back_to_default(generator, caplog):
    template = PR_TEMPLATES["爆点事件"][0]
    invalid = "{knowledge_context}\n{template_spec}\n{style_hints}\n{unknown_placeholder}"

    with caplog.at_level("WARNING"):
        rendered = generator._build_system_prompt(
            template,
            "技术分析视角",
            template_override=invalid,
        )

    assert rendered.startswith("你是亚信安全市场部的公众号撰稿人。")
    assert "产品知识内容" in rendered
    assert "降级默认" in caplog.text
