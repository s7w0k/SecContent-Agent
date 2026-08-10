"""阶段 0.3 参数化路由测试矩阵。

spec（Agent生产上线优化实施计划-20260810/01-阶段0-基线冻结与工程门禁清零.md）要求：
  所有 Agent flag 关闭 / Chat shadow / Chat 1%/10%/100% /
  Knowledge Skills shadow/active / Multi-Agent current/shadow/planned /
  Autonomous disabled / A2A disabled & internal peer。

每类模式验证：最终执行路径、灰度桶、回退路径、业务写入次数。

阶段 0 基线事实（本矩阵锁定，后续阶段接线时随功能演进更新）：
  - CHAT_AGENT_SHADOW_ENABLED / CHAT_AGENT_ROLLOUT_PERCENT 已定义但未接入 Chat 决策
    （config.py 存在，业务代码未读取）→ Chat 实际只有 legacy / agent 两态；
  - AUTONOMOUS_AGENT_SHADOW_ENABLED / AUTONOMOUS_AGENT_ROLLOUT_PERCENT 同样未接线
    → Autonomous 实际只有 启用 / disabled(503) 两态（internal/rollout 区分尚未实现）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent.context_bridge import ContextBridge, context_mode, user_in_rollout
from agent.draft_chat import DraftChatAgent
from agent.multi_agent import (
    MODE_CURRENT,
    MODE_PLANNED,
    MODE_SHADOW,
    _ShadowCol,
    decide_execution_mode,
)
from pydantic import ValidationError


def _context_settings(**kw) -> SimpleNamespace:
    defaults = {
        "KNOWLEDGE_SKILLS_ENABLED": False,
        "KNOWLEDGE_SKILLS_SHADOW_ENABLED": False,
        "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 0,
        "KNOWLEDGE_BASE_DIR": "/tmp/kb",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _bridge(settings: SimpleNamespace) -> ContextBridge:
    return ContextBridge(settings=settings)


# ═══════════════════════════════════════════════════════════════
# 1. 所有 Agent flag 关闭（基线）
# ═══════════════════════════════════════════════════════════════


def test_all_flags_off_baseline():
    """默认配置下 5 个 Agent 全部关闭，路由全部走 legacy/off/current。"""
    from config import Settings

    s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
    # 开关默认值（与 baseline-manifest.json feature_flags 一致）
    assert s.CHAT_AGENT_ENABLED is False
    assert s.CHAT_AGENT_SHADOW_ENABLED is False
    assert s.CHAT_AGENT_ROLLOUT_PERCENT == 0
    assert s.KNOWLEDGE_SKILLS_ENABLED is False
    assert s.MULTI_AGENT_ENABLED is False
    assert s.AUTONOMOUS_AGENT_ENABLED is False
    assert s.A2A_ENABLED is False
    assert s.A2A_CLIENT_ENABLED is False
    assert s.WEB_SEARCH_ENABLED is False
    # 路由结果
    assert (
        decide_execution_mode(enabled=False, shadow_enabled=True, rollout_percent=100, user_id="u1")
        == MODE_CURRENT
    )
    assert context_mode(s) == "off"
    assert _bridge(s).effective_mode("u1") == "off"


# ═══════════════════════════════════════════════════════════════
# 2. 灰度桶矩阵（Chat 1%/10%/100% 的确定性分桶基础）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("percent", [0, 1, 10, 100])
def test_rollout_bucket_distribution(percent):
    """灰度桶命中率与 percent 一致（sha256 确定性分桶）。"""
    users = [f"u-{i}" for i in range(400)]
    hits = sum(user_in_rollout(u, percent) for u in users)
    if percent == 0:
        assert hits == 0
    elif percent == 100:
        assert hits == 400
    elif percent == 1:  # 期望 ≈4，±3σ 内允许 0~12
        assert 0 <= hits <= 12
    elif percent == 10:  # 期望 ≈40，±3σ 内允许 20~60
        assert 20 <= hits <= 60


def test_rollout_bucket_deterministic_and_consistent_with_multi_agent():
    """同一 user 同一 percent 分桶稳定，且与 Multi-Agent 决策分桶一致。"""
    for uid in ["u-1", "u-2", "u-3"]:
        assert user_in_rollout(uid, 30) == user_in_rollout(uid, 30)
        expected = MODE_PLANNED if user_in_rollout(uid, 30) else MODE_CURRENT
        assert (
            decide_execution_mode(
                enabled=True, shadow_enabled=False, rollout_percent=30, user_id=uid
            )
            == expected
        )


# ═══════════════════════════════════════════════════════════════
# 3. Multi-Agent：current / shadow / planned + 业务写入次数
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("enabled", "shadow", "rollout", "expected"),
    [
        (False, True, 100, MODE_CURRENT),  # 总开关关 → current（灰度/影子不生效）
        (True, True, 0, MODE_SHADOW),  # shadow 优先于灰度
        (True, False, 100, MODE_PLANNED),  # 灰度 100% → planned
        (True, False, 0, MODE_CURRENT),  # 灰度 0% → current
    ],
)
def test_multi_agent_mode_matrix(enabled, shadow, rollout, expected):
    assert (
        decide_execution_mode(
            enabled=enabled, shadow_enabled=shadow, rollout_percent=rollout, user_id="u1"
        )
        == expected
    )


@pytest.mark.asyncio
async def test_multi_agent_shadow_zero_business_writes():
    """shadow 模式业务写入次数 = 0：写操作被 _ShadowCol 拦截，仅记差异日志。"""
    real = MagicMock()
    real.insert_one = AsyncMock()
    log_col = MagicMock()
    log_col.insert_one = AsyncMock()

    shadow = _ShadowCol(real, "articles", log_col)
    result = await shadow.insert_one({"_id": 1, "title": "x"})

    assert result.acknowledged is True
    real.insert_one.assert_not_awaited()  # 正式集合零写入
    log_col.insert_one.assert_awaited_once()  # 差异日志写入


# ═══════════════════════════════════════════════════════════════
# 4. Knowledge Skills：off / shadow / active + 灰度回退
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("cfg", "user", "expected"),
    [
        # 总开关关 → off
        ({}, "u1", "off"),
        # shadow 优先
        (
            {"KNOWLEDGE_SKILLS_ENABLED": True, "KNOWLEDGE_SKILLS_SHADOW_ENABLED": True},
            "u1",
            "shadow",
        ),
        # active + 灰度 100% → active
        (
            {"KNOWLEDGE_SKILLS_ENABLED": True, "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 100},
            "u1",
            "active",
        ),
        # active + 灰度 0% → 回退 off（legacy 路径）
        (
            {"KNOWLEDGE_SKILLS_ENABLED": True, "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 0},
            "u1",
            "off",
        ),
        # shadow + 灰度 0% → shadow（灰度只影响 active）
        (
            {
                "KNOWLEDGE_SKILLS_ENABLED": True,
                "KNOWLEDGE_SKILLS_SHADOW_ENABLED": True,
                "KNOWLEDGE_SKILLS_ROLLOUT_PERCENT": 0,
            },
            "u1",
            "shadow",
        ),
    ],
)
def test_context_effective_mode_matrix(cfg, user, expected):
    assert _bridge(_context_settings(**cfg)).effective_mode(user) == expected


def test_context_shadow_no_business_write():
    """shadow 模式 resolve 返回 (None, telemetry)：调用方走旧知识路径，不落库。"""
    bridge = _bridge(
        _context_settings(KNOWLEDGE_SKILLS_ENABLED=True, KNOWLEDGE_SKILLS_SHADOW_ENABLED=True)
    )
    bridge._registry = MagicMock()
    assert bridge.mode() == "shadow"


# ═══════════════════════════════════════════════════════════════
# 5. Chat：legacy / agent 双轨（含 shadow/rollout 未接线基线）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("enabled", "ask", "shadow", "rollout", "has_run_ctx", "expected"),
    [
        (False, False, False, 0, True, "legacy"),  # 总开关关
        (True, False, False, 0, True, "legacy"),  # ask 子开关关
        (True, True, False, 0, True, "agent"),  # 双开 → agent
        (True, True, False, 100, True, "agent"),  # rollout 未接线 → 仍 agent（基线事实）
        (True, True, True, 100, True, "agent"),  # shadow 未接线 → 仍 agent（基线事实）
        (True, True, False, 0, False, "legacy"),  # 无 run_context → legacy
    ],
)
@pytest.mark.asyncio
async def test_chat_dual_track_routing(
    monkeypatch, enabled, ask, shadow, rollout, has_run_ctx, expected
):
    fake_settings = SimpleNamespace(
        CHAT_AGENT_ENABLED=enabled,
        CHAT_ASK_AGENT_ENABLED=ask,
        CHAT_AGENT_SHADOW_ENABLED=shadow,
        CHAT_AGENT_ROLLOUT_PERCENT=rollout,
    )
    monkeypatch.setattr("config.get_settings", lambda: fake_settings)

    agent_path = AsyncMock(return_value={"answer": "a", "agent_mode": True, "run_id": "r1"})
    legacy_path = AsyncMock(return_value={"answer": "a", "references": []})
    monkeypatch.setattr(DraftChatAgent, "_answer_agent", agent_path)
    monkeypatch.setattr(DraftChatAgent, "_answer_legacy", legacy_path)

    chat = DraftChatAgent(llm=MagicMock(), knowledge_loader=MagicMock(), llm_wrapper=MagicMock())
    result = await chat.answer(message="hi", run_context=MagicMock() if has_run_ctx else None)

    if expected == "agent":
        agent_path.assert_awaited_once()
        legacy_path.assert_not_awaited()
        assert result.get("agent_mode") is True
    else:
        legacy_path.assert_awaited_once()
        agent_path.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════
# 6. Autonomous：disabled（503 语义）+ 配置强校验
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "field",
    [
        "AUTONOMOUS_MAX_STEPS",
        "AUTONOMOUS_MAX_RUNTIME_SECONDS",
        "AUTONOMOUS_MAX_INPUT_TOKENS",
        "AUTONOMOUS_MAX_OUTPUT_TOKENS",
        "AUTONOMOUS_MAX_TOOL_CALLS",
        "AUTONOMOUS_MAX_RETRIES",
        "AUTONOMOUS_MAX_CONSECUTIVE_FAILURES",
    ],
)
def test_autonomous_enabled_requires_positive_budget(field):
    """总开关开启时任一预算为 0 即拒绝启动（配置强校验，防无上限运行）。"""
    from config import Settings

    with pytest.raises(ValidationError):
        Settings(
            DEEPSEEK_API_KEY="test",
            _env_file=None,
            AUTONOMOUS_AGENT_ENABLED=True,
            **{field: 0},
        )


def test_autonomous_disabled_defaults_to_503_service_unavailable():
    """disabled 默认值下服务不初始化（API 层返回 503），shadow/rollout 未接线。"""
    from config import Settings

    s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
    assert s.AUTONOMOUS_AGENT_ENABLED is False
    assert s.AUTONOMOUS_AGENT_SHADOW_ENABLED is False
    assert s.AUTONOMOUS_AGENT_ROLLOUT_PERCENT == 0


# ═══════════════════════════════════════════════════════════════
# 7. A2A：disabled / internal peer
# ═══════════════════════════════════════════════════════════════


def test_a2a_disabled_baseline():
    """A2A server/client 默认关闭，外部 peer 允许列表为空。"""
    from config import Settings

    s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
    assert s.A2A_ENABLED is False
    assert s.A2A_CLIENT_ENABLED is False
    assert s.A2A_ALLOWED_PEERS == []


def test_a2a_requires_autonomous_enabled_matrix():
    """A2A 复用自主运行服务：不允许单独暴露。"""
    from config import Settings

    with pytest.raises(ValidationError):
        Settings(
            DEEPSEEK_API_KEY="test",
            _env_file=None,
            A2A_ENABLED=True,
            AUTONOMOUS_AGENT_ENABLED=False,
        )


def test_a2a_internal_peer_loopback():
    """internal peer：允许列表为空 = 仅本机闭环（无外部 peer）。"""
    from config import Settings

    s = Settings(
        DEEPSEEK_API_KEY="test",
        _env_file=None,
        A2A_ENABLED=True,
        AUTONOMOUS_AGENT_ENABLED=True,
        A2A_ALLOWED_PEERS=[],
    )
    assert s.A2A_ALLOWED_PEERS == []
    assert s.A2A_CLIENT_ENABLED is False
