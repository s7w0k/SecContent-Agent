"""Execution 指标（§59 / §101）：按 engine 打标签的轻量内存计数器。

生产可替换为 Prometheus multi-process；此处提供确定性、可测试的内存实现。
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


class ExecutionMetrics:
    """按 engine 聚合执行指标。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, str], float] = defaultdict(float)
        self._latency: dict[tuple[str, str], list[float]] = defaultdict(list)

    def _inc(self, metric: str, engine: str, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[(metric, engine)] += amount

    def record(
        self,
        *,
        metric: str,
        engine: str,
        value: float = 1.0,
        latency_ms: float | None = None,
    ) -> None:
        self._inc(metric, engine, value)
        if latency_ms is not None:
            key = ("latency", engine)
            with self._lock:
                self._latency[key].append(latency_ms)


class ExecutionMetricsClient:
    """便捷封装：optional 指标，避免装配缺失破坏主流程。"""

    def __init__(self, metrics: ExecutionMetrics | None = None) -> None:
        self._metrics = metrics
        self._enabled = metrics is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(
        self,
        *,
        metric: str,
        engine: str,
        value: float = 1.0,
        latency_ms: float | None = None,
    ) -> None:
        if self._metrics is not None:
            self._metrics.record(metric=metric, engine=engine, value=value, latency_ms=latency_ms)


__all__ = [
    "ExecutionMetrics",
    "ExecutionMetricsClient",
]
