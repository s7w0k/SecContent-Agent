"""阶段八任务 8.1：日志模型与开发者权限测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from api.logs import _log_to_db, log_pipeline
from auth.deps import AuthError, get_developer_user
from db.mongo import MongoDB
from models.feedback import PipelineLog
from models.user import UserInDB, UserPublic

from scripts.set_developer import _resolve_backend_dir, set_developer


def _request(user_id: str | None, db) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(user_id=user_id),
        app=SimpleNamespace(state=SimpleNamespace(db=db)),
    )


def test_pipeline_log_enhanced_fields_are_backward_compatible() -> None:
    legacy = PipelineLog(
        user_id="user-a",
        level="INFO",
        phase="crawl",
        message="完成",
        date="2026-07-13",
    )
    assert legacy.log_id.startswith("log-")
    assert legacy.trace_id is None
    assert legacy.action == "complete"
    assert legacy.duration_ms is None
    assert legacy.error is None

    enhanced = legacy.model_copy(
        update={
            "trace_id": "trace-1",
            "username": "alice",
            "action": "error",
            "duration_ms": 120,
            "error": {"type": "RuntimeError", "message": "failed", "stack_trace": "..."},
        }
    )
    assert enhanced.trace_id == "trace-1"
    assert enhanced.error["stack_trace"] == "..."


def test_developer_flag_defaults_false_and_is_public() -> None:
    user = UserInDB(username="alice", display_name="Alice", hashed_password="hash")
    public = UserPublic.model_validate(user.model_dump())
    assert user.is_developer is False
    assert public.is_developer is False


@pytest.mark.asyncio
async def test_get_developer_user_allows_developer() -> None:
    users = MagicMock()
    users.find_one = AsyncMock(
        return_value={"user_id": "user-a", "username": "alice", "is_developer": True}
    )
    user_id, user = await get_developer_user(_request("user-a", {"users": users}))
    assert user_id == "user-a"
    assert user["username"] == "alice"


@pytest.mark.asyncio
async def test_get_developer_user_rejects_normal_user() -> None:
    users = MagicMock()
    users.find_one = AsyncMock(
        return_value={"user_id": "user-a", "username": "alice", "is_developer": False}
    )
    with pytest.raises(AuthError) as caught:
        await get_developer_user(_request("user-a", {"users": users}))
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_log_pipeline_persists_enhanced_fields() -> None:
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    db = {"pipeline_logs": collection}

    task = log_pipeline(
        db,
        "ERROR",
        "draft",
        "草稿失败",
        user_id="user-a",
        username="alice",
        trace_id="trace-1",
        action="error",
        duration_ms=321,
        error={"type": "RuntimeError", "message": "failed", "stack_trace": "trace"},
        detail={"draft_count": 0},
    )
    assert task is not None
    await task

    document = collection.insert_one.await_args.args[0]
    assert document["log_id"].startswith("log-")
    assert document["trace_id"] == "trace-1"
    assert document["username"] == "alice"
    assert document["action"] == "error"
    assert document["duration_ms"] == 321
    assert document["error"]["stack_trace"] == "trace"


@pytest.mark.asyncio
async def test_log_write_failure_is_degraded_to_warning(caplog: pytest.LogCaptureFixture) -> None:
    collection = MagicMock()
    collection.insert_one = AsyncMock(side_effect=RuntimeError("mongo unavailable"))
    with caplog.at_level("WARNING", logger="backend.api.logs"):
        await _log_to_db(
            {"pipeline_logs": collection},
            "INFO",
            "crawl",
            "完成",
            "user-a",
        )
    assert "log_pipeline 写入失败" in caplog.text


@pytest.mark.asyncio
async def test_set_developer_updates_only_existing_user() -> None:
    users = MagicMock()
    users.find_one = AsyncMock(return_value={"user_id": "user-a", "username": "alice"})
    users.update_one = AsyncMock()
    result = await set_developer("alice", db={"users": users})
    assert result == {"user_id": "user-a", "username": "alice", "is_developer": True}
    users.update_one.assert_awaited_once_with(
        {"user_id": "user-a"}, {"$set": {"is_developer": True}}
    )


def test_set_developer_resolves_source_and_container_layouts(tmp_path: Path) -> None:
    source_script = tmp_path / "source" / "scripts" / "set_developer.py"
    source_backend = tmp_path / "source" / "services" / "backend"
    source_backend.mkdir(parents=True)
    source_script.parent.mkdir(parents=True)
    (source_backend / "config.py").touch()
    assert _resolve_backend_dir(str(source_script)) == str(source_backend)

    container_script = tmp_path / "container" / "scripts" / "set_developer.py"
    container_script.parent.mkdir(parents=True)
    (tmp_path / "container" / "config.py").touch()
    assert _resolve_backend_dir(str(container_script)) == str(tmp_path / "container")


@pytest.mark.asyncio
async def test_pipeline_log_indexes_are_idempotently_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, list] = {}

    class FakeCollection:
        def __init__(self, name: str) -> None:
            self.name = name

        async def create_indexes(self, indexes):
            captured[self.name] = indexes
            return [index.document["name"] for index in indexes]

        async def drop_index(self, name: str) -> None:
            return None

    collections: dict[str, FakeCollection] = {}

    def get_collection(cls, name: str) -> FakeCollection:
        return collections.setdefault(name, FakeCollection(name))

    monkeypatch.setattr(MongoDB, "get_collection", classmethod(get_collection))
    await MongoDB.ensure_indexes()
    names = {index.document["name"] for index in captured["pipeline_logs"]}
    assert {
        "idx_pipeline_log_user_date",
        "idx_pipeline_log_trace_created",
        "idx_pipeline_log_phase_date",
        "idx_pipeline_log_level_date",
        "idx_pipeline_log_date_created",
    } <= names
