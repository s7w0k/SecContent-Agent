"""P3 Ops 观测出口：collect_metrics 计数与状态分布测试。"""

from __future__ import annotations

import pytest
from api.ops import collect_metrics


class _CountingCollection:
    def __init__(self, default: int | None = 0, by_status: dict | None = None):
        self.default = default
        self.by_status = dict(by_status or {})

    async def count_documents(self, query: dict | None = None):
        q = query or {}
        if "status" in q:
            return self.by_status.get(q["status"], 0)
        if self.default is None:
            raise RuntimeError("db unavailable")
        return self.default


class _FakeDb:
    def __init__(self, collections: dict):
        self.collections = collections

    def __getitem__(self, name: str):
        return self.collections[name]


def _db() -> _FakeDb:
    return _FakeDb(
        {
            "pipeline_tasks": _CountingCollection(
                by_status={
                    "pending": 1,
                    "running": 2,
                    "resume_pending": 0,
                    "failed": 1,
                    "completed": 9,
                    "cancelled": 0,
                }
            ),
            "users": _CountingCollection(default=5),
            "articles": _CountingCollection(default=120),
            "feedbacks": _CountingCollection(default=3),
            "llm_call_logs": _CountingCollection(default=77),
        }
    )


@pytest.mark.asyncio
async def test_collect_metrics_sums_active_tasks_and_counts():
    metrics = await collect_metrics(_db())
    assert metrics["tasks"]["active"] == 3  # pending 1 + running 2
    assert metrics["tasks"]["by_status"]["completed"] == 9
    assert metrics["users"] == 5
    assert metrics["articles"] == 120
    assert metrics["feedbacks"] == 3
    assert metrics["llm_call_logs"] == 77


@pytest.mark.asyncio
async def test_collect_metrics_tolerates_missing_collections():
    db = _FakeDb({"pipeline_tasks": _CountingCollection(by_status={})})
    metrics = await collect_metrics(db)
    assert metrics["tasks"]["active"] == 0
    assert metrics["users"] is None  # 单集合计数失败 → None 而非整体失败
