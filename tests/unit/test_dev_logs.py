"""阶段八任务 8.6：开发者日志 API 完整测试矩阵。"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from api.dev_logs import router
from auth.deps import AuthError, auth_error_handler
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for field, expected in query.items():
        actual = document.get(field)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif isinstance(expected, dict) and "$regex" in expected:
            flags = re.IGNORECASE if "i" in expected.get("$options", "") else 0
            if re.search(expected["$regex"], str(actual or ""), flags) is None:
                return False
        elif actual != expected:
            return False
    return True


class FakeCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = deepcopy(documents)

    def sort(self, field: str, direction: int) -> FakeCursor:
        self.documents.sort(key=lambda item: item.get(field), reverse=direction < 0)
        return self

    def skip(self, count: int) -> FakeCursor:
        self.documents = self.documents[count:]
        return self

    def limit(self, count: int) -> FakeCursor:
        self.documents = self.documents[:count]
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        return deepcopy(self.documents if length is None else self.documents[:length])

    def __aiter__(self):
        async def iterate():
            for document in self.documents:
                yield deepcopy(document)

        return iterate()


class FakeCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = deepcopy(documents)

    def find(self, query: dict[str, Any]) -> FakeCursor:
        return FakeCursor([item for item in self.documents if _matches(item, query)])

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return next((deepcopy(item) for item in self.documents if _matches(item, query)), None)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(_matches(item, query) for item in self.documents)

    async def distinct(self, field: str, query: dict[str, Any] | None = None) -> list[Any]:
        matched = [item for item in self.documents if _matches(item, query or {})]
        return list({item[field] for item in matched if item.get(field) is not None})

    def aggregate(self, pipeline: list[dict[str, Any]]) -> FakeCursor:
        query = pipeline[0].get("$match", {})
        matched = [item for item in self.documents if _matches(item, query)]
        users: dict[str, str] = {}
        for item in matched:
            if item.get("user_id"):
                users[item["user_id"]] = item.get("username") or item["user_id"]
        documents = [
            {"_id": user_id, "username": username}
            for user_id, username in sorted(users.items(), key=lambda item: (item[1], item[0]))
        ]
        return FakeCursor(documents)


def _log(
    log_id: str,
    *,
    user_id: str,
    username: str,
    trace_id: str,
    phase: str,
    level: str,
    message: str,
    created_at: datetime,
    duration_ms: int | None = None,
    error: dict[str, Any] | None = None,
    date: str = "2026-07-13",
) -> dict[str, Any]:
    return {
        "_id": f"mongo-{log_id}",
        "log_id": log_id,
        "trace_id": trace_id,
        "user_id": user_id,
        "username": username,
        "level": level,
        "phase": phase,
        "action": "error" if error else "complete",
        "message": message,
        "detail": {"source": "test"},
        "duration_ms": duration_ms,
        "error": error,
        "created_at": created_at,
        "date": date,
    }


@pytest.fixture
def app() -> FastAPI:
    logs = [
        _log(
            "log-a-crawl",
            user_id="user-a",
            username="alice",
            trace_id="trace-a",
            phase="crawl",
            level="INFO",
            message="海外新闻爬取完成",
            duration_ms=100,
            created_at=datetime(2026, 7, 13, 1, 0, tzinfo=UTC),
        ),
        _log(
            "log-a-score",
            user_id="user-a",
            username="alice",
            trace_id="trace-a",
            phase="score_v2",
            level="WARNING",
            message="评分完成，部分文章信息不足",
            duration_ms=200,
            created_at=datetime(2026, 7, 13, 1, 1, tzinfo=UTC),
        ),
        _log(
            "log-b-crawl",
            user_id="user-b",
            username="bob",
            trace_id="trace-b",
            phase="crawl",
            level="INFO",
            message="公众号爬取完成",
            duration_ms=300,
            created_at=datetime(2026, 7, 13, 2, 0, tzinfo=UTC),
        ),
        _log(
            "log-b-draft",
            user_id="user-b",
            username="bob",
            trace_id="trace-b",
            phase="draft",
            level="ERROR",
            message="草稿生成失败",
            duration_ms=400,
            error={
                "type": "RuntimeError",
                "message": "model unavailable",
                "stack_trace": "Traceback: RuntimeError: model unavailable",
            },
            created_at=datetime(2026, 7, 13, 2, 1, tzinfo=UTC),
        ),
        _log(
            "log-old",
            user_id="user-c",
            username="carol",
            trace_id="trace-old",
            phase="auth",
            level="INFO",
            message="用户登录",
            created_at=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
            date="2026-07-12",
        ),
    ]
    users = [
        {"user_id": "developer", "username": "dev", "is_developer": True},
        {"user_id": "normal", "username": "normal", "is_developer": False},
    ]
    test_app = FastAPI()
    test_app.state.db = {
        "pipeline_logs": FakeCollection(logs),
        "users": FakeCollection(users),
    }
    test_app.add_exception_handler(AuthError, auth_error_handler)

    @test_app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.user_id = request.headers.get("X-Test-User")
        return await call_next(request)

    test_app.include_router(router)
    return test_app


async def _get(app: FastAPI, path: str, user: str = "developer"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers={"X-Test-User": user})


@pytest.mark.asyncio
async def test_developer_queries_logs_across_users(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 4
    assert {item["user_id"] for item in data["logs"]} == {"user-a", "user-b"}


@pytest.mark.asyncio
async def test_developer_filters_logs_by_user_and_paginates(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13&user_id=user-a&page=2&page_size=1")
    data = response.json()["data"]
    assert data["total"] == 2
    assert data["page"] == 2
    assert data["page_size"] == 1
    assert len(data["logs"]) == 1
    assert data["logs"][0]["user_id"] == "user-a"


@pytest.mark.asyncio
async def test_normal_user_is_forbidden(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13", user="normal")
    assert response.status_code == 403
    assert response.json() == {
        "ok": False,
        "error": {"code": "FORBIDDEN", "message": "需要开发者权限"},
    }


@pytest.mark.asyncio
async def test_trace_returns_complete_ordered_chain(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs/trace/trace-b")
    data = response.json()["data"]
    assert [event["phase"] for event in data["events"]] == ["crawl", "draft"]
    assert data["total_duration_ms"] == 700
    assert data["phase_count"] == 2
    assert data["has_error"] is True


@pytest.mark.asyncio
async def test_trace_not_found(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs/trace/missing")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "TRACE_NOT_FOUND"


@pytest.mark.asyncio
async def test_filters_multiple_phases(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13&phase=crawl,draft")
    assert {item["phase"] for item in response.json()["data"]["logs"]} == {
        "crawl",
        "draft",
    }


@pytest.mark.asyncio
async def test_filters_multiple_levels(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13&level=WARNING,ERROR")
    assert {item["level"] for item in response.json()["data"]["logs"]} == {
        "WARNING",
        "ERROR",
    }


@pytest.mark.asyncio
async def test_filters_keyword_case_insensitively(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13&keyword=%E6%B5%B7%E5%A4%96")
    logs = response.json()["data"]["logs"]
    assert [item["log_id"] for item in logs] == ["log-a-crawl"]


@pytest.mark.asyncio
async def test_error_log_contains_stack_trace(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs?date=2026-07-13&level=ERROR")
    error = response.json()["data"]["logs"][0]["error"]
    assert error["type"] == "RuntimeError"
    assert "RuntimeError" in error["stack_trace"]


@pytest.mark.asyncio
async def test_dates_are_global_and_descending(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs/dates")
    assert response.json()["data"]["dates"] == ["2026-07-13", "2026-07-12"]


@pytest.mark.asyncio
async def test_stats_aggregate_levels_users_errors_and_duration(app: FastAPI) -> None:
    response = await _get(app, "/api/dev/logs/stats?date=2026-07-13")
    data = response.json()["data"]
    assert data["total"] == 4
    assert data["by_level"] == {"INFO": 2, "WARNING": 1, "ERROR": 1}
    assert data["by_phase"] == {"crawl": 2, "score_v2": 1, "draft": 1}
    assert data["error_count"] == 1
    assert data["avg_duration_ms"] == {"crawl": 200, "score_v2": 200, "draft": 400}
