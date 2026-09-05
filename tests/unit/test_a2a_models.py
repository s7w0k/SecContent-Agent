"""PR-4B-01 测试：A2A 模型 / 状态映射 / 输入净化 / Task Store。

覆盖 spec 4B-1（定义模型和映射）：
  - 八种 TaskStatus 与终态集合；
  - RuntimeStatus ↔ TaskStatus 双向映射（含边界：WAITING_APPROVAL→INPUT_REQUIRED、
    STOPPED→FAILED、REJECTED→STOPPED；未知状态抛 ProtocolError）；
  - Agent Card 能力声明；
  - Part 按 kind 校验、TaskSendResult 互斥校验；
  - 不可信输入净化（文本超长 / 凭证关键字 / 恶意正则 / file: URI / 消息超字节 / 超 Part 数）；
  - Task ↔ RuntimeState 双向追溯字段（internal_run_id / context_id / message_id）；
  - Task Store：版本乐观锁防旧覆盖、幂等创建/更新、事件投递去重、多租户隔离、终态不可逆。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from agent.a2a.mapper import (
    build_denied_task,
    map_runtime_to_task,
    map_state_to_task,
    map_task_to_runtime,
    validate_external_input,
)
from agent.a2a.models import (
    AGENT_CARD_PATH,
    PROTOCOL_VERSION,
    SDK_VERSION,
    TERMINAL_TASK_STATUSES,
    VERSION_HEADER,
    AgentCard,
    InvalidInputError,
    Message,
    Part,
    ProtocolError,
    Skill,
    Task,
    TaskSendResult,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from agent.a2a.task_store import A2ATaskConflictError, A2ATaskStore
from agent.runtime_state import (
    DecisionSummary,
    EvidenceRecord,
    RuntimeState,
    RuntimeStatus,
)

FIXED_NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════
# Fake Mongo（与 test_autonomous_api.py 同款，支持 $set / $inc）
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


def _make_store(db=None) -> A2ATaskStore:
    return A2ATaskStore(db or _FakeDB())


def _task(
    task_id: str = "a2a-1",
    status: TaskStatus = TaskStatus.SUBMITTED,
    created_at: datetime = FIXED_NOW,
) -> Task:
    return Task(
        id=task_id,
        status=status,
        created_timestamp=created_at,
        last_updated_timestamp=created_at,
    )


# ═══════════════════════════════════════════════════════════════
# 协议常量 / 模型
# ═══════════════════════════════════════════════════════════════


class TestProtocolModels:
    def test_protocol_constants(self):
        assert PROTOCOL_VERSION == "1.0"
        assert VERSION_HEADER == "A2A-Version"
        assert AGENT_CARD_PATH == "/.well-known/agent-card.json"
        assert SDK_VERSION.startswith("a2a-sdk ")

    def test_eight_task_statuses(self):
        values = {s.value for s in TaskStatus}
        assert values == {
            "SUBMITTED",
            "WORKING",
            "INPUT_REQUIRED",
            "AUTH_REQUIRED",
            "COMPLETED",
            "FAILED",
            "CANCELED",
            "REJECTED",
        }

    def test_terminal_task_statuses(self):
        assert TaskStatus.REJECTED in TERMINAL_TASK_STATUSES
        assert TaskStatus.COMPLETED in TERMINAL_TASK_STATUSES
        assert TaskStatus.WORKING not in TERMINAL_TASK_STATUSES
        assert TaskStatus.INPUT_REQUIRED not in TERMINAL_TASK_STATUSES

    def test_agent_card_capability_declaration(self):
        card = AgentCard(
            name="PR情报智能体",
            description="PR 情报分析",
            url="http://agent.local/.well-known/agent-card.json",
            skills=[
                Skill(id="pr_intel", name="PR 情报分析", description="检索与总结"),
            ],
        )
        assert card.protocol_version == PROTOCOL_VERSION == "1.0"
        assert card.skills[0].id == "pr_intel"
        assert card.skills[0].input_modes == ["text"]
        assert card.default_input_modes == ["text"]
        # 未声明能力不得静默伪装：Agent Card 只包含真实声明
        assert [s.id for s in card.skills] == ["pr_intel"]

    def test_part_kind_validation(self):
        with pytest.raises(ValueError):
            Part(kind="text")  # text 必须携带 text
        with pytest.raises(ValueError):
            Part(kind="file")  # file 必须携带 uri 或 name
        with pytest.raises(ValueError):
            Part(kind="data")  # data 必须携带 data
        assert Part(kind="text", text="hi").text == "hi"
        assert Part(kind="file", uri="https://x.example/a.pdf").uri
        assert Part(kind="data", data={"k": 1}).data

    def test_task_send_result_exactly_one(self):
        with pytest.raises(ValueError):
            TaskSendResult()
        with pytest.raises(ValueError):
            TaskSendResult(task=_task(), message=Message(message_id="m1"))
        assert TaskSendResult(task=_task()).task is not None
        assert TaskSendResult(message=Message(message_id="m1")).message is not None

    def test_status_update_event_cursor(self):
        event = TaskStatusUpdateEvent(
            event_id="e1", task_id="a2a-1", status=TaskStatus.WORKING, timestamp=FIXED_NOW
        )
        assert event.task_id == "a2a-1"
        assert event.status == TaskStatus.WORKING


# ═══════════════════════════════════════════════════════════════
# RuntimeStatus ↔ TaskStatus 映射
# ═══════════════════════════════════════════════════════════════


class TestStatusMapping:
    def test_boundary_mappings(self):
        # WAITING_APPROVAL → INPUT_REQUIRED
        assert map_runtime_to_task(RuntimeStatus.WAITING_APPROVAL) == TaskStatus.INPUT_REQUIRED
        # STOPPED → FAILED（策略熔断对外表现为失败）
        assert map_runtime_to_task(RuntimeStatus.STOPPED) == TaskStatus.FAILED
        assert map_runtime_to_task(RuntimeStatus.BUDGET_EXCEEDED) == TaskStatus.FAILED
        # CANCEL_REQUESTED 在安全点停止前对外仍是 WORKING
        assert map_runtime_to_task(RuntimeStatus.CANCEL_REQUESTED) == TaskStatus.WORKING
        assert map_runtime_to_task(RuntimeStatus.PENDING) == TaskStatus.SUBMITTED
        assert map_runtime_to_task(RuntimeStatus.RUNNING) == TaskStatus.WORKING
        # REJECTED → STOPPED（拒绝映射为策略熔断终态）
        assert map_task_to_runtime(TaskStatus.REJECTED) == RuntimeStatus.STOPPED
        assert map_task_to_runtime(TaskStatus.AUTH_REQUIRED) == RuntimeStatus.WAITING_APPROVAL
        assert map_task_to_runtime(TaskStatus.WORKING) == RuntimeStatus.RUNNING
        assert map_task_to_runtime(TaskStatus.SUBMITTED) == RuntimeStatus.PENDING

    def test_all_statuses_mappable(self):
        # 覆盖完备性：八种 TaskStatus 与全部 RuntimeStatus 均不抛错
        for rs in RuntimeStatus:
            assert map_runtime_to_task(rs) in TaskStatus
        for ts in TaskStatus:
            assert map_task_to_runtime(ts) in RuntimeStatus

    def test_round_trip_stable(self):
        for ts in (
            TaskStatus.SUBMITTED,
            TaskStatus.WORKING,
            TaskStatus.INPUT_REQUIRED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        ):
            assert map_runtime_to_task(map_task_to_runtime(ts)) == ts

    def test_unknown_status_raises_protocol_error(self):
        with pytest.raises(ProtocolError):
            map_runtime_to_task("no_such_status")
        with pytest.raises(ProtocolError):
            map_task_to_runtime("no_such_status")

    def test_task_to_runtime_state_traceability(self):
        state = RuntimeState(
            run_id="run-1",
            thread_id="thr-9",
            user_id="u1",
            goal="目标",
            status=RuntimeStatus.COMPLETED,
            completed_steps=["s1", "s2"],
            evidence=[
                EvidenceRecord(
                    evidence_id="ev-1", kind="artifact", note="证据说明", acceptance_index=0
                ),
                EvidenceRecord(evidence_id="ev-2", kind="", note="无 kind 跳过"),
            ],
            decision_summaries=[
                DecisionSummary(
                    step_id="st-1", phase="plan", action="规划检索方案", outcome="success"
                ),
                DecisionSummary(
                    step_id="st-2",
                    phase="execute",
                    action="调用 retrieve_articles",
                    outcome="success",
                    reason="命中缓存",
                ),
            ],
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
        )
        task = map_state_to_task(state, task_id="a2a-task-1")
        # 双向追溯：internal_run_id / context_id
        assert task.id == "a2a-task-1"
        assert task.internal_run_id == "run-1"
        assert task.context_id == "thr-9"
        assert task.status == TaskStatus.COMPLETED
        # artifacts 仅来自有 kind 的证据（脱敏摘要）
        assert len(task.artifacts) == 1
        assert task.artifacts[0].artifact_id == "ev-1"
        assert task.artifacts[0].name == "artifact"
        assert "证据说明" in task.artifacts[0].description
        # history 来自决策摘要：message_id = dec-<step_id>
        assert len(task.history) == 2
        assert task.history[0].message_id == "dec-st-1"
        assert task.history[0].role == "agent"
        assert task.history[1].metadata["outcome"] == "success"
        assert task.metadata["run_id"] == "run-1"

    def test_map_waiting_approval_state(self):
        state = RuntimeState(
            run_id="run-2", thread_id="t", user_id="u1", status=RuntimeStatus.WAITING_APPROVAL
        )
        task = map_state_to_task(state, task_id="a2a-task-2")
        assert task.status == TaskStatus.INPUT_REQUIRED

    def test_build_denied_task(self):
        denied = build_denied_task("a2a-denied", "不可信输入被拒绝")
        assert denied.id == "a2a-denied"
        assert denied.status == TaskStatus.REJECTED
        assert denied.status in TERMINAL_TASK_STATUSES
        assert "不可信输入" in denied.metadata["error"]


# ═══════════════════════════════════════════════════════════════
# 不可信输入净化
# ═══════════════════════════════════════════════════════════════


class TestInputValidation:
    def test_valid_message_passes(self):
        msg = Message(
            message_id="m1",
            task_id="a2a-1",
            parts=[Part(kind="text", text="hello world")],
        )
        validate_external_input(msg)  # 不抛错

    def test_data_part_passes(self):
        msg = Message(message_id="m2", parts=[Part(kind="data", data={"n": 1})])
        validate_external_input(msg)

    def test_credential_keyword_rejected(self):
        msg = Message(message_id="m3", parts=[Part(kind="text", text="使用 api_key=sk-abc 调用")])
        with pytest.raises(InvalidInputError):
            validate_external_input(msg)

    def test_malicious_pattern_rejected(self):
        msg = Message(message_id="m4", parts=[Part(kind="text", text="<script>alert(1)</script>")])
        with pytest.raises(InvalidInputError):
            validate_external_input(msg)

    def test_file_uri_rejected(self):
        msg = Message(message_id="m5", parts=[Part(kind="file", uri="file:///etc/passwd")])
        with pytest.raises(InvalidInputError):
            validate_external_input(msg)

    def test_http_file_allowed(self):
        msg = Message(
            message_id="m6",
            parts=[Part(kind="file", uri="https://example.com/PR.pdf", name="PR.pdf")],
        )
        validate_external_input(msg)

    def test_message_too_large_bytes(self):
        msg = Message(message_id="m7", parts=[Part(kind="text", text="x" * 4096)])
        with pytest.raises(InvalidInputError):
            validate_external_input(msg, max_bytes=1024)

    def test_too_many_parts(self):
        msg = Message(message_id="m8", parts=[Part(kind="text", text="x") for _ in range(70)])
        with pytest.raises(InvalidInputError):
            validate_external_input(msg, max_parts=64)

    def test_aggregate_text_too_long(self):
        # 单个 Part 受 200k 上限约束，聚合超限由校验层兜底
        msg = Message(
            message_id="m9",
            parts=[Part(kind="text", text="a" * 150_000) for _ in range(2)],
        )
        with pytest.raises(InvalidInputError):
            validate_external_input(msg)

    def test_task_id_too_long(self):
        # Message 字段层已限制 100 字符；此处用 model_construct 绕过模型校验，
        # 验证 mapper 的防御性二次检查确实生效。
        msg = Message.model_construct(
            message_id="m10", task_id="x" * 101, parts=[Part(kind="text", text="hi")]
        )
        with pytest.raises(InvalidInputError):
            validate_external_input(msg)


# ═══════════════════════════════════════════════════════════════
# Task Store：版本乐观锁 / 幂等 / 多租户 / 终态不可逆
# ═══════════════════════════════════════════════════════════════


class TestTaskStore:
    async def test_create_idempotent(self):
        store = _make_store()
        assert await store.create(_task("t1"), user_id="u1") is True
        assert await store.create(_task("t1"), user_id="u1") is False  # 幂等不覆盖

    async def test_save_cas_version_conflict(self):
        store = _make_store()
        task = _task("t1")
        await store.create(task, user_id="u1")
        # 版本不匹配：拒绝旧版本覆盖新状态
        newer = task.model_copy(update={"status": TaskStatus.WORKING})
        assert await store.save(newer, user_id="u1", expected_version=1) is True
        assert await store.save(task, user_id="u1", expected_version=1) is False
        loaded = await store.load("t1", user_id="u1")
        assert loaded is not None and loaded.status == TaskStatus.WORKING

    async def test_save_cross_user_rejected(self):
        store = _make_store()
        await store.create(_task("t1"), user_id="u1")
        with pytest.raises(A2ATaskConflictError):
            await store.save(_task("t1"), user_id="u2")

    async def test_load_multitenant_isolation(self):
        store = _make_store()
        await store.create(_task("t1"), user_id="u1")
        assert (await store.load("t1", user_id="u1")) is not None
        assert (await store.load("t1", user_id="u2")) is None

    async def test_load_by_run_id_reverse_lookup(self):
        store = _make_store()
        await store.create(
            _task("t1").model_copy(update={"internal_run_id": "run-9"}), user_id="u1"
        )
        found = await store.load_by_run_id("run-9", user_id="u1")
        assert found is not None and found.id == "t1"
        assert await store.load_by_run_id("run-9", user_id="u2") is None

    async def test_update_status_terminal_irreversible(self):
        store = _make_store()
        await store.create(_task("t1", status=TaskStatus.COMPLETED), user_id="u1")
        assert await store.update_status("t1", TaskStatus.WORKING, user_id="u1") is False
        # save 终态回退同样被拒
        assert await store.save(_task("t1", status=TaskStatus.WORKING), user_id="u1") is False

    async def test_update_status_idempotent(self):
        store = _make_store()
        await store.create(_task("t1"), user_id="u1")
        assert await store.update_status("t1", TaskStatus.WORKING, user_id="u1") is True
        assert await store.update_status("t1", TaskStatus.WORKING, user_id="u1") is False

    async def test_update_status_version_guard(self):
        store = _make_store()
        await store.create(_task("t1"), user_id="u1")
        assert (
            await store.update_status("t1", TaskStatus.WORKING, user_id="u1", expected_version=1)
            is True
        )
        assert (
            await store.update_status("t1", TaskStatus.COMPLETED, user_id="u1", expected_version=1)
            is False
        )
        assert (
            await store.update_status("t1", TaskStatus.COMPLETED, user_id="u1", expected_version=2)
            is True
        )

    async def test_update_status_cross_user_rejected(self):
        store = _make_store()
        await store.create(_task("t1"), user_id="u1")
        assert await store.update_status("t1", TaskStatus.WORKING, user_id="u2") is False

    async def test_record_event_dedup(self):
        store = _make_store()
        assert await store.record_event("t1", "e1") is True
        assert await store.record_event("t1", "e1") is False  # 重复投递去重
        assert await store.record_event("t1", "e2") is True
        assert await store.has_event("t1", "e1") is True
        assert await store.has_event("t1", "e2") is True
        assert await store.has_event("t1", "e3") is False

    async def test_list_tasks_filter(self):
        store = _make_store()
        await store.create(_task("a1", created_at=FIXED_NOW), user_id="u1")
        await store.create(
            _task(
                "a2",
                status=TaskStatus.WORKING,
                created_at=datetime(2026, 8, 10, 13, 0, 0, tzinfo=UTC),
            ),
            user_id="u1",
        )
        await store.create(_task("a3", created_at=FIXED_NOW), user_id="u2")
        assert [t.id for t in await store.list_tasks(user_id="u1")] == ["a2", "a1"]
        assert [t.id for t in await store.list_tasks(user_id="u1", status="WORKING")] == ["a2"]
        assert [t.id for t in await store.list_tasks(user_id="u2")] == ["a3"]
        assert await store.list_tasks(user_id="u9") == []
