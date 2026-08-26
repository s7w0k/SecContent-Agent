"""PR-01 Wiki Contracts 单元测试。"""

from __future__ import annotations

import pytest
from agent.wiki.contracts import (
    PageIdError,
    SourceRef,
    WikiError,
    WikiRelation,
    is_path_safe,
    is_valid_page_id,
    path_traversal,
    safe_page_path,
    slugify,
    validate_page_id,
)


class TestSourceRef:
    def test_source_ref_defaults(self):
        ref = SourceRef(source_id="src_ab", relative_path="a/b.md", content_hash="h")
        assert ref.heading == ""
        assert ref.section_id == ""
        assert ref.line_start is None

    def test_content_hash_required_by_lint(self):
        ref = SourceRef(source_id="s", relative_path="a.md", content_hash="")
        assert ref.content_hash == ""


class TestPageId:
    def test_product_page_id(self):
        assert is_valid_page_id("product.agent_identity")

    def test_capability_page_id(self):
        assert is_valid_page_id("product.agent_identity.capability.identity_auth")

    def test_concept_page_id(self):
        assert is_valid_page_id("concept.agent_identity_spoofing")

    def test_invalid_type_prefix(self):
        assert not is_valid_page_id("foo.bar")

    def test_traversal_segment_rejected(self):
        assert not is_valid_page_id("product.agent_identity..hack")


class TestSlugify:
    def test_slug(self):
        assert slugify("Agent Identity") == "agent_identity"

    def test_cjk_chars_are_dropped(self):
        # slugify 只保留 [a-z0-9]，CJK 会被剥离
        assert slugify("Agent 身份认证") == "agent"

    def test_empty_falls_back(self):
        assert slugify("!!!") == "untitled"


class TestPathSafety:
    def test_safe_path_ok(self):
        assert is_path_safe("products/agent/overview.md")

    def test_absolute_rejected(self):
        assert not is_path_safe("/etc/passwd")

    def test_traversal_flag(self):
        assert path_traversal("../x.md")

    def test_traversal_rejected(self):
        assert not is_path_safe("products/../../etc/passwd")

    def test_url_encoded_traversal_rejected(self):
        assert not is_path_safe("products/%2e%2e/secret.md")

    def test_safe_page_path_maps_to_products(self, tmp_path):
        target = safe_page_path(tmp_path, "product.agent_identity.capability.identity_auth")
        assert target.name == "identity_auth.md"
        assert "products" in str(target)
        assert "capabilities" in str(target)


class TestExceptions:
    def test_validate_rejects_invalid(self):
        with pytest.raises(PageIdError):
            validate_page_id("bad")

    def test_safe_page_path_rejects_traversal(self, tmp_path):
        # 双点会先被 page_id 段校验拒绝（PageIdError），是 WikiError 基类的子类
        with pytest.raises(WikiError):
            safe_page_path(tmp_path, "concept.agent_identity..x")


class TestRelations:
    def test_relation_contract(self):
        rel = WikiRelation(relation_type="belongs_to", target_page_id="product.x")
        assert rel.relation_type == "belongs_to"
        assert rel.target_page_id == "product.x"
