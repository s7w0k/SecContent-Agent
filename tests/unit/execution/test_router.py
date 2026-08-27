"""ExecutionRouter 四模式路由 + Sticky / 无隐式回退不变量（计划 §28-32 / §102）。

验收：
  - legacy / skill_canary / skill_planned 命中对应 engine
  - skill_shadow 模式走 ShadowExecutor（legacy 权威）
  - Retry（selected_engine 已固化）复用同一 engine，不重新 rollout
  - Skill 失败绝不隐式回退 Legacy（§104）
  - resume 读取任务创建时的 engine，不重新 rollout（§32 / §65）
"""

from __future__ import annotations

import pytest
from agent.execution.contracts import (
    ExecutionRequest,
    ExecutionResult,
    WorkflowExecutor,
)
from agent.execution.errors import EngineNotConfigured, UnsupportedExecutionMode
from agent.execution.metrics import ExecutionMetrics
from agent.execution.rollout import LevelCanaryRollout
from agent.execution.router import ExecutionRouter
from agent.execution.shadow_executor import ShadowExecutor


class _Fake(WorkflowExecutor):
    """可编程 WorkflowExecutor 桩，记录调用。"""

    def __init__(self, result: ExecutionResult | None = None, *, engine: str) -> None:
        self.result = result or ExecutionResult(engine=engine)
        self.calls: list[str] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append("execute")
        return self.result

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append("resume")
        return self.result


def _req(**kw) -> ExecutionRequest:
    defaults = {"task_id": "t1", "task_type": "run-v2", "tenant_id": "ten", "user_id": "usr"}
    defaults.update(kw)
    return ExecutionRequest(**defaults)


def _router(mode: str, *, legacy, skill, shadow=None, percent: int = 100) -> ExecutionRouter:
    return ExecutionRouter(
        mode=mode,
        legacy=legacy,
        skill=skill,
        shadow=shadow,
        rollout=LevelCanaryRollout(percent=percent, seed="s") if percent else None,
        metrics=ExecutionMetrics(),
    )


class TestModeSelection:
    async def test_legacy_mode_uses_legacy(self) -> None:
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        router = _router("legacy", legacy=legacy, skill=skill)
        result = await router.execute(_req())
        assert result.engine == "legacy"
        assert legacy.calls == ["execute"]

    async def test_skill_planned_mode_uses_skill(self) -> None:
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        router = _router("skill_planned", legacy=legacy, skill=skill)
        result = await router.execute(_req())
        assert result.engine == "skill_planned"
        assert skill.calls == ["execute"]

    async def test_skill_canary_100_percent_uses_skill(self) -> None:
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        router = _router("skill_canary", legacy=legacy, skill=skill, percent=100)
        result = await router.execute(_req())
        assert result.engine == "skill_planned"

    async def test_skill_canary_0_percent_uses_legacy(self) -> None:
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        router = _router("skill_canary", legacy=legacy, skill=skill, percent=0)
        result = await router.execute(_req())
        assert result.engine == "legacy"

    async def test_skill_shadow_returns_legacy_primary(self) -> None:
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        shadow = ShadowExecutor(legacy=legacy, skill=skill)
        router = _router("skill_shadow", legacy=legacy, skill=skill, shadow=shadow)
        result = await router.execute(_req())
        assert result.engine == "legacy"
        assert legacy.calls == ["execute"]

    async def test_unsupported_mode_raises_at_construction(self) -> None:
        with pytest.raises(UnsupportedExecutionMode):
            _router("bogus", legacy=None, skill=None)

    async def test_missing_engine_raises_engine_not_configured(self) -> None:
        router = _router("skill_planned", legacy=_Fake(engine="legacy"), skill=None)
        with pytest.raises(EngineNotConfigured):
            await router.execute(_req())


class _FailAlways(_Fake):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("skill boom")


class TestStickyAndNoFallback:
    async def test_skill_failure_does_not_fallback_legacy(self) -> None:
        """skill_planned 模式：skill 抛错必须向上传播，不得回退 legacy（§104）。"""
        legacy = _Fake(engine="legacy")
        skill = _FailAlways(engine="skill_planned")
        router = _router("skill_planned", legacy=legacy, skill=skill)
        with pytest.raises(RuntimeError, match="skill boom"):
            await router.execute(_req())
        assert legacy.calls == []  # 关键：skill 失败不触达 legacy

    async def test_retry_reuses_selected_engine(self) -> None:
        """selected_engine 固化后，Retry 复用同一 engine（§31-32）。"""
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        router = _router("skill_canary", legacy=legacy, skill=skill, percent=50)
        # 首次创建选定 engine 并写入 task state
        selected = router.select_engine(_req())
        # Retry 携带 selected_engine → 必须复用同一 executor，不重新 rollout
        result = await router.execute(_req(selected_engine=selected))
        assert result.engine == selected
        called = skill if selected == "skill_planned" else legacy
        assert called.calls == ["execute"]

    async def test_resume_reuses_same_engine(self) -> None:
        """resume 不重新 rollout；用 selected_engine 或模式默认（§32 / §65）。"""
        legacy = _Fake(engine="legacy")
        skill = _Fake(engine="skill_planned")
        # skill_planned 模式显式选择 skill
        router = _router("skill_planned", legacy=legacy, skill=skill)
        result = await router.resume(_req(selected_engine="skill_planned"))
        assert result.engine == "skill_planned"
        assert skill.calls == ["resume"]

        # legacy 模式默认 legacy
        router2 = _router(
            "legacy", legacy=_Fake(engine="legacy"), skill=_Fake(engine="skill_planned")
        )
        result2 = await router2.resume(_req())
        assert result2.engine == "legacy"
