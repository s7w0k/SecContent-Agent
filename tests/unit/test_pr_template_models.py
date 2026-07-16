"""用户自定义 PR 模板数据模型测试。"""

from __future__ import annotations

import pytest
from models.pr_template import (
    MAX_TEMPLATE_SERIALIZED_LENGTH,
    EffectivePRTemplate,
    PRTemplateCategory,
    TemplateChangeType,
    TemplateKey,
    TemplateSection,
    TemplateSlot,
    TemplateSnapshot,
    TemplateSource,
    UserPRTemplate,
    UserPRTemplateUpdate,
    UserPRTemplateVersion,
)
from pydantic import ValidationError


def _content() -> dict:
    return {
        "name": "我的爆点模板",
        "title_template": "# [事件名称]：影响分析",
        "sections": [
            {"heading": "技术分析", "guide": "分析攻击原理", "order": 2},
            {"heading": "事件概述", "guide": "说明事件背景", "order": 1},
        ],
        "perspectives": ["技术视角", "市场视角"],
        "extra_instructions": "突出智能体身份风险",
    }


def _snapshot() -> TemplateSnapshot:
    return TemplateSnapshot(
        **_content(),
        template_key=TemplateKey.BREAKING_A,
        category_v2=PRTemplateCategory.BREAKING_EVENT,
        slot=TemplateSlot.A,
    )


class TestTemplateContentValidation:
    def test_normalizes_text_and_section_order(self):
        payload = _content()
        payload["name"] = "  我的模板  "
        payload["perspectives"] = [" 技术视角 ", " 市场视角 "]

        model = UserPRTemplateUpdate(**payload, expected_version=2)

        assert model.name == "我的模板"
        assert model.perspectives == ["技术视角", "市场视角"]
        assert [section.heading for section in model.sections] == ["事件概述", "技术分析"]
        assert [section.order for section in model.sections] == [1, 2]
        assert model.expected_version == 2

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "   "),
            ("title_template", ""),
            ("perspectives", ["技术视角", "   "]),
            ("perspectives", ["同一视角", "同一视角"]),
        ],
    )
    def test_rejects_blank_or_duplicate_content(self, field: str, value: object):
        payload = _content()
        payload[field] = value

        with pytest.raises(ValidationError):
            UserPRTemplateUpdate(**payload)

    def test_requires_exactly_two_perspectives(self):
        payload = _content()
        payload["perspectives"] = ["技术视角"]

        with pytest.raises(ValidationError):
            UserPRTemplateUpdate(**payload)

    def test_rejects_duplicate_section_headings(self):
        payload = _content()
        payload["sections"][1]["heading"] = "技术分析"

        with pytest.raises(ValidationError, match="section headings must be distinct"):
            UserPRTemplateUpdate(**payload)

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            UserPRTemplateUpdate(**_content(), user_id="attacker")

    def test_section_rejects_oversized_guide(self):
        with pytest.raises(ValidationError):
            TemplateSection(heading="章节", guide="x" * 1001, order=1)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("name", "名" * 101),
            ("title_template", "题" * 301),
            ("extra_instructions", "要求" * 1001),
            ("expected_version", 0),
        ],
    )
    def test_rejects_field_values_beyond_defined_boundaries(
        self,
        field: str,
        value: object,
    ) -> None:
        payload = _content()
        payload[field] = value

        with pytest.raises(ValidationError):
            UserPRTemplateUpdate(**payload)

    def test_accepts_exactly_twelve_sections(self) -> None:
        payload = _content()
        payload["sections"] = [
            {"heading": f"章节 {index}", "guide": "写作指引", "order": index}
            for index in range(1, 13)
        ]

        model = UserPRTemplateUpdate(**payload)

        assert len(model.sections) == 12
        assert model.sections[-1].order == 12

    @pytest.mark.parametrize("count", [0, 13])
    def test_rejects_section_count_outside_one_to_twelve(self, count: int) -> None:
        payload = _content()
        payload["sections"] = [
            {"heading": f"章节 {index}", "guide": "写作指引", "order": index}
            for index in range(1, count + 1)
        ]

        with pytest.raises(ValidationError):
            UserPRTemplateUpdate(**payload)

    def test_rejects_oversized_serialized_template(self) -> None:
        payload = _content()
        payload["perspectives"] = ["技术视角", "x" * MAX_TEMPLATE_SERIALIZED_LENGTH]

        with pytest.raises(ValidationError, match="template content exceeds"):
            UserPRTemplateUpdate(**payload)


class TestTemplateIdentity:
    def test_snapshot_accepts_matching_identity(self):
        snapshot = _snapshot()

        assert snapshot.template_key == TemplateKey.BREAKING_A
        assert snapshot.category_v2 == PRTemplateCategory.BREAKING_EVENT
        assert snapshot.slot == TemplateSlot.A

    def test_snapshot_rejects_mismatched_slot(self):
        with pytest.raises(ValidationError, match="template_key does not match"):
            TemplateSnapshot(
                **_content(),
                template_key=TemplateKey.BREAKING_A,
                category_v2=PRTemplateCategory.BREAKING_EVENT,
                slot=TemplateSlot.B,
            )

    def test_user_document_has_generated_identity_and_defaults(self):
        template = UserPRTemplate(
            **_content(),
            user_id="user-a",
            template_key=TemplateKey.LAW_B,
            category_v2=PRTemplateCategory.LAW_AND_REGULATION,
            slot=TemplateSlot.B,
        )

        assert template.template_id.startswith("tpl-")
        assert template.version == 1
        assert template.base_system_version == 1
        assert template.enabled is True
        assert template.created_at.tzinfo is not None

    def test_user_document_rejects_key_category_mismatch(self):
        with pytest.raises(ValidationError, match="template_key does not match"):
            UserPRTemplate(
                **_content(),
                user_id="user-a",
                template_key=TemplateKey.AI_A,
                category_v2=PRTemplateCategory.BREAKING_EVENT,
                slot=TemplateSlot.A,
            )


class TestTemplateResponseAndVersion:
    def test_effective_template_carries_source_and_versions(self):
        effective = EffectivePRTemplate(
            **_snapshot().model_dump(),
            template_id="system:breaking_a",
            source=TemplateSource.SYSTEM,
            version=1,
            system_version=1,
        )

        assert effective.source == TemplateSource.SYSTEM
        assert effective.template_id == "system:breaking_a"

    def test_version_document_accepts_matching_snapshot(self):
        version = UserPRTemplateVersion(
            template_id="tpl-user-a",
            user_id="user-a",
            template_key=TemplateKey.BREAKING_A,
            version=2,
            snapshot=_snapshot(),
            change_type=TemplateChangeType.UPDATE,
        )

        assert version.version_id.startswith("tplv-")
        assert version.snapshot.template_key == version.template_key

    def test_version_document_rejects_snapshot_from_another_template(self):
        with pytest.raises(ValidationError, match="does not match snapshot"):
            UserPRTemplateVersion(
                template_id="tpl-user-a",
                user_id="user-a",
                template_key=TemplateKey.AI_A,
                version=2,
                snapshot=_snapshot(),
                change_type=TemplateChangeType.RESTORE,
            )
