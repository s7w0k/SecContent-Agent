"""Phase 18 Observability（§21）单元测试：Knowledge Request 结构化 Trace 与运行指标。"""

from __future__ import annotations

import logging

from agent.wiki.observability import KnowledgeMetrics, record_trace


def test_knowledge_metrics_inc_and_snapshot():
    m = KnowledgeMetrics()
    m.inc("wiki_request_total")
    m.inc("wiki_request_total")
    m.inc("wiki_grounded_total")
    m.gauge("evidence_coverage", 0.8)
    snap = m.snapshot()
    assert snap["counters"]["wiki_request_total"] == 2
    assert snap["counters"]["wiki_grounded_total"] == 1
    assert snap["gauges"]["evidence_coverage"] == 0.8
    assert snap["requests"] == 0


def test_record_trace_accumulates_request_and_grounded():
    m = KnowledgeMetrics()
    record_trace(
        metrics=m,
        status="SUFFICIENT",
        coverage=0.85,
        confidence=0.9,
        latency_ms=12.5,
        trace_id="t-1",
        reason="found",
    )
    snap = m.snapshot()
    assert snap["counters"]["wiki_request_total"] == 1
    assert snap["counters"]["wiki_request_success_total"] == 1
    assert snap["counters"]["wiki_grounded_total"] == 1
    assert snap["counters"]["wiki_bundle_status_total:SUFFICIENT"] == 1
    assert snap["gauges"]["evidence_coverage"] == 0.85
    assert snap["gauges"]["evidence_confidence"] == 0.9
    assert snap["requests"] == 1
    assert snap["gauges"]["wiki_request_latency_ms_avg"] == 12.5


def test_record_trace_error_and_non_sufficient_not_grounded():
    m = KnowledgeMetrics()
    record_trace(metrics=m, status="FAILED", success=False, latency_ms=3.0)
    record_trace(metrics=m, status="PARTIAL", latency_ms=5.0)
    snap = m.snapshot()
    assert snap["counters"]["wiki_request_total"] == 2
    assert snap["counters"]["wiki_request_error_total"] == 1
    assert snap["counters"]["wiki_request_success_total"] == 1
    assert "wiki_grounded_total" not in snap["counters"]
    assert snap["gauges"]["wiki_request_latency_ms_avg"] == round((3.0 + 5.0) / 2, 3)


def test_record_trace_without_metrics_only_logs(caplog):
    caplog.set_level(logging.INFO)
    record_trace(status="SUFFICIENT", trace_id="t-9", evidence_count=3)
    assert any(
        "event=knowledge.request" in r and "trace_id=t-9" in r and "evidence_count=3" in r
        for r in caplog.messages
    )


def test_record_trace_filters_empty_fields(caplog):
    caplog.set_level(logging.INFO)
    record_trace(status="SUFFICIENT", reason="", coverage=None, wiki_version="")
    for r in caplog.messages:
        assert "reason=" not in r or r.count("reason=") == 0
        assert "coverage=" not in r
