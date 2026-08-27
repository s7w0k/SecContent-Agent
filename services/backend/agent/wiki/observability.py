"""Observability（Phase 18 / §21）— Knowledge Request 结构化 Trace 与运行指标。

- `record_trace()`：为一次 Knowledge Request 输出结构化日志（trace_id/task_id/user_id/
  product_ids/wiki_version/source_snapshot_id/status/coverage/confidence/latency_ms）。
- `KnowledgeMetrics`：进程内运行指标累计器（thread-safe），覆盖 plan §21 运行时指标。
  可注入 Provider / 导出到 Prometheus/日志。

保持轻量、无第三方依赖，只依赖标准库。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger("backend.agent.wiki.observability")

_TRACE_ATTRS = (
    "trace_id",
    "task_id",
    "user_id",
    "tenant_id",
    "product_ids",
    "wiki_version",
    "source_snapshot_id",
    "status",
    "reason",
    "coverage",
    "confidence",
    "evidence_count",
    "pages_opened",
    "tool_calls",
    "latency_ms",
    "action",
)


class KnowledgeMetrics:
    """进程内 Wiki 运行时指标计数器（thread-safe）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._sum_latency_ms = 0.0
        self._requests = 0

    def inc(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self._events[name] = self._events.get(name, 0) + delta

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe_latency(self, latency_ms: float) -> None:
        with self._lock:
            self._sum_latency_ms += latency_ms
            self._requests += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._events)
            gauges = dict(self._gauges)
            requests = self._requests
            sum_ms = self._sum_latency_ms
        if requests:
            gauges["wiki_request_latency_ms_avg"] = round(sum_ms / requests, 3)
        return {"counters": counters, "gauges": gauges, "requests": requests}


def record_trace(
    *,
    metrics: KnowledgeMetrics | None = None,
    success: bool = True,
    latency_ms: float = 0.0,
    **fields: Any,
) -> None:
    """输出一次 Knowledge Request 的结构化 Trace（§21），并累计运行指标。

    若传入 metrics，会自动累加 request 计数、成功/GROUNDED 事件与覆盖率 gauge。
    """
    payload = {k: fields[k] for k in _TRACE_ATTRS if k in fields and fields[k] not in (None, "")}
    payload["latency_ms"] = payload.get("latency_ms", round(latency_ms, 3))
    payload.setdefault("action", "knowledge.request")
    logger.info("trace event=knowledge.request %s", _fmt(payload))

    if metrics is None:
        return
    metrics.inc("wiki_request_total")
    metrics.inc("wiki_request_success_total" if success else "wiki_request_error_total")
    status = fields.get("status")
    if status:
        metrics.inc(f"wiki_bundle_status_total:{status}")
    if status == "SUFFICIENT":
        metrics.inc("wiki_grounded_total")
    coverage = fields.get("coverage")
    if coverage is not None:
        metrics.gauge("evidence_coverage", float(coverage))
    conf = fields.get("confidence")
    if conf is not None:
        metrics.gauge("evidence_confidence", float(conf))
    metrics.observe_latency(latency_ms)


def _fmt(payload: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in payload.items())
