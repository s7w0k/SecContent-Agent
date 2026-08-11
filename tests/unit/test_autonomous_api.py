"""PR-4A-06 测试：自主模式 API / SSE / 编排服务。

覆盖 spec 步骤 4A-9（API、事件和前端）与 4A-10（测试与灰度）：
  - AutonomousRunService 端到端：create → start → COMPLETED（DemoPlanner 4 步链）；
  - 多租户隔离：跨用户读取返回 None / 404；
  - 取消流程：cancel_run → cancel_requested → 安全点转为 CANCELED；
  - 审批流程：L2 工具 REQUIRE_APPROVAL → approve → resume → 一次性授权消费后放行；
  - 审批拒绝：reject → 待审批标记 rejected；
  - 工具链服务端白名单：非法工具链被拒绝；
  - 事件流：run 内 sequence 单调递增、Last-Event-ID 续传语义；
  - API 端点：创建 / 列表 / 详情 / 取消 / 审批（含 SSE 流）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from agent.autonomous_service import AutonomousRunService
from agent.policy_engine import ApprovalService, PolicyEngine
from agent.runtime_events import RuntimeEventStore
from agent.runtime_state import BudgetUsage, DecisionSummary, RuntimeStatus
from agent.runtime_store import RuntimeStateStore

FIXED_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════
# Fake Mongo（支持 insert/replace/update/find + $gt/$in/$set/$inc）
# ═══════════════════════════════════════════════════════════════


def _match(doc: dict, query: dict) -> bool:
    for k, v in (query or {}).items():
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$gt" in v and not (doc.get(k, 0) > v["$gt"]):
                return False
            if "$lt" in v and not (doc.get(k, 0) < v["$lt"]):
                return False
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _FakeCursor:
    def __init__(self, col, query):
        self.col = col
        self.query = query
        self._sort_key: str | None = None
        self._sort_reverse: bool = False
        self._limit: int = 1000

    def sort(self, key, direction):
        self._sort_key = key
        self._sort_reverse = direction < 0
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length: int | None = None):
        limit = length or self._limit
        matched = [d for d in self.col.docs if _match(d, self.query)]
        if self._sort_key:
            matched = sorted(
                matched, key=lambda d: d.get(self._sort_key, 0), reverse=self._sort_reverse
            )
        return matched[:limit]


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []
        self.created_indexes: list = []

    async def insert_one(self, doc: dict):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc.get("_id", "id"))

    async def replace_one(self, query, doc, upsert=False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs[i] = doc
                return SimpleNamespace(matched_count=1)
        if upsert:
            self.docs.append(doc)
            return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_one(self, query, update, **kwargs):
        for d in self.docs:
            if _match(d, query):
                for op, fields in update.items():
                    if op == "$set":
                        d.update(fields)
                    elif op == "$inc":
                        for k, v in fields.items():
                            d[k] = d.get(k, 0) + v
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def find_one(self, query=None, sort=None):
        query = query or {}
        if sort and sort[0][0] == "sequence":
            matched = [d for d in self.docs if _match(d, query)]
            if not matched:
                return None
            return max(matched, key=lambda d: d.get("sequence", 0))
        for d in self.docs:
            if _match(d, query):
                return d
        return None

    def find(self, *args, **kwargs):
        query = kwargs.get("filter", args[0] if args else {})
        return _FakeCursor(self, query)

    async def create_indexes(self, indexes):
        self.created_indexes = list(indexes)
        return [i.document["name"] for i in indexes]


class _FakeDB(dict):
    def __init__(self):
        super().__init__()
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str):
        if name not in self._cols:
            self._cols[name] = _FakeCol()
        return self._cols[name]


# ═══════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════


def _make_service(db=None) -> AutonomousRunService:
    """构造测试服务：进程内审批（不写 runtime_approvals），FakeDB 持久化。"""
    return AutonomousRunService(
        store=RuntimeStateStore(db),
        event_store=RuntimeEventStore(db, expires_days=30),
        policy=PolicyEngine(),
        approval_service=ApprovalService(db=None, ttl_seconds=1800),
        db=db,
    )


async def _slow_executor(state, action, meta: dict):
    await asyncio.sleep(0.2)
    if action.tool_name == "export_articles_csv":
        return {"ok": True, "evidence": [{"acceptance_index": 0}], "duration_ms": 5}
    return {"ok": True, "duration_ms": 5}


async def _wait_status(service, run_id, user_id, target, timeout=8.0):
    deadline = time.monotonic() + timeout
    last: str = "?"
    while time.monotonic() < deadline:
        state = await service.get_run(run_id, user_id)
        if state is not None:
            last = state.status.value
            if state.status == target or state.is_terminal:
                return state
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout waiting for {target}, last={last}")


def _slow_service(db=None) -> AutonomousRunService:
    """使用慢执行器，便于取消/审批流程中观察中间状态。"""
    return AutonomousRunService(
        store=RuntimeStateStore(db),
        event_store=RuntimeEventStore(db, expires_days=30),
        policy=PolicyEngine(),
        approval_service=ApprovalService(db=None, ttl_seconds=1800),
        db=db,
        executor_factory=lambda state: _slow_executor,
    )


# ═══════════════════════════════════════════════════════════════
# 服务层：端到端 / 隔离 / 取消 / 审批 / 事件
# ═══════════════════════════════════════════════════════════════


class TestAutonomousService:
    async def test_create_and_run_to_complete(self):
        """默认 4 步工具链端到端：create → start → COMPLETED，证据落库。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(
            user_id="u1",
            goal="完成一次情报处理演示",
            acceptance_criteria=["输出结果文件"],
        )
        assert state.status == RuntimeStatus.PENDING
        assert await service.start_run(state.run_id, "u1")

        final = await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)
        assert final.status == RuntimeStatus.COMPLETED
        assert len(final.completed_steps) == 4
        assert final.evidence  # export 步骤产出验收证据
        assert final.usage.steps == 4

    async def test_start_terminal_run_rejected(self):
        """已完成的 run 不能再次启动。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        final = await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)
        assert await service.start_run(final.run_id, "u1") is False

    async def test_multitenant_isolation(self):
        """跨用户读取运行返回 None（多租户隔离）。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)
        assert await service.get_run(state.run_id, "u2") is None
        runs = await service.list_runs("u2")
        assert runs == []

    async def test_cancel_flow(self):
        """取消流程：RUNNING 中 cancel_run → cancel_requested → CANCELED。"""
        service = _slow_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        running = await _wait_status(service, state.run_id, "u1", RuntimeStatus.RUNNING)
        assert running.status == RuntimeStatus.RUNNING

        assert await service.cancel_run(state.run_id, "u1")
        assert await service.get_run(state.run_id, "u1") is not None  # cancel_requested 仍可读

        final = await _wait_status(service, state.run_id, "u1", RuntimeStatus.CANCELED)
        assert final.status == RuntimeStatus.CANCELED

    async def test_cancel_terminal_run_rejected(self):
        """已终态的 run 不能再取消。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        final = await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)
        assert await service.cancel_run(final.run_id, "u1") is False

    async def test_approval_flow_resume_consumes_token(self):
        """L2 工具触发审批：approve → resume → 一次性授权消费后放行执行。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(
            user_id="u1", goal="审批演示", acceptance_criteria=["完成动作"], tool_chain=["send_message"]
        )
        await service.start_run(state.run_id, "u1")
        waiting = await _wait_status(service, state.run_id, "u1", RuntimeStatus.WAITING_APPROVAL)
        assert waiting.status == RuntimeStatus.WAITING_APPROVAL
        assert len(waiting.approval_state.pending_approvals) == 1
        approval_id = waiting.approval_state.pending_approvals[0].approval_id

        # 审批通过 → 授权进入 approved_tokens
        approved_state = await service.approve(approval_id, "u1")
        assert approved_state is not None
        assert len(approved_state.approval_state.approved_tokens) == 1
        token = approved_state.approval_state.approved_tokens[0]

        # 恢复运行 → runtime 消费授权并执行 send_message
        assert await service.resume_run(state.run_id, "u1")
        final = await _wait_status(service, state.run_id, "u1", RuntimeStatus.STOPPED)
        assert final.status in (RuntimeStatus.STOPPED, RuntimeStatus.COMPLETED)
        assert len(final.completed_steps) == 1
        assert token in final.approval_state.consumed_tokens  # 一次性授权已消费
        assert final.approval_state.approved_tokens == []  # 消费后不可复用

    async def test_reject_flow(self):
        """审批拒绝：待审批项标记 rejected，拒绝后无法用该授权放行。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(
            user_id="u1", goal="审批演示", acceptance_criteria=["完成动作"], tool_chain=["send_message"]
        )
        await service.start_run(state.run_id, "u1")
        waiting = await _wait_status(service, state.run_id, "u1", RuntimeStatus.WAITING_APPROVAL)
        approval_id = waiting.approval_state.pending_approvals[0].approval_id

        rejected_state = await service.reject(approval_id, "u1")
        assert rejected_state is not None
        pending = rejected_state.approval_state.pending_approvals[0]
        assert pending.status == "rejected"
        assert rejected_state.approval_state.approved_tokens == []

    async def test_invalid_tool_chain_rejected(self):
        """工具链服务端白名单：非法工具被过滤后抛 ValueError。"""
        service = _make_service(_FakeDB())
        with pytest.raises(ValueError):
            await service.create_run(
                user_id="u1", goal="目标", acceptance_criteria=["a"],
                tool_chain=["unknown_tool", "also_unknown"],
            )

    async def test_events_sequence_and_resume(self):
        """事件流：sequence 单调递增；last_sequence 断线续传语义。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)

        all_events = await service.events(state.run_id, "u1")
        assert len(all_events) >= 5  # run_created + step_planned/policy/tool x4 + run_finished
        seqs = [e.sequence for e in all_events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # run 内唯一

        mid = seqs[len(seqs) // 2]
        tail = await service.events(state.run_id, "u1", last_sequence=mid)
        assert [e.sequence for e in tail] == [s for s in seqs if s > mid]

        types = {e.event_type for e in all_events}
        assert "run_created" in types and "run_finished" in types and "tool_executed" in types

    async def test_events_other_user_empty(self):
        """跨用户读取事件流返回空（多租户隔离）。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)
        assert await service.events(state.run_id, "u2") == []


# ═══════════════════════════════════════════════════════════════
# API 层：端点 + SSE
# ═══════════════════════════════════════════════════════════════


class TestAutonomousAPI:
    def _app(self, service, *, user="u1"):
        from api.autonomous import router as autonomous_router
        from auth.deps import get_current_user
        from fastapi import FastAPI

        app = FastAPI()
        app.state.autonomous_service = service

        async def override_current_user():
            return user

        app.dependency_overrides[get_current_user] = override_current_user
        app.include_router(autonomous_router)
        return app

    async def _client(self, app):
        from httpx import ASGITransport, AsyncClient

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_create_run_endpoint(self):
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "完成一次情报处理演示", "acceptance_criteria": ["输出结果文件"]},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["run_id"]
            assert body["status"] in ("pending", "running")

            # 轮询至终态
            run_id = body["run_id"]
            for _ in range(800):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                assert detail.status_code == 200
                if detail.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert detail.json()["status"] == "completed"
            assert len(detail.json()["completed_steps"]) == 4

    async def test_list_and_get_runs(self):
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            await client.post(
                "/api/autonomous/runs",
                json={"goal": "目标A", "acceptance_criteria": ["a"]},
            )
            resp = await client.get("/api/autonomous/runs")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

    async def test_cross_user_404(self):
        service = _make_service(_FakeDB())
        app_u1 = self._app(service, user="u1")
        async with await self._client(app_u1) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "测试目标", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]

        app_u2 = self._app(service, user="u2")
        async with await self._client(app_u2) as client:
            resp = await client.get(f"/api/autonomous/runs/{run_id}")
            assert resp.status_code == 404

    async def test_service_disabled_503(self):
        from fastapi import FastAPI

        app = FastAPI()
        app.state.autonomous_service = None  # 未初始化
        from auth.deps import get_current_user

        async def override_current_user():
            return "u1"

        app.dependency_overrides[get_current_user] = override_current_user
        from api.autonomous import router as autonomous_router

        app.include_router(autonomous_router)
        async with await self._client(app) as client:
            resp = await client.get("/api/autonomous/runs")
            assert resp.status_code == 503

    async def test_cancel_endpoint(self):
        service = _slow_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "取消演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]

            # 等运行中再取消
            for _ in range(500):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "running":
                    break
                await asyncio.sleep(0.01)

            cancel = await client.post(f"/api/autonomous/runs/{run_id}/cancel")
            assert cancel.status_code == 200
            assert cancel.json()["status"] == "cancel_requested"

            for _ in range(800):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "canceled":
                    break
                await asyncio.sleep(0.01)
            assert detail.json()["status"] == "canceled"

    async def test_approve_and_resume_endpoint(self):
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "审批演示", "acceptance_criteria": ["完成动作"], "tool_chain": ["send_message"]},
            )
            run_id = resp.json()["run_id"]

            for _ in range(500):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "waiting_approval":
                    break
                await asyncio.sleep(0.01)
            assert detail.json()["status"] == "waiting_approval"
            approval_id = detail.json()["pending_approvals"][0]["approval_id"]

            approved = await client.post(f"/api/autonomous/approvals/{approval_id}/approve")
            assert approved.status_code == 200

            resumed = await client.post(f"/api/autonomous/runs/{run_id}/resume")
            assert resumed.status_code == 200

            for _ in range(800):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] in ("stopped", "completed"):
                    break
                await asyncio.sleep(0.01)
            assert detail.json()["status"] in ("stopped", "completed")
            assert len(detail.json()["completed_steps"]) == 1

    async def test_reject_endpoint(self):
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "审批演示", "acceptance_criteria": ["完成动作"], "tool_chain": ["send_message"]},
            )
            run_id = resp.json()["run_id"]

            for _ in range(500):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "waiting_approval":
                    break
                await asyncio.sleep(0.01)
            approval_id = detail.json()["pending_approvals"][0]["approval_id"]

            rejected = await client.post(f"/api/autonomous/approvals/{approval_id}/reject")
            assert rejected.status_code == 200
            assert rejected.json()["status"] == "rejected"

            detail = await client.get(f"/api/autonomous/runs/{run_id}")
            assert detail.json()["pending_approvals"][0]["status"] == "rejected"

    async def test_sse_events_stream(self):
        """SSE：完成后的 run 事件流含全部事件 + done 收尾事件。"""
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "SSE 演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]
            for _ in range(800):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)

            async with client.stream("GET", f"/api/autonomous/runs/{run_id}/events") as stream:
                assert stream.status_code == 200
                assert stream.headers.get("content-type", "").startswith("text/event-stream")
                body = ""
                async for chunk in stream.aiter_text():
                    body += chunk
            assert "event: run_created" in body
            assert "event: run_finished" in body
            assert "event: done" in body
            assert "sequence" in body and "schema_version" in body

    async def test_sse_last_event_id(self):
        """SSE Last-Event-ID：从指定 sequence 续传。"""
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "SSE 演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]
            for _ in range(800):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)

            async with client.stream(
                "GET", f"/api/autonomous/runs/{run_id}/events", headers={"Last-Event-ID": "1"}
            ) as stream:
                body = ""
                async for chunk in stream.aiter_text():
                    body += chunk
            # 从 sequence 1 续传：不应包含 sequence=1 的事件（id: 1）
            assert "id: 1\n" not in body
            assert "event: done" in body

    async def test_trace_endpoint(self):
        """统一追溯报告端点：返回 7 问 answers 与清单指纹。"""
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "追溯演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]
            for _ in range(800):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.01)

            trace = await client.get(f"/api/autonomous/runs/{run_id}/trace")
            assert trace.status_code == 200, trace.text
            body = trace.json()
            assert body["run_id"] == run_id
            assert body["manifest_saved"] is True
            assert body["code_revision"]  # 冻结的代码版本
            assert body["tool_registry_version"]
            assert set(body["answers"].keys()) == {
                "mode_and_model",
                "version_manifest",
                "step_decisions",
                "evidence_coverage",
                "token_cost_time",
                "errors_and_recovery",
                "final_outcome",
            }

    async def test_trace_other_user_404(self):
        """跨用户访问追溯报告返回 404（多租户隔离）。"""
        service = _make_service(_FakeDB())
        app = self._app(service, user="u1")
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "隔离演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]
            other_app = self._app(service, user="u2")
            async with await self._client(other_app) as other:
                resp = await other.get(f"/api/autonomous/runs/{run_id}/trace")
                assert resp.status_code == 404

    async def test_recovery_suggestion_endpoint(self):
        """恢复建议端点：从故障状态输出确定性恢复动作（含脱敏上下文）。"""
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "恢复建议演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]
            # 构造预算耗尽故障状态（模拟运行中故障落盘）
            state = await service.get_run(run_id, "u1")
            failed = state.model_copy(
                update={
                    "status": RuntimeStatus.BUDGET_EXCEEDED,
                    "reason_code": "budget_exceeded",
                    "usage": BudgetUsage(steps=3, retries=2, consecutive_failures=3),
                    "decision_summaries": [
                        DecisionSummary(
                            step_id="step-3",
                            phase="execute",
                            action="retry",
                            tool_name="export_articles_csv",
                            outcome="failed",
                            reason="budget_exceeded",
                        )
                    ],
                }
            )
            await service.store.save(failed)

            sug = await client.get(f"/api/autonomous/runs/{run_id}/recovery-suggestion")
            assert sug.status_code == 200, sug.text
            body = sug.json()
            assert body["run_id"] == run_id
            assert body["status"] == "budget_exceeded"
            assert body["context"]["category"] == "budget_exhausted"
            assert body["context"]["error_code"] == "budget_exceeded"
            assert body["context"]["tool_name"] == "export_articles_csv"
            assert body["decision"]["action"] in ("continue_partial", "stop_failed")
            assert body["decision"]["reason"]

    async def test_recovery_suggestion_other_user_404(self):
        """跨用户访问恢复建议返回 404（多租户隔离）。"""
        service = _make_service(_FakeDB())
        app = self._app(service, user="u1")
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={"goal": "隔离演示", "acceptance_criteria": ["a"]},
            )
            run_id = resp.json()["run_id"]
            other_app = self._app(service, user="u2")
            async with await self._client(other_app) as other:
                resp = await other.get(f"/api/autonomous/runs/{run_id}/recovery-suggestion")
                assert resp.status_code == 404

    async def test_approval_includes_impact_scope(self):
        """审批项对外暴露脱敏影响范围（不含参数原文）。"""
        service = _make_service(_FakeDB())
        app = self._app(service)
        async with await self._client(app) as client:
            resp = await client.post(
                "/api/autonomous/runs",
                json={
                    "goal": "审批演示",
                    "acceptance_criteria": ["完成动作"],
                    "tool_chain": ["send_message"],
                },
            )
            run_id = resp.json()["run_id"]
            for _ in range(500):
                detail = await client.get(f"/api/autonomous/runs/{run_id}")
                if detail.json()["status"] == "waiting_approval":
                    break
                await asyncio.sleep(0.01)
            assert detail.json()["status"] == "waiting_approval"
            approvals = detail.json()["pending_approvals"]
            assert approvals[0]["impact_scope"]
            assert "send_message" in approvals[0]["impact_scope"]


class TestRunManifestFrozen:
    """RunManifest 接线：create_run 冻结启动前清单；清单失败不得启动。"""

    @pytest.mark.asyncio
    async def test_create_run_freezes_manifest(self):
        from agent.run_manifest import ExecutionMode, RunManifestStore

        db = _FakeDB()
        service = _make_service(db)
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])

        manifest = await RunManifestStore(db=db).load(state.run_id)
        assert manifest is not None
        assert manifest.run_id == state.run_id
        assert manifest.user_id == "u1"
        assert manifest.execution_mode == ExecutionMode.AUTONOMOUS
        assert manifest.code_revision  # 非空（dev-local 或部署注入）
        assert manifest.tool_registry_version  # 非空（默认 1.0）
        assert manifest.budget.max_steps > 0
        assert manifest.acceptance_criteria == ["a"]
        # 清单冻结为不可变
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            manifest.run_id = "other"

    @pytest.mark.asyncio
    async def test_create_run_manifest_failure_aborts(self):
        db = _FakeDB()
        col = _FakeCol()

        async def _boom_replace(*args, **kwargs):
            raise RuntimeError("manifest write failed")

        col.replace_one = _boom_replace
        db._cols["runtime_manifests"] = col
        service = _make_service(db)

        with pytest.raises(RuntimeError, match="manifest write failed"):
            await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        # state 未被保存（清单失败即中止，不启动 Agent）
        assert db["runtime_runs"].docs == []


class TestServiceMetrics:
    """AutonomousRunService 喂生产指标：run 终态与事件接入 MetricCollector。"""

    @pytest.mark.asyncio
    async def test_run_feeds_metrics_and_alert_view(self):
        from agent.harness.observability import MetricCollector, production_alert_metrics

        db = _FakeDB()
        collector = MetricCollector()
        service = AutonomousRunService(
            store=RuntimeStateStore(db),
            event_store=RuntimeEventStore(db, expires_days=30),
            policy=PolicyEngine(),
            approval_service=ApprovalService(db=None, ttl_seconds=1800),
            db=db,
            metrics=collector,
        )
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)

        assert collector.counter("run_finished_total") == 1
        assert collector.counter("run_completed_total") == 1
        assert collector.counter("steps_total") > 0  # demo 确定性执行器 4 步
        assert collector.histogram("run_duration_seconds").count == 1

        # 告警规则输入键全部有生产者（含派生指标）
        view = production_alert_metrics(collector)
        assert set(view) == {
            "unsafe_action_count",
            "duplicate_side_effect_count",
            "error_rate_delta_pp",
            "latency_p95_seconds",
            "usd_per_success_budget_delta_pct",
            "budget_exhaustion_rate",
            "quality_spot_below_gate",
            "stuck_running_seconds",
        }

    @pytest.mark.asyncio
    async def test_metrics_optional_noop(self):
        """metrics 未注入时运行不受影响（None 短路）。"""
        service = _make_service(_FakeDB())
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)


class TestOutboxFeeding:
    """Outbox 生产接线：运行时事件先入 outbox（幂等），投递到事件存储。"""

    @pytest.mark.asyncio
    async def test_run_events_flow_through_outbox(self):
        from agent.outbox import OutboxStatus

        db = _FakeDB()
        service = _make_service(db)
        state = await service.create_run(user_id="u1", goal="目标", acceptance_criteria=["a"])
        await service.start_run(state.run_id, "u1")
        await _wait_status(service, state.run_id, "u1", RuntimeStatus.COMPLETED)

        # 事件经 outbox 入队并全部投递（SENT），无 pending 残留
        docs = db["event_outbox"].docs
        assert docs, "事件应经 outbox 入队"
        for doc in docs:
            assert doc["status"] == OutboxStatus.SENT.value
            assert doc["run_id"] == state.run_id

        # 事件存储收到事件（SSE 数据源）
        events = await service.events(state.run_id, "u1")
        types = [e.event_type for e in events]
        assert "run_created" in types
        assert "run_finished" in types

    @pytest.mark.asyncio
    async def test_outbox_dedup_idempotent(self):
        from agent.outbox import EventOutbox, OutboxStore

        db = _FakeDB()
        outbox = EventOutbox(store=OutboxStore(db=db))
        first = await outbox.enqueue_run_event(
            run_id="r1", event_type="step_failed", payload={"step_id": "s1"}
        )
        second = await outbox.enqueue_run_event(
            run_id="r1", event_type="step_failed", payload={"step_id": "s1"}
        )
        assert first is not None
        assert second is None  # 幂等去重：0 重复副作用
