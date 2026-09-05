"""生产观测与告警 — 阶段4 §2/§3（WBS 4.3）。

- MetricCollector：进程内 counter/gauge/histogram 聚合（按标签），
  供质量/Token 成本/可靠性三类仪表盘、告警与自动回滚消费；
- Sli / Slo：SLI 定义与 SLO 达标率计算；
- AlertRule + AlertEvaluator：阶段4 §3 的 8 条自动回滚触发条件，缺省即用。

安全约束：只聚合数值与标签，不落 prompt 正文、完整工具参数、密钥或私有思维链。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MetricKind(StrEnum):
    """指标类型。"""

    COUNTER = "counter"  # 单调递增计数
    GAUGE = "gauge"  # 可增可减的瞬时值
    HISTOGRAM = "histogram"  # 分布（min/p50/p95/max）


@dataclass(frozen=True)
class MetricDef:
    """指标定义（名称/类型/单位/标签）。"""

    name: str
    kind: MetricKind
    unit: str = ""
    help: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistSummary:
    """直方图摘要。"""

    count: int
    min: float
    p50: float
    p95: float
    max: float


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = max(0, min(len(sorted_v) - 1, round((p / 100.0) * (len(sorted_v) - 1))))
    return sorted_v[idx]


class MetricCollector:
    """进程内指标聚合器（window 内保留，可滚动）。"""

    def __init__(self, *, now_provider: Callable[[], datetime] | None = None):
        self._now = now_provider or _utc_now
        self._counters: dict[tuple[str, ...], int] = defaultdict(int)
        self._gauges: dict[tuple[str, ...], float] = {}
        self._hist: dict[tuple[str, ...], list[float]] = defaultdict(list)

    # ── 写入 ────────────────────────────────────────────────

    def inc(self, name: str, *, amount: float = 1.0, **tags: Any) -> None:
        key = self._key(name, tags)
        self._counters[key] += int(amount)

    def set_gauge(self, name: str, *, value: float, **tags: Any) -> None:
        key = self._key(name, tags)
        self._gauges[key] = float(value)

    def observe(self, name: str, *, value: float, **tags: Any) -> None:
        key = self._key(name, tags)
        self._hist[key].append(float(value))

    # ── 读取 ────────────────────────────────────────────────

    def counter(self, name: str, **tags: Any) -> int:
        return self._counters.get(self._key(name, tags), 0)

    def gauge(self, name: str, **tags: Any) -> float:
        return self._gauges.get(self._key(name, tags), 0.0)

    def histogram(self, name: str, **tags: Any) -> HistSummary:
        values = self._hist.get(self._key(name, tags), [])
        if not values:
            return HistSummary(count=0, min=0.0, p50=0.0, p95=0.0, max=0.0)
        return HistSummary(
            count=len(values),
            min=min(values),
            p50=_percentile(values, 50),
            p95=_percentile(values, 95),
            max=max(values),
        )

    def snapshot(self) -> dict[str, Any]:
        """导出全部指标（名称→按标签聚合），供告警/回滚/报告。"""
        out: dict[str, Any] = {"counters": {}, "gauges": {}, "histograms": {}}
        for key, value in self._counters.items():
            out["counters"][self._label_key(key)] = value
        for key, value in self._gauges.items():
            out["gauges"][self._label_key(key)] = value
        for key, values in self._hist.items():
            out["histograms"][self._label_key(key)] = HistSummary(
                count=len(values),
                min=min(values),
                p50=_percentile(values, 50),
                p95=_percentile(values, 95),
                max=max(values),
            )
        return out

    def reset(self) -> None:
        self._counters.clear()
        self._gauges.clear()
        self._hist.clear()

    # ── 内部 ────────────────────────────────────────────────

    @staticmethod
    def _key(name: str, tags: dict[str, Any]) -> tuple[str, ...]:
        return (name, *(f"{k}={v}" for k, v in sorted(tags.items())))

    @staticmethod
    def _label_key(key: tuple[str, ...]) -> str:
        return "|".join(key)


# ═══════════════════════════════════════════════════════════════
# SLI / SLO
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Sli:
    """服务级别指标：好事件 / 总事件。"""

    name: str
    good_events: int = 0
    total_events: int = 0

    def ratio(self) -> float:
        return self.good_events / self.total_events if self.total_events else 1.0


@dataclass(frozen=True)
class Slo:
    """服务级别目标：指定 SLI 在窗口内的达标率。"""

    name: str
    sli: str
    target_ratio: float = 0.99
    window_seconds: int = 0

    def met(self, ratio: float) -> bool:
        return ratio >= self.target_ratio


# ═══════════════════════════════════════════════════════════════
# 告警规则（阶段4 §3 初始触发条件）
# ═══════════════════════════════════════════════════════════════

ALERT_ACTION_STOP_NEW = "stop_new"
ALERT_ACTION_ROLLBACK = "rollback"
ALERT_ACTION_DEGRADE = "degrade"
ALERT_ACTION_BREAKER_CAPABILITY = "breaker_capability"
ALERT_ACTION_STOP_WRITE_TOOLS = "stop_write_tools"


@dataclass(frozen=True)
class AlertRule:
    """告警/自动回滚触发规则。"""

    name: str
    metric: str  # 指标键（AlertEvaluator 输入 dict 中的键）
    op: str  # ">=" | ">" | "<=" | "<" | "=="
    threshold: float
    window_seconds: int = 0
    severity: str = "P1"
    action: str = ALERT_ACTION_ROLLBACK
    enabled: bool = True

    def matches(self, value: float) -> bool:
        if self.op == ">=":
            return value >= self.threshold
        if self.op == ">":
            return value > self.threshold
        if self.op == "<=":
            return value <= self.threshold
        if self.op == "<":
            return value < self.threshold
        if self.op == "==":
            return value == self.threshold
        return False


def default_alert_rules(
    *,
    latency_p95_slo_seconds: float = 5.0,
    usd_budget_delta_pct: float = 10.0,
    recovery_seconds: float = 900.0,
) -> list[AlertRule]:
    """阶段4 §3 建议初始触发条件（数值在阶段0 基线冻结后校准）。"""
    return [
        AlertRule(
            "unsafe_action_any",
            "unsafe_action_count",
            ">",
            0.0,
            severity="P0",
            action=ALERT_ACTION_STOP_NEW,
        ),
        AlertRule(
            "duplicate_side_effect_any",
            "duplicate_side_effect_count",
            ">",
            0.0,
            severity="P0",
            action=ALERT_ACTION_STOP_WRITE_TOOLS,
        ),
        AlertRule(
            "error_rate_delta_pp",
            "error_rate_delta_pp",
            ">",
            0.5,
            severity="P1",
            action=ALERT_ACTION_ROLLBACK,
        ),
        AlertRule(
            "latency_p95_slo",
            "latency_p95_seconds",
            ">",
            latency_p95_slo_seconds,
            window_seconds=900,
            severity="P1",
            action=ALERT_ACTION_ROLLBACK,
        ),
        AlertRule(
            "usd_per_success_budget",
            "usd_per_success_budget_delta_pct",
            ">",
            usd_budget_delta_pct,
            severity="P1",
            action=ALERT_ACTION_DEGRADE,
        ),
        AlertRule(
            "budget_exhaustion_rate",
            "budget_exhaustion_rate",
            ">",
            0.05,
            severity="P1",
            action=ALERT_ACTION_STOP_NEW,
        ),
        AlertRule(
            "quality_spot_check",
            "quality_spot_below_gate",
            ">",
            0.0,
            severity="P1",
            action=ALERT_ACTION_STOP_NEW,
        ),
        AlertRule(
            "stuck_running",
            "stuck_running_seconds",
            ">",
            recovery_seconds,
            severity="P1",
            action=ALERT_ACTION_BREAKER_CAPABILITY,
        ),
    ]


@dataclass(frozen=True)
class AlertFiring:
    """已触发的告警。"""

    rule_name: str
    metric: str
    value: float
    threshold: float
    severity: str
    action: str
    fired_at: datetime


class AlertEvaluator:
    """对指标快照评估全部规则，返回触发列表（按严重度降序）。"""

    def __init__(
        self,
        rules: list[AlertRule] | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self.rules = list(rules) if rules is not None else default_alert_rules()
        self._now = now_provider or _utc_now

    def evaluate(self, metrics: dict[str, float]) -> list[AlertFiring]:
        firing: list[AlertFiring] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            value = metrics.get(rule.metric)
            if value is None:
                continue
            if rule.matches(float(value)):
                firing.append(
                    AlertFiring(
                        rule_name=rule.name,
                        metric=rule.metric,
                        value=float(value),
                        threshold=rule.threshold,
                        severity=rule.severity,
                        action=rule.action,
                        fired_at=self._now(),
                    )
                )
        firing.sort(key=lambda f: (f.severity, f.rule_name))
        return firing

    def worst_action(self, metrics: dict[str, float]) -> str | None:
        """返回当前最严重的告警动作（无触发返回 None）。"""
        firing = self.evaluate(metrics)
        return firing[0].action if firing else None


# ═══════════════════════════════════════════════════════════════
# 生产指标字段统一：MetricCollector 原语 → 告警规则输入键
# ═══════════════════════════════════════════════════════════════


def production_alert_metrics(
    collector: MetricCollector,
    *,
    usd_budget_per_success: float = 0.0,
) -> dict[str, float]:
    """从生产 MetricCollector 聚合出告警规则输入键（统一字段）。

    生产链路（AgentLoop / AutonomousRunService）喂原语计数，本函数负责
    聚合出 default_alert_rules 引用的派生指标：

    - error_rate_delta_pp：当前窗口错误率（基线为 0，阶段0 基线冻结后校准）；
    - usd_per_success_budget_delta_pct：每次成功成本相对预算的偏差百分比；
    - budget_exhaustion_rate：预算耗尽 run 占比；
    - latency_p95_seconds：run 时长 p95；
    - stuck_running_seconds：当前卡住运行的秒数（gauge，由 reaper 维护）。

    cost_usd_micro_total 以微美元累计（counter 为整数，避免浮点截断）。
    """
    total = float(collector.counter("run_finished_total"))
    succeeded = float(collector.counter("run_completed_total"))
    exhausted = float(collector.counter("budget_exhausted"))
    errors = float(collector.counter("error_events_total"))
    cost_usd = collector.counter("cost_usd_micro_total") / 1_000_000.0

    error_rate = errors / total if total else 0.0
    budget_rate = exhausted / total if total else 0.0
    usd_delta_pct = 0.0
    if usd_budget_per_success > 0 and succeeded:
        per_success = cost_usd / succeeded
        usd_delta_pct = (per_success - usd_budget_per_success) / usd_budget_per_success * 100.0

    return {
        "unsafe_action_count": float(collector.counter("unsafe_action_count")),
        "duplicate_side_effect_count": float(collector.counter("duplicate_side_effect_count")),
        "error_rate_delta_pp": round(error_rate * 100.0, 4),
        "latency_p95_seconds": collector.histogram("run_duration_seconds").p95,
        "usd_per_success_budget_delta_pct": round(usd_delta_pct, 4),
        "budget_exhaustion_rate": round(budget_rate, 6),
        "quality_spot_below_gate": float(collector.counter("quality_spot_below_gate")),
        "stuck_running_seconds": collector.gauge("stuck_running_seconds"),
    }
