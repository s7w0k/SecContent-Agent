"""
ContextCache 单元测试（阶段二 Step 6）

覆盖：
  - 缓存键 hash 稳定性与全分量敏感
  - get/set、TTL 兜底过期
  - user namespace 物理隔离与读取后断言
  - single-flight 并发构建只执行一次
  - 主动失效（按用户 / purpose）
  - 事件日志仅存 key hash/status
  - ContextBridge + cache 集成：命中/未命中/版本变化自动失效/跨用户隔离

运行:
    pytest tests/unit/test_context_cache.py -v
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.context_cache import ContextCache, ContextCacheKey
from config import Settings

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _key(**kw) -> ContextCacheKey:
    base = {
        "user_id": "u-1",
        "purpose": "score",
        "product_ids": ("prod-1",),
        "query_hash": "h",
        "model_id": "deepseek-chat",
        "token_budget": 0,
        "skill_snapshot_hash": "s1",
        "knowledge_snapshot": "k1",
        "memory_version": "none",
    }
    base.update(kw)
    return ContextCacheKey(**base)


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = copy.deepcopy(docs)

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length: int = 0):
        return self._docs if not length else self._docs[:length]


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None):
        self._docs = docs or []

    def find(self, query: dict | None = None):
        if not query:
            return FakeCursor(self._docs)
        return FakeCursor([d for d in self._docs if _matches(d, query)])

    async def find_one(self, query: dict | None = None):
        for doc in self._docs:
            if _matches(doc, query or {}):
                return copy.deepcopy(doc)
        return None


def _make_db(entries=None) -> dict[str, FakeCollection]:
    return {
        "user_products": FakeCollection(
            [{"product_id": "prod-1", "name": "星海外部攻击面管理平台"}]
        ),
        "user_knowledge_entries": FakeCollection(
            entries
            if entries is not None
            else [
                {
                    "entry_id": "e1",
                    "user_id": "u-1",
                    "product_id": "prod-1",
                    "product_scope": "user",
                    "doc_type": "overview",
                    "title": "产品概述",
                    "content": "该产品用于外部攻击面发现与管理。",
                    "content_hash": "ch1",
                    "enabled": True,
                    "sort_order": 1,
                },
            ]
        ),
    }


def _write_skill(root, name: str, description: str):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n"
        f"# {name}\n\n主体内容。\n",
        encoding="utf-8",
    )


def _make_registry(tmp_path):
    from agent.skill_registry import SkillRegistry

    _write_skill(tmp_path, "scoring-knowledge", "用于评分时选择知识资料，不用于对话。")
    _write_skill(tmp_path, "draft-writing", "用于稿件生成，不用于评分。")
    _write_skill(tmp_path, "compliance-review", "用于合规红线检查，不用于写作。")
    registry = SkillRegistry(skills_root=str(tmp_path))
    registry.load()
    return registry


def _settings(tmp_path, **kw) -> Settings:
    defaults = {
        "KNOWLEDGE_SKILLS_ENABLED": True,
        "KNOWLEDGE_SKILLS_SHADOW_ENABLED": False,
        "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100,
        "CONTEXT_MAX_INPUT_TOKENS": 0,
        "CONTEXT_CACHE_TTL_SECONDS": 300,
        "CONTEXT_OFFLINE_COMPRESSION_ENABLED": False,
        "KNOWLEDGE_BASE_DIR": str(tmp_path),
        "DEEPSEEK_MODEL": "deepseek-chat",
    }
    defaults.update(kw)
    return Settings(**defaults)


def _bridge(tmp_path, cache: ContextCache, db=None):
    from agent.context_bridge import ContextBridge

    return ContextBridge(
        db=db,
        settings=_settings(tmp_path),
        registry=_make_registry(tmp_path),
        cache=cache,
    )


# ═══════════════════════════════════════════════════════════════
# 1. 缓存键
# ═══════════════════════════════════════════════════════════════


def test_key_hash_stable_and_component_sensitive():
    k = _key()
    assert k.key_hash == _key().key_hash

    sensitive_fields = [
        ("user_id", "u-2"),
        ("purpose", "draft"),
        ("product_ids", ("prod-2",)),
        ("query_hash", "h2"),
        ("model_id", "gpt-4o"),
        ("token_budget", 1000),
        ("skill_snapshot_hash", "s2"),
        ("knowledge_snapshot", "k2"),
        ("memory_version", "memory:v1"),
    ]
    for field, value in sensitive_fields:
        assert _key(**{field: value}).key_hash != k.key_hash, f"{field} 未影响 key hash"
    # 无重复 key
    hashes = {_key(**{f: v}).key_hash for f, v in sensitive_fields}
    assert len(hashes) == len(sensitive_fields)


# ═══════════════════════════════════════════════════════════════
# 2. get / set / TTL
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_set_get_roundtrip():
    cache = ContextCache(ttl_seconds=300)
    key = _key()
    plan = {"content": "x", "tokens": 10}
    await cache.set(key, plan)
    assert await cache.get(key) is plan


@pytest.mark.asyncio
async def test_ttl_expiry():
    cache = ContextCache(ttl_seconds=0.05)
    key = _key()
    await cache.set(key, {"content": "x"})
    assert await cache.get(key) is not None
    await asyncio.sleep(0.08)
    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_user_namespace_isolation():
    cache = ContextCache(ttl_seconds=300)
    await cache.set(_key(user_id="u-1"), {"owner": "u-1"})
    await cache.set(_key(user_id="u-2"), {"owner": "u-2"})
    assert (await cache.get(_key(user_id="u-1")))["owner"] == "u-1"
    assert (await cache.get(_key(user_id="u-2")))["owner"] == "u-2"
    # 读取后断言：篡改 user_id 不得命中他人条目
    assert await cache.get(_key(user_id="u-3")) is None


# ═══════════════════════════════════════════════════════════════
# 3. single-flight
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_single_flight_builds_once():
    cache = ContextCache(ttl_seconds=300)
    key = _key()
    build_count = 0

    async def builder():
        nonlocal build_count
        build_count += 1
        await asyncio.sleep(0.02)
        return {"content": "built", "n": build_count}

    results = await asyncio.gather(*(cache.get_or_build(key, builder) for _ in range(5)))
    statuses = {status for _, status in results}
    assert statuses == {"hit", "built"}
    assert build_count == 1
    plans = {id(plan) for plan, _ in results}
    assert len(plans) == 1  # 并发请求共享同一 plan


# ═══════════════════════════════════════════════════════════════
# 4. 主动失效
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_invalidate_by_user_and_purpose():
    cache = ContextCache(ttl_seconds=300)
    await cache.set(_key(user_id="u-1", purpose="score"), {"a": 1})
    await cache.set(_key(user_id="u-1", purpose="draft"), {"b": 1})
    await cache.set(_key(user_id="u-2", purpose="score"), {"c": 1})

    removed = await cache.invalidate(user_id="u-1", purpose="score")
    assert removed == 1
    assert await cache.get(_key(user_id="u-1", purpose="score")) is None
    assert await cache.get(_key(user_id="u-1", purpose="draft")) is not None
    assert await cache.get(_key(user_id="u-2", purpose="score")) is not None

    removed = await cache.invalidate(user_id="u-2")
    assert removed == 1
    assert await cache.get(_key(user_id="u-2", purpose="score")) is None


# ═══════════════════════════════════════════════════════════════
# 5. 事件日志（仅 hash/status）
# ═══════════════════════════════════════════════════════════════


def test_events_only_hashes_and_status():
    cache = ContextCache(ttl_seconds=300)
    cache.record(_key().key_hash, "hit", "active")
    cache.record(_key(user_id="u-2").key_hash, "miss", "shadow")
    events = cache.recent_events()
    assert len(events) == 2
    for event in events:
        assert set(event) == {"key_hash", "status", "mode"}
        assert event["key_hash"].startswith("sha256:")
        assert event["status"] in {"hit", "miss", "built", "invalidate"}
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1


# ═══════════════════════════════════════════════════════════════
# 6. ContextBridge + cache 集成
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bridge_cache_hit_on_second_build(tmp_path):
    cache = ContextCache(ttl_seconds=300)
    bridge = _bridge(tmp_path, cache, db=_make_db())
    r1 = await bridge.build_plan(purpose="score", user_id="u-1", products=["prod-1"])
    r2 = await bridge.build_plan(purpose="score", user_id="u-1", products=["prod-1"])
    assert r1 is not None and r2 is not None
    assert r1.plan.plan_hash == r2.plan.plan_hash
    assert r1.telemetry["cache"] == "built"
    assert r2.telemetry["cache"] == "hit"
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1
    # 事件只含 hash/status
    for event in cache.recent_events():
        assert set(event) == {"key_hash", "status", "mode"}


@pytest.mark.asyncio
async def test_bridge_cache_auto_invalidate_on_knowledge_change(tmp_path):
    cache = ContextCache(ttl_seconds=300)
    db1 = _make_db()
    bridge1 = _bridge(tmp_path, cache, db=db1)
    r1 = await bridge1.build_plan(purpose="score", user_id="u-1", products=["prod-1"])
    assert r1 is not None
    assert r1.telemetry["cache"] == "built"

    # 用户知识启停（enabled 翻转）→ 指纹变化 → 新键 → 未命中
    db2 = _make_db()
    db2["user_knowledge_entries"]._docs[0]["enabled"] = False
    bridge2 = _bridge(tmp_path, cache, db=db2)
    r2 = await bridge2.build_plan(purpose="score", user_id="u-1", products=["prod-1"])
    assert r2 is not None
    assert r2.telemetry["cache"] == "built"
    assert r2.plan.plan_hash != r1.plan.plan_hash


@pytest.mark.asyncio
async def test_bridge_cache_cross_user_isolation(tmp_path):
    cache = ContextCache(ttl_seconds=300)
    bridge = _bridge(tmp_path, cache, db=_make_db())
    r1 = await bridge.build_plan(purpose="score", user_id="u-1", products=["prod-1"])
    r2 = await bridge.build_plan(purpose="score", user_id="u-2", products=["prod-1"])
    assert r1 is not None and r2 is not None
    # 不同用户不得共享缓存条目
    assert r2.telemetry["cache"] == "built"
    assert r1.plan.plan_hash != r2.plan.plan_hash
    # u-1 的条目未被 u-2 读取
    r3 = await bridge.build_plan(purpose="score", user_id="u-1", products=["prod-1"])
    assert r3.telemetry["cache"] == "hit"
