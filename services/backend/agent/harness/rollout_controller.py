"""自动回滚控制器 — 阶段4 §3/§4（WBS 4.4）。

- RolloutStage / RolloutTracker：offline→shadow→internal→1%→10%→50%→100% 灰度推进，
  以「最小成功样本量 + 观察窗口」为准，不以自然日替代样本量；
- FeatureGate：轻量灰度开关（百分比/总开关）；
- RollbackController：指标告警 → 安全回滚决策；
- RollbackLedger：回滚审计（不可抵赖）。

安全约束：自动回滚只关闭流量或高风险能力（降低/清零灰度百分比、停新任务、
熔断能力），不自动修改 prompt、模型、知识或业务数据。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agent.harness.observability import AlertEvaluator


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RolloutStage(StrEnum):
    """灰度档位（§4.1 Chat Agent 上线顺序）。"""

    OFFLINE = "offline"
    SHADOW = "shadow"
    INTERNAL = "internal"
    PERCENT_1 = "1%"
    PERCENT_10 = "10%"
    PERCENT_50 = "50%"
    FULL = "100%"


STAGE_ORDER: tuple[RolloutStage, ...] = (
    RolloutStage.OFFLINE,
    RolloutStage.SHADOW,
    RolloutStage.INTERNAL,
    RolloutStage.PERCENT_1,
    RolloutStage.PERCENT_10,
    RolloutStage.PERCENT_50,
    RolloutStage.FULL,
)

STAGE_PERCENT: dict[RolloutStage, float] = {
    RolloutStage.OFFLINE: 0.0,
    RolloutStage.SHADOW: 0.0,  # shadow 返回给影子通道，不进入用户可见流量
    RolloutStage.INTERNAL: 0.0,  # 内部员工试用（按白名单）
    RolloutStage.PERCENT_1: 0.01,
    RolloutStage.PERCENT_10: 0.10,
    RolloutStage.PERCENT_50: 0.50,
    RolloutStage.FULL: 1.0,
}


class FeatureGate:
    """可调灰度开关（百分比 + 总开关）。"""

    def __init__(self, *, capability: str, percent: float = 0.0, enabled: bool = False):
        self.capability = capability
        self.percent = max(0.0, min(1.0, percent))
        self.enabled = enabled

    def set_percent(self, percent: float) -> None:
        self.percent = max(0.0, min(1.0, percent))

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "enabled": self.enabled,
            "percent": self.percent,
        }


@dataclass(frozen=True)
class RolloutSample:
    """灰度样本（仅记录结果与用户 hash，不落正文）。"""

    user_id: str
    outcome: str  # success | partial | fail
    ts: datetime


class RolloutTracker:
    """灰度档位推进：样本量 + 观察窗口双条件。"""

    def __init__(
        self,
        *,
        capability: str,
        stage: RolloutStage = RolloutStage.OFFLINE,
        min_sample_size: int = 0,
        observation_window_seconds: int = 0,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.capability = capability
        self.stage = stage
        self.min_sample_size = min_sample_size
        self.observation_window_seconds = observation_window_seconds
        self.samples: list[RolloutSample] = []
        self.advanced_at: datetime | None = None
        self._now = now_provider or _utc_now

    def record(self, *, user_id: str, outcome: str, ts: datetime | None = None) -> None:
        self.samples.append(
            RolloutSample(user_id=user_id, outcome=outcome, ts=ts or self._now())
        )

    def sample_count(self) -> int:
        return len(self.samples)

    def success_rate(self) -> float:
        if not self.samples:
            return 0.0
        ok = sum(1 for s in self.samples if s.outcome == "success")
        return ok / len(self.samples)

    def ready_to_advance(self, *, now: datetime | None = None) -> bool:
        """达到最小样本量且观察窗口足够（不以自然日替代样本量）。"""
        if len(self.samples) < self.min_sample_size:
            return False
        stamp = now or self._now()
        if not self.observation_window_seconds:
            return True
        first = self.samples[0].ts
        return (stamp - first).total_seconds() >= self.observation_window_seconds

    def next_stage(self) -> RolloutStage | None:
        idx = STAGE_ORDER.index(self.stage)
        return STAGE_ORDER[idx + 1] if idx + 1 < len(STAGE_ORDER) else None

    def advance(self, *, now: datetime | None = None) -> RolloutStage | None:
        nxt = self.next_stage()
        if nxt is not None:
            self.stage = nxt
            self.advanced_at = now or self._now()
        return nxt

    def snapshot(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "stage": self.stage.value,
            "samples": len(self.samples),
            "min_sample_size": self.min_sample_size,
            "observation_window_seconds": self.observation_window_seconds,
            "success_rate": round(self.success_rate(), 4),
            "ready_to_advance": self.ready_to_advance(),
            "advanced_at": self.advanced_at.isoformat() if self.advanced_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# 回滚决策与审计
# ═══════════════════════════════════════════════════════════════

# 回滚动作（阶段4 §3 触发条件 → 动作）
ACTION_NONE = "none"
ACTION_ROLLBACK = "rollback"  # 回退当前灰度档（关闭流量）
ACTION_STOP_NEW = "stop_new"  # 停止放量/新任务
ACTION_DEGRADE = "degrade"  # 降级模型或路径（此处仅关流量，不自动改配置）
ACTION_BREAKER_CAPABILITY = "breaker_capability"  # 熔断能力的新任务
ACTION_STOP_WRITE_TOOLS = "stop_write_tools"  # 停止相关写工具

# 动作 → 应用到 FeatureGate 的方式（全部只关流量）
_ACTION_GATE_EFFECT: dict[str, tuple[bool, float]] = {
    ACTION_NONE: (False, 0.0),  # 保持不动
    ACTION_ROLLBACK: (True, 0.0),  # 停用
    ACTION_STOP_NEW: (False, 0.0),  # 仅停新任务（enabled=False）
    ACTION_DEGRADE: (True, 0.0),  # 停用（不自动改模型）
    ACTION_BREAKER_CAPABILITY: (False, 0.0),  # 熔断能力
    ACTION_STOP_WRITE_TOOLS: (False, 0.0),  # 停写工具（能力级）
}


@dataclass(frozen=True)
class RollbackDecision:
    """自动回滚决策（安全：只关闭流量，不修改 prompt/模型/知识/数据）。"""

    action: str
    reason_code: str = ""
    rule_name: str = ""
    metric_name: str = ""
    threshold: float = 0.0
    value: float = 0.0
    fired_at: datetime = field(default_factory=_utc_now)
    safe: bool = True

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason_code": self.reason_code,
            "rule_name": self.rule_name,
            "metric_name": self.metric_name,
            "threshold": self.threshold,
            "value": self.value,
            "fired_at": self.fired_at.isoformat(),
            "safe": self.safe,
        }


class RollbackLedger:
    """回滚审计（不可抵赖，仅记录动作与触发指标）。"""

    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self._entries: list[dict[str, Any]] = list(entries) if entries else []

    def record(self, *, decision: RollbackDecision, capability: str, actor: str = "auto") -> None:
        self._entries.append(
            {
                "capability": capability,
                "actor": actor,
                "action": decision.action,
                "reason_code": decision.reason_code,
                "rule_name": decision.rule_name,
                "metric_name": decision.metric_name,
                "value": decision.value,
                "threshold": decision.threshold,
                "fired_at": decision.fired_at.isoformat(),
            }
        )

    def entries_for(self, capability: str) -> list[dict[str, Any]]:
        return [e for e in self._entries if e["capability"] == capability]

    def all(self) -> list[dict[str, Any]]:
        return list(self._entries)


class RollbackController:
    """告警评估 → 回滚决策 → 应用到 FeatureGate（+ 审计）。

    任何动作都只操作 FeatureGate（percent/enabled）与能力级熔断，
    不自动修改 prompt、模型、知识或业务数据。
    """

    def __init__(
        self,
        *,
        evaluator: AlertEvaluator,
        ledger: RollbackLedger | None = None,
        breaker_registry: Any = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.evaluator = evaluator
        self.ledger = ledger or RollbackLedger()
        self.breaker_registry = breaker_registry
        self._now = now_provider or _utc_now

    def evaluate(self, metrics: dict[str, float]) -> RollbackDecision:
        firing = self.evaluator.evaluate(metrics)
        if not firing:
            return RollbackDecision(action=ACTION_NONE, reason_code="no_alert", fired_at=self._now())
        worst = firing[0]
        return RollbackDecision(
            action=worst.action,
            reason_code=worst.rule_name,
            rule_name=worst.rule_name,
            metric_name=worst.metric,
            threshold=worst.threshold,
            value=worst.value,
            fired_at=worst.fired_at,
            safe=True,
        )

    def apply(self, decision: RollbackDecision, *, gate: FeatureGate, capability: str = "") -> None:
        """将决策应用到 FeatureGate（只关流量）。"""
        cap = capability or gate.capability
        if decision.action == ACTION_ROLLBACK:
            gate.set_percent(0.0)
            gate.set_enabled(False)
        elif decision.action == ACTION_STOP_NEW:
            gate.set_enabled(False)
        elif decision.action == ACTION_DEGRADE:
            gate.set_percent(0.0)
            gate.set_enabled(False)
        elif decision.action == ACTION_BREAKER_CAPABILITY and self.breaker_registry is not None:
            gate.set_enabled(False)
            try:
                breaker = self.breaker_registry.breaker(f"capability:{cap}")
                # 打开能力级熔断：记录达到阈值次数的失败（不依赖脆弱的下游接口）
                for _ in range(max(1, breaker.config.failure_threshold)):
                    breaker.record_failure(timed_out=False)
            except Exception:
                pass
        elif decision.action == ACTION_STOP_WRITE_TOOLS:
            gate.set_enabled(False)
        if decision.action != ACTION_NONE:
            self.ledger.record(decision=decision, capability=cap)

    def snapshot(self) -> dict[str, Any]:
        return {"ledger_entries": len(self.ledger.all())}
