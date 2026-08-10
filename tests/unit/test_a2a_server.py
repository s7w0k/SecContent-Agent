"""PR-4B-02 测试：A2A Server 与 Agent Card（Server API 契约测试）。

覆盖 spec 4B-2 / 4B-6（Server API 契约测试）：
  - Agent Card：只发布真实开放能力，protocol_version=1.0；
  - Message Send：净化 → 能力门禁 → PolicyEngine → 创建内部 run → 状态映射；
  - 幂等：同 task_id 重复 Send 返回既有任务，不重复创建；
  - 不可信输入净化失败 / 未实现能力 / 协议版本错误 → 明确协议错误（400/501）；
  - 多租户：跨 principal 访问返回 404；
  - Tasks Query / List / Get / Cancel；
  - Subscribe / Resubscribe / Message Stream：SSE 事件流 + Last-Event-ID 续传；
  - 服务未启用（a2a_server=None）返回 503。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from agent.a2a.models import AGENT_CARD_PATH, PROTOCOL_VERSION, Skill
from agent.a2a.server import A2AServer
from agent.a2a.task_store import A2ATaskStore
from agent.autonomous_service import AutonomousRunService
from agent.policy_engine import ApprovalService, PolicyEngine
from agent.runtime_events import RuntimeEventStore
from agent.runtime_store import RuntimeStateStore

FIXED_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════
# Fake Mongo（完整版：支持 sequence 排序 / $gt / $in / $set / $inc）
# ═══════════════════════════════════════════════════════════════


def _match(doc: dict, query: dict) -> bool:
    for k, v in (query or {}).items():
        if isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$gt" in v and not (doc.get(k, 0) > v["$gt"]):
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


def _make_service(db, *, slow=False) -> AutonomousRunService:
    if slow:

        async def _slow_executor(state, action, meta: dict):
            await asyncio.sleep(0.2)
            if action.tool_name == "export_articles_csv":
                return {"ok": True, "evidence": [{"acceptance_index": 0}], "duration_ms": 5}
            return {"ok": True, "duration_ms": 5}

        return AutonomousRunService(
            store=RuntimeStateStore(db),
            event_store=RuntimeEventStore(db, expires_days=30),
            policy=PolicyEngine(),
            approval_service=ApprovalService(db=None, ttl_seconds=1800),
            db=db,
            executor_factory=lambda state: _slow_executor,
        )
    return AutonomousRunService(
        store=RuntimeStateStore(db),
        event_store=RuntimeEventStore(db, expires_days=30),
        policy=PolicyEngine(),
        approval_service=ApprovalService(db=None, ttl_seconds=1800),
        db=db,
    )


def _make_server(db=None, *, slow=False) -> A2AServer:
    db = db or _FakeDB()
    return A2AServer(
        run_service=_make_service(db, slow=slow),
        task_store=A2ATaskStore(db),
        skills=[
            Skill(
                id="pr_intel",
                name="PR 情报分析",
                description="只读情报分析",
                tags=["read-only"],
            ),
        ],
        card_url=f"http://test{AGENT_CARD_PATH}",
    )


class TestA2AAPI:
    def _app(self, server, *, user="u1"):
        from api.a2a import router as a2a_router
        from auth.deps import get_current_user
        from fastapi import FastAPI

        app = FastAPI()
        app.state.a2a_server = server

        async def override_current_user():
            return user

        app.dependency_overrides[get_current_user] = override_current_user
        app.include_router(a2a_router)
        return app

    async def _client(self, app):
        from httpx import ASGITransport, AsyncClient

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    def _headers(self, **extra):
        headers = {"A2A-Version": PROTOCOL_VERSION}
        headers.update(extra)
        return headers

    async def _wait_status(self, client, task_id, target, timeout=8.0):
        deadline = time.monotonic() + timeout
        last = "?"
        while time.monotonic() < deadline:
            resp = await client.get(f"/a2a/tasks/{task_id}", headers=self._headers())
            assert resp.status_code == 200, resp.text
            status = resp.json()["status"]
            last = status
            if status == target or status in ("COMPLETED", "FAILED", "CANCELED", "REJECTED"):
                return status
            await asyncio.sleep(0.01)
        raise AssertionError(f"timeout waiting for {target}, last={last}")

    def _msg(self, text, *, task_id="t1", message_id="m1", skill_id="pr_intel", **meta):
        body = {
            "message_id": message_id,
            "task_id": task_id,
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "context_id": "ctx-1",
            "metadata": dict(meta, skill_id=skill_id),
        }
        return body

    # ── Agent Card ──────────────────────────────────────────

    async def test_agent_card_declares_offered_skills(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.get(AGENT_CARD_PATH)
            assert resp.status_code == 200, resp.text
            card = resp.json()
            assert card["protocol_version"] == PROTOCOL_VERSION == "1.0"
            assert card["name"]
            assert card["url"] == f"http://test{AGENT_CARD_PATH}"
            assert [s["id"] for s in card["skills"]] == ["pr_intel"]
            # 只发布真实开放能力：不包含未实现 skill
            assert all("read-only" in s.get("tags", []) for s in card["skills"])

    # ── Message Send ────────────────────────────────────────

    async def test_send_and_poll_to_complete(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.post(
                "/a2a/message/send",
                json=self._msg("分析近 7 天 PR 情报"),
                headers=self._headers(),
            )
            assert resp.status_code == 200, resp.text
            result = resp.json()
            task = result["task"]
            assert task is not None
            assert task["id"] == "t1"
            assert task["status"] in ("SUBMITTED", "WORKING")
            assert task["internal_run_id"]
            assert task["context_id"] == "ctx-1"
            # 双向追溯：context_id ↔ thread_id
            assert task["metadata"]["run_id"] == task["internal_run_id"]

            status = await self._wait_status(client, "t1", "COMPLETED")
            assert status == "COMPLETED"
            detail = (await client.get("/a2a/tasks/t1", headers=self._headers())).json()
            assert detail["status"] == "COMPLETED"
            assert len(detail["history"]) >= 1  # 决策摘要 → history 消息

    async def test_send_idempotent_same_task(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            body = self._msg("分析情报")
            first = (
                await client.post("/a2a/message/send", json=body, headers=self._headers())
            ).json()
            second = (
                await client.post("/a2a/message/send", json=body, headers=self._headers())
            ).json()
            assert first["task"]["id"] == second["task"]["id"] == "t1"
            # 幂等：同一 task_id 复用同一内部 run
            assert first["task"]["internal_run_id"] == second["task"]["internal_run_id"]

    async def test_send_credential_input_rejected_400(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.post(
                "/a2a/message/send",
                json=self._msg("我的 api_key=sk-abc 需要替换"),
                headers=self._headers(),
            )
            assert resp.status_code == 400
            assert resp.json()["detail"]["error"]["code"] == "invalid_input"

    async def test_send_unknown_skill_501(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.post(
                "/a2a/message/send",
                json=self._msg("分析", skill_id="not_offered"),
                headers=self._headers(),
            )
            assert resp.status_code == 501  # 未实现能力：明确协议错误，不伪装成功
            assert resp.json()["detail"]["error"]["code"] == "method_not_implemented"

    async def test_missing_version_header_400(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.post("/a2a/message/send", json=self._msg("分析"))
            assert resp.status_code == 400
            assert resp.json()["detail"]["error"]["code"] == "version_error"

    async def test_wrong_version_header_400(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.post(
                "/a2a/message/send",
                json=self._msg("分析"),
                headers={"A2A-Version": "9.9"},
            )
            assert resp.status_code == 400

    # ── 多租户隔离 ──────────────────────────────────────────

    async def test_cross_principal_404(self):
        server = _make_server()
        app_u1 = self._app(server, user="u1")
        async with await self._client(app_u1) as client:
            resp = await client.post(
                "/a2a/message/send", json=self._msg("分析"), headers=self._headers()
            )
            assert resp.status_code == 200
        # 同一 server，另一个 principal 读取 → 404（A2A 多租户隔离）
        app_u2 = self._app(server, user="u2")
        async with await self._client(app_u2) as client:
            resp = await client.get("/a2a/tasks/t1", headers=self._headers())
            assert resp.status_code == 404

    # ── Tasks Query / List / Cancel ─────────────────────────

    async def test_tasks_query_filter(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            await client.post(
                "/a2a/message/send", json=self._msg("分析 A"), headers=self._headers()
            )
            resp = await client.post(
                "/a2a/tasks/query", json={"status": "SUBMITTED"}, headers=self._headers()
            )
            assert resp.status_code == 200, resp.text
            assert [t["id"] for t in resp.json()["tasks"]] == ["t1"]

    async def test_tasks_get_not_found(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.get("/a2a/tasks/nope", headers=self._headers())
            assert resp.status_code == 404

    async def test_cancel_task(self):
        app = self._app(_make_server(slow=True))
        async with await self._client(app) as client:
            resp = await client.post(
                "/a2a/message/send", json=self._msg("慢速分析"), headers=self._headers()
            )
            assert resp.status_code == 200
            # 等待首轮检查点落库（WORKING）后再取消：RUNNING 才允许 cancel_requested
            await self._wait_status(client, "t1", "WORKING")
            cancel = await client.post("/a2a/tasks/t1/cancel", headers=self._headers())
            assert cancel.status_code == 200, cancel.text
            status = await self._wait_status(client, "t1", "CANCELED")
            assert status == "CANCELED"

    # ── Subscribe / Stream（SSE） ───────────────────────────

    async def _read_sse(self, client, method, url, **kwargs):
        """读取完整 SSE 流，返回 (原始文本, 事件名列表)。"""
        chunks: list[str] = []
        event_names: list[str] = []
        async with client.stream(method, url, **kwargs) as resp:
            assert resp.status_code == 200, await resp.aread()
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    event_names.append(line[len("event: ") :])
                chunks.append(line)
        return "\n".join(chunks), event_names

    async def test_resubscribe_sse_stream(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            await client.post("/a2a/message/send", json=self._msg("分析"), headers=self._headers())
            await self._wait_status(client, "t1", "COMPLETED")
            text, names = await self._read_sse(
                client, "POST", "/a2a/tasks/t1/resubscribe", headers=self._headers()
            )
            assert "task_status_update" in names
            assert "done" in names
            assert "A2A-Version" in text or "COMPLETED" in text

    async def test_resubscribe_last_event_id_resume(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            await client.post("/a2a/message/send", json=self._msg("分析"), headers=self._headers())
            await self._wait_status(client, "t1", "COMPLETED")
            # 全量读取并记录最后一条 sequence
            full, _ = await self._read_sse(
                client, "POST", "/a2a/tasks/t1/resubscribe", headers=self._headers()
            )
            seqs = [
                int(line.split(":", 1)[1].strip())
                for line in full.splitlines()
                if line.startswith("id: ")
            ]
            assert seqs == sorted(seqs)
            assert seqs
            last = seqs[-1]
            # 从 last 续传：只应收到 last 之后的事件
            tail, _ = await self._read_sse(
                client,
                "POST",
                "/a2a/tasks/t1/resubscribe",
                headers=self._headers(**{"Last-Event-ID": str(last)}),
            )
            tail_seqs = [
                int(line.split(":", 1)[1].strip())
                for line in tail.splitlines()
                if line.startswith("id: ")
            ]
            assert all(s > last for s in tail_seqs)

    async def test_message_stream_sse(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            text, names = await self._read_sse(
                client,
                "POST",
                "/a2a/message/stream",
                json=self._msg("流式分析"),
                headers=self._headers(),
            )
            assert "task_status_update" in names
            assert "done" in names
            # 事件流里能观察到 COMPLETED 状态
            assert "COMPLETED" in text

    async def test_resubscribe_unknown_task_404(self):
        app = self._app(_make_server())
        async with await self._client(app) as client:
            resp = await client.post("/a2a/tasks/nope/resubscribe", headers=self._headers())
            assert resp.status_code == 404

    # ── 服务未启用 ──────────────────────────────────────────

    async def test_disabled_server_503(self):
        app = self._app(None)  # a2a_server=None
        async with await self._client(app) as client:
            resp = await client.get(AGENT_CARD_PATH)
            assert resp.status_code == 503
