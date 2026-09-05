"""阶段0 S0-2：产品路由契约测试。

覆盖：
  1. 路由快照 JSON 序列化稳定（可往返、字段完整）
  2. routing_version 输入相同则一致、顺序无关
  3. mode 与产品列表约束正确（selected 必须非空 / none 必须为空）
  4. 旧任务缺少新字段时可兼容读取（默认值兜底）
"""

from __future__ import annotations

import pytest
from models.article_assessment import ProductSnapshot
from models.generation_config import (
    PipelineConfigSnapshot,
    ProductRoutingSnapshot,
    ResolvedProduct,
    build_routing_snapshot,
    compute_routing_version,
)
from pydantic import ValidationError


def _snapshot(products=None, mode="auto"):
    return build_routing_snapshot(
        mode=mode,
        resolved_products=products
        or [
            ResolvedProduct(
                product_id="ai-bom",
                product_name="AI-BOM",
                match_score=80,
                match_reason="命中关键词",
                match_source="rule",
            )
        ],
    )


class TestRoutingSerialization:
    def test_snapshot_roundtrip(self):
        """快照序列化后反序列化应保持字段完整。"""
        snap = _snapshot()
        data = snap.model_dump_json()
        restored = ProductRoutingSnapshot.model_validate_json(data)
        assert restored.mode == "auto"
        assert restored.product_ids == ["ai-bom"]
        assert restored.routing_version == snap.routing_version
        assert restored.resolved_products[0].match_source == "rule"

    def test_nested_in_pipeline_snapshot_roundtrip(self):
        """路由快照作为 PipelineConfigSnapshot 字段可序列化往返。"""
        snap = _snapshot()
        cfg = PipelineConfigSnapshot(
            product_target_mode="auto",
            resolved_products=[{"product_id": "ai-bom", "product_name": "AI-BOM"}],
            routing=snap,
            routing_version=snap.routing_version,
        )
        restored = PipelineConfigSnapshot.model_validate_json(cfg.model_dump_json())
        assert restored.routing is not None
        assert restored.routing.product_ids == ["ai-bom"]
        assert restored.routing_version
        assert restored.resolved_products[0]["product_id"] == "ai-bom"

    def test_serialization_is_stable(self):
        """相同对象两次序列化得到相同 JSON 语义。"""
        import json

        snap = _snapshot()
        a = json.loads(snap.model_dump_json())
        b = json.loads(snap.model_dump_json())
        assert a == b

    def test_product_ids_property(self):
        snap = _snapshot()
        assert snap.product_ids == ["ai-bom"]
        assert snap.product_ids == [p.product_id for p in snap.resolved_products]


class TestRoutingVersionDeterminism:
    def test_same_input_same_version(self):
        v1 = compute_routing_version(mode="auto", product_ids=["ai-bom", "agent-security"])
        v2 = compute_routing_version(mode="auto", product_ids=["ai-bom", "agent-security"])
        assert v1 == v2
        assert v1.startswith("sha256:")

    def test_order_insensitive(self):
        v1 = compute_routing_version(mode="auto", product_ids=["ai-bom", "agent-security"])
        v2 = compute_routing_version(mode="auto", product_ids=["agent-security", "ai-bom"])
        assert v1 == v2

    def test_different_input_different_version(self):
        v1 = compute_routing_version(mode="auto", product_ids=["ai-bom"])
        v2 = compute_routing_version(mode="auto", product_ids=["agent-security"])
        assert v1 != v2

    def test_snapshot_version_matches_compute(self):
        snap = _snapshot()
        expected = compute_routing_version(mode="auto", product_ids=["ai-bom"])
        assert snap.routing_version == expected


class TestModeConstraints:
    def test_selected_requires_product(self):
        with pytest.raises(ValidationError):
            ProductRoutingSnapshot(mode="selected", resolved_products=[])

    def test_none_forbids_product(self):
        with pytest.raises(ValidationError):
            ProductRoutingSnapshot(
                mode="none",
                resolved_products=[ResolvedProduct(product_id="ai-bom")],
            )

    def test_auto_allows_empty(self):
        snap = ProductRoutingSnapshot(mode="auto", resolved_products=[])
        assert snap.product_ids == []

    def test_none_allows_empty(self):
        snap = ProductRoutingSnapshot(mode="none", resolved_products=[])
        assert snap.mode == "none"

    def test_build_routing_snapshot_selected(self):
        snap = build_routing_snapshot(
            mode="selected",
            resolved_products=[ResolvedProduct(product_id="ai-bom", match_source="user_selected")],
        )
        assert snap.product_ids == ["ai-bom"]
        assert snap.routing_version.startswith("sha256:")


class TestLegacyCompatibility:
    def test_pipeline_snapshot_without_routing_field(self):
        """旧任务缺少 routing 字段时使用默认值 None，兼容读取。"""
        raw = {
            "schema_version": 1,
            "product_target_mode": "auto",
            "selected_product_ids": ["ai-bom"],
            "resolved_products": [{"product_id": "ai-bom", "product_name": "AI-BOM"}],
        }
        cfg = PipelineConfigSnapshot.model_validate(raw)
        assert cfg.routing is None
        assert cfg.routing_version == ""
        assert cfg.knowledge_source_ids == []
        assert cfg.knowledge_fallback is None
        assert cfg.selected_product_ids == ["ai-bom"]

    def test_user_assessment_snapshot_without_routing(self):
        """用户级评分快照缺少 routing 字段时兼容读取。"""
        raw = {"mode": "auto", "knowledge_hash": "sha256:none"}
        ps = ProductSnapshot.model_validate(raw)
        assert ps.routing is None

    def test_user_assessment_snapshot_with_routing(self):
        """用户级评分快照可保存路由快照副本。"""
        snap = _snapshot(
            mode="selected",
            products=[ResolvedProduct(product_id="ai-bom", match_source="user_selected")],
        )
        ps = ProductSnapshot(mode="selected", routing=snap)
        restored = ProductSnapshot.model_validate_json(ps.model_dump_json())
        assert restored.routing is not None
        assert restored.routing.product_ids == ["ai-bom"]
        assert restored.routing.routing_version == snap.routing_version
