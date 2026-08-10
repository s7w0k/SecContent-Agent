"""
ContextBridge 单元测试（阶段二 Step 5）

覆盖：
  - 模式决策（off/shadow/active）与灰度分流
  - build_plan 收集 skill_core + required_product + 可选 skill_references
  - resolve_knowledge 三种模式语义与 telemetry（plan_hash/skill_versions/source_ids）
  - 无来源降级、配置键默认值

运行:
    pytest tests/unit/test_context_bridge.py -v
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.context_bridge import (  # noqa: E402
    ContextBridge,
    context_mode,
    user_in_rollout,
)
from agent.skill_registry import SkillRegistry  # noqa: E402
from config import Settings  # noqa: E402


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _write_skill(root, name: str, description: str, extra_refs: dict | None = None):
    """构造一个最小合规 Skill 包（与 test_skill_registry 一致）。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    refs_md = ""
    for rel, content in (extra_refs or {}).items():
        fp = skill_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        refs_md += f"参考：{rel}\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n"
        f"# {name}\n\n主体内容。\n\n{refs_md}",
        encoding="utf-8",
    )
    return skill_dir


def _make_registry(tmp_path) -> SkillRegistry:
    _write_skill(
        tmp_path,
        "scoring-knowledge",
        "用于评分时选择知识资料，不用于对话。",
        extra_refs={"references/scoring-rules.md": "评分维度与注入顺序说明。"},
    )
    _write_skill(
        tmp_path,
        "draft-writing",
        "用于稿件生成，不用于评分。",
        extra_refs={"references/writing-guidelines.md": "写作结构约束与禁用表述。"},
    )
    _write_skill(
        tmp_path,
        "compliance-review",
        "用于合规红线检查，不用于写作。",
    )
    registry = SkillRegistry(skills_root=str(tmp_path))
    registry.load()
    return registry


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = copy.deepcopy(docs)

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length: int = 0):
        return self._docs if not length else self._docs[:length]


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None):
        self._docs = docs or []

    def find(self, query: dict | None = None):
        return FakeCursor(self._docs)

    async def find_one(self, query: dict | None = None):
        return copy.deepcopy(self._docs[0]) if self._docs else None


def _make_db() -> dict[str, FakeCollection]:
    return {
        "user_products": FakeCollection(
            [{"product_id": "prod-1", "name": "星海外部攻击面管理平台"}]
        ),
        "user_knowledge_entries": FakeCollection(
            [
                {
                    "entry_id": "e1",
                    "user_id": "u-1",
                    "product_id": "prod-1",
                    "product_scope": "user",
                    "doc_type": "overview",
                    "title": "产品概述",
                    "content": "该产品用于外部攻击面发现与管理。",
                    "enabled": True,
                    "sort_order": 1,
                    "updated_at": "2026-01-01T00:00:00",
                },
                {
                    "entry_id": "e2",
                    "user_id": "u-1",
                    "product_id": "prod-1",
                    "product_scope": "user",
                    "doc_type": "market-brief",
                    "title": "市场简报",
                    "content": "面向政企客户的安全运营市场。",
                    "enabled": True,
                    "sort_order": 2,
                    "updated_at": "2026-01-01T00:00:00",
                },
            ]
        ),
    }


def _settings(tmp_path, **kw) -> Settings:
    defaults = {
        "KNOWLEDGE_SKILLS_ENABLED": False,
        "KNOWLEDGE_SKILLS_SHADOW_ENABLED": False,
        "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 0,
        "CONTEXT_MAX_INPUT_TOKENS": 0,
        "CONTEXT_CACHE_TTL_SECONDS": 300,
        "CONTEXT_OFFLINE_COMPRESSION_ENABLED": False,
        "KNOWLEDGE_BASE_DIR": str(tmp_path),
        "DEEPSEEK_MODEL": "deepseek-chat",
    }
    defaults.update(kw)
    return Settings(**defaults)


def _bridge(tmp_path, settings_kw: dict | None = None, with_registry: bool = True, db=None):
    settings = _settings(tmp_path, **(settings_kw or {}))
    registry = _make_registry(tmp_path) if with_registry else None
    return ContextBridge(db=db, settings=settings, registry=registry)


# ═══════════════════════════════════════════════════════════════
# 1. 模式决策
# ═══════════════════════════════════════════════════════════════


def test_context_mode_off_shadow_active(tmp_path):
    off = _settings(tmp_path)
    assert context_mode(off) == "off"

    shadow = _settings(
        tmp_path, KNOWLEDGE_SKILLS_ENABLED=True, KNOWLEDGE_SKILLS_SHADOW_ENABLED=True
    )
    assert context_mode(shadow) == "shadow"

    active = _settings(
        tmp_path, KNOWLEDGE_SKILLS_ENABLED=True, KNOWLEDGE_SKILLS_SHADOW_ENABLED=False
    )
    assert context_mode(active) == "active"


def test_user_in_rollout_boundaries_and_determinism():
    assert user_in_rollout("u-1", 0) is False
    assert user_in_rollout("u-1", 100) is True
    # 确定性：同一用户多次结果一致
    assert user_in_rollout("u-1", 30) == user_in_rollout("u-1", 30)
    assert user_in_rollout("u-2", 30) == user_in_rollout("u-2", 30)
    # 部分灰度时不同用户可能分流不同
    bucket = {user_in_rollout(f"u-{i}", 50) for i in range(20)}
    assert bucket <= {True, False}
    assert False in bucket and True in bucket


def test_effective_mode_respects_rollout(tmp_path):
    # active 但灰度 0 → off
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 0,
        },
    )
    assert bridge.mode() == "active"
    assert bridge.effective_mode("u-1") == "off"

    # active + 灰度 100 → active
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100,
        },
    )
    assert bridge.effective_mode("u-1") == "active"


def test_config_keys_defaults(tmp_path):
    settings = _settings(tmp_path)
    assert settings.KNOWLEDGE_SKILLS_ENABLED is False
    assert settings.KNOWLEDGE_SKILLS_SHADOW_ENABLED is False
    assert settings.KNOWLEDGE_SKILLS_ROLLOUT_PERCENT == 0
    assert settings.CONTEXT_MAX_INPUT_TOKENS == 0
    assert settings.CONTEXT_CACHE_TTL_SECONDS == 300
    assert settings.CONTEXT_OFFLINE_COMPRESSION_ENABLED is False


# ═══════════════════════════════════════════════════════════════
# 2. build_plan：sources + telemetry
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_plan_score_collects_skill_and_product(tmp_path):
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100,
        },
        db=_make_db(),
    )
    result = await bridge.build_plan(
        purpose="score", user_id="u-1", products=["prod-1"], model_id="deepseek-chat"
    )
    assert result is not None
    plan = result.plan

    source_ids = [s.source_id for s in plan.sections]
    assert "skill_core:scoring-knowledge" in source_ids
    assert any(sid.startswith("required_product:") for sid in source_ids)
    # skill_core 是 required，必须被分配
    assert all(s.source.required for s in plan.sections if s.source.section_type == "skill_core")

    telemetry = result.telemetry
    assert telemetry["context_plan_hash"].startswith("sha256:")
    assert "scoring-knowledge" in telemetry["skill_versions"]
    assert telemetry["knowledge_snapshot"] != "none"
    assert len(telemetry["source_ids"]) == len(source_ids)
    assert telemetry["budget_tokens"] > 0
    # active 模式内容 = skill 指令 + 产品知识
    assert "评分时选择知识资料" in plan.rendered()
    assert "外部攻击面发现与管理" in plan.rendered()


@pytest.mark.asyncio
async def test_build_plan_draft_includes_optional_references(tmp_path):
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100,
        },
        db=_make_db(),
    )
    result = await bridge.build_plan(
        purpose="draft", user_id="u-1", products=["prod-1"], model_id="deepseek-chat"
    )
    assert result is not None
    source_ids = [s.source_id for s in result.plan.sections]
    assert "skill_core:draft-writing" in source_ids
    assert "skill_core:compliance-review" in source_ids
    assert any(sid.startswith("skill_references:") for sid in source_ids)
    # 可选 references 不强制
    ref_sections = [s for s in result.plan.sections if s.source.section_type == "skill_references"]
    assert all(not s.source.required for s in ref_sections)


@pytest.mark.asyncio
async def test_build_plan_no_sources_returns_none(tmp_path):
    bridge = _bridge(
        tmp_path,
        {"KNOWLEDGE_SKILLS_ENABLED": True, "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100},
        with_registry=False,
        db=None,
    )
    result = await bridge.build_plan(purpose="chat", user_id="", model_id="deepseek-chat")
    assert result is None


# ═══════════════════════════════════════════════════════════════
# 3. resolve_knowledge：off / shadow / active 语义
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resolve_off_returns_none(tmp_path):
    bridge = _bridge(tmp_path, {}, db=_make_db())
    content, telemetry = await bridge.resolve_knowledge(
        purpose="score", user_id="u-1", products=["prod-1"]
    )
    assert content is None
    assert telemetry == {"mode": "off"}


@pytest.mark.asyncio
async def test_resolve_shadow_returns_none_with_telemetry(tmp_path):
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_SHADOW_ENABLED": True,
        },
        db=_make_db(),
    )
    content, telemetry = await bridge.resolve_knowledge(
        purpose="score", user_id="u-1", products=["prod-1"]
    )
    # shadow：内容仍走旧路径（None），但已构建 plan 并记录差异
    assert content is None
    assert telemetry["mode"] == "shadow"
    assert telemetry["context_plan_hash"].startswith("sha256:")
    assert "scoring-knowledge" in telemetry["skill_versions"]
    assert telemetry["knowledge_snapshot"] != "none"
    assert "skill_core:scoring-knowledge" in telemetry["source_ids"]


@pytest.mark.asyncio
async def test_resolve_active_renders_plan_and_telemetry(tmp_path):
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100,
        },
        db=_make_db(),
    )
    content, telemetry = await bridge.resolve_knowledge(
        purpose="score", user_id="u-1", products=["prod-1"]
    )
    assert content is not None
    assert "评分时选择知识资料" in content  # skill_core 指令
    assert "外部攻击面发现与管理" in content  # 产品知识
    assert telemetry["mode"] == "active"
    assert telemetry["context_plan_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_resolve_plan_hash_stable_for_same_input(tmp_path):
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100,
        },
        db=_make_db(),
    )
    _, t1 = await bridge.resolve_knowledge(
        purpose="score", user_id="u-1", products=["prod-1"]
    )
    _, t2 = await bridge.resolve_knowledge(
        purpose="score", user_id="u-1", products=["prod-1"]
    )
    assert t1["context_plan_hash"] == t2["context_plan_hash"]


@pytest.mark.asyncio
async def test_resolve_active_respects_rollout_percent(tmp_path):
    # 灰度 0：即使开启 active 也回退 off
    bridge = _bridge(
        tmp_path,
        {
            "KNOWLEDGE_SKILLS_ENABLED": True,
            "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 0,
        },
        db=_make_db(),
    )
    content, telemetry = await bridge.resolve_knowledge(
        purpose="score", user_id="u-1", products=["prod-1"]
    )
    assert content is None
    assert telemetry == {"mode": "off"}
