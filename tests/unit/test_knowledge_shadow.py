"""
知识检索 Shadow 遥测与灰度（阶段七）单元测试

覆盖：
  - 模式决策 off/shadow/active（knowledge_retrieval_mode）
  - 灰度分流（user_in_retrieval_rollout）与 effective_retrieval_mode
  - shadow 差异记录（RetrievalShadowDiff / RetrievalShadowTracker）
  - 停止条件评估（evaluate_stop_conditions）

运行:
    pytest tests/unit/test_knowledge_shadow.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.knowledge_shadow import (
    RetrievalShadowDiff,
    RetrievalShadowTracker,
    effective_retrieval_mode,
    evaluate_stop_conditions,
    knowledge_retrieval_mode,
    record_retrieval_shadow,
    user_in_retrieval_rollout,
)
from config import Settings


def _settings(**kw) -> Settings:
    defaults = {
        "KNOWLEDGE_RETRIEVAL_ENABLED": False,
        "KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED": True,
        "KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT": 0,
        "KNOWLEDGE_INDEX_VERSION": "v1",
    }
    defaults.update(kw)
    return Settings(**defaults)


# ═══════════════════════════════════════════════════════════════
# 模式决策
# ═══════════════════════════════════════════════════════════════


def test_retrieval_mode_off_when_disabled():
    s = _settings(KNOWLEDGE_RETRIEVAL_ENABLED=False)
    assert knowledge_retrieval_mode(s) == "off"


def test_retrieval_mode_shadow_when_shadow_enabled():
    s = _settings(
        KNOWLEDGE_RETRIEVAL_ENABLED=True,
        KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED=True,
    )
    assert knowledge_retrieval_mode(s) == "shadow"


def test_retrieval_mode_active_when_not_shadow():
    s = _settings(
        KNOWLEDGE_RETRIEVAL_ENABLED=True,
        KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED=False,
    )
    assert knowledge_retrieval_mode(s) == "active"


def test_retrieval_mode_shadow_defaults_to_on_when_enabled():
    # 默认 SHADOW_ENABLED=True，仅开总开关即进入 shadow（安全首期）
    s = _settings(KNOWLEDGE_RETRIEVAL_ENABLED=True)
    assert knowledge_retrieval_mode(s) == "shadow"


# ═══════════════════════════════════════════════════════════════
# 灰度分流
# ═══════════════════════════════════════════════════════════════


def test_rollout_percent_bounds():
    assert user_in_retrieval_rollout("u1", 0) is False
    assert user_in_retrieval_rollout("u1", 100) is True


def test_rollout_percent_deterministic():
    assert user_in_retrieval_rollout("u1", 50) == user_in_retrieval_rollout("u1", 50)
    assert user_in_retrieval_rollout("u1", 50) == user_in_retrieval_rollout("u1", 50)


def test_effective_off_kept_when_disabled():
    s = _settings(KNOWLEDGE_RETRIEVAL_ENABLED=False, KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT=100)
    assert effective_retrieval_mode(s, "u1") == "off"


def test_effective_shadow_not_affected_by_rollout():
    # shadow 不参与灰度：始终 shadow
    s = _settings(
        KNOWLEDGE_RETRIEVAL_ENABLED=True,
        KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED=True,
        KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT=0,
    )
    assert effective_retrieval_mode(s, "u1") == "shadow"


def test_effective_active_rollout_hit_and_miss():
    # active 且灰度 100：命中 → active
    hit = _settings(
        KNOWLEDGE_RETRIEVAL_ENABLED=True,
        KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED=False,
        KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT=100,
    )
    assert effective_retrieval_mode(hit, "u1") == "active"

    # active 且灰度 0：未命中 → off
    miss = _settings(
        KNOWLEDGE_RETRIEVAL_ENABLED=True,
        KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED=False,
        KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT=0,
    )
    assert effective_retrieval_mode(miss, "u1") == "off"


# ═══════════════════════════════════════════════════════════════
# shadow 差异记录
# ═══════════════════════════════════════════════════════════════


def test_shadow_diff_char_delta_and_required_lost():
    diff = RetrievalShadowDiff(
        old_char_count=1000,
        new_char_count=600,
        required_missing_old=["a"],
        required_missing_new=["a", "b"],
    )
    assert diff.char_delta == -400
    assert diff.required_lost == ["b"]


def test_shadow_diff_no_required_lost_when_subset():
    diff = RetrievalShadowDiff(
        required_missing_old=["a", "b"],
        required_missing_new=["a"],
    )
    assert diff.required_lost == []


def test_tracker_records_latency_and_fields():
    tracker = RetrievalShadowTracker(
        purpose="draft", user_id="u1", query="错误码", index_version="v1"
    )
    diff = tracker.finish(
        old_char_count=1000,
        new_char_count=800,
        old_source_docs=["doc_old"],
        new_source_docs=["doc_new"],
        required_missing_old=[],
        required_missing_new=[],
        new_index_version="v1",
    )
    assert diff.purpose == "draft"
    assert diff.query == "错误码"
    assert diff.old_source_docs == ["doc_old"]
    assert diff.new_source_docs == ["doc_new"]
    assert diff.new_index_version == "v1"
    assert diff.latency_ms >= 0.0


def test_record_retrieval_shadow_no_raise():
    diff = RetrievalShadowDiff(
        purpose="draft",
        user_id="u1",
        index_version="v1",
        old_char_count=1000,
        new_char_count=800,
    )
    record_retrieval_shadow(diff)  # 不应抛异常


# ═══════════════════════════════════════════════════════════════
# 停止条件
# ═══════════════════════════════════════════════════════════════


def test_stop_no_conditions_when_clean():
    diff = RetrievalShadowDiff(
        old_char_count=1000,
        new_char_count=800,
        required_missing_old=[],
        required_missing_new=[],
        index_version="v1",
        new_index_version="v1",
    )
    assert evaluate_stop_conditions(diff) == []


def test_stop_required_lost():
    diff = RetrievalShadowDiff(
        required_missing_old=["a"],
        required_missing_new=["a", "b"],
    )
    conds = evaluate_stop_conditions(diff)
    assert any("required_docs_lost" in c for c in conds)


def test_stop_index_version_mismatch():
    diff = RetrievalShadowDiff(
        index_version="v1",
        new_index_version="v2",
    )
    conds = evaluate_stop_conditions(diff)
    assert any("index_version_mismatch" in c for c in conds)


def test_stop_no_version_mismatch_when_present():
    diff = RetrievalShadowDiff(
        index_version="v1",
        new_index_version="v1",
    )
    assert any("index_version_mismatch" in c for c in evaluate_stop_conditions(diff)) is False


def test_stop_new_chain_error():
    diff = RetrievalShadowDiff(error="boom")
    conds = evaluate_stop_conditions(diff)
    assert any("new_chain_error" in c for c in conds)
