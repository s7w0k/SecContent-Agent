"""OneShot Production Cutover — Critical Tests & Startup Matrix（计划 §56 / §59-69）。

覆盖：
  §59 Startup Matrix：legacy / skill_shadow / skill_canary / skill_planned 四模式全部通过。
  §60/61 Critical Test 1/2：skill_planned 不构造 PipelineManagerV2 / old MultiAgentRuntime。
  §62 Critical Test 3：legacy 模式匹配切机前行为。
  §63 Critical Test 4：Shadow 只读、绝不写生产。
  §64 Critical Test 5：Skill 失败绝不自动 fallback Legacy。
  §65/66 Critical Test 6/7：Retry / Resume 保持 selected_engine（sticky）。
  §67 Critical Test 8：Worker 与 Main 共用同一 production builder。
  §68 Critical Test 9：Worker skill_planned 装配出 skill_executor / skill_runtime / orchestration_runtime。
  §69 Critical Test 10：生产 Artifact 在 Worker 重启后持久（MongoArtifactStore）。

约定：全部使用内存桩，不发网络请求（遵守 CLAUDE.md 测试禁止事项）。
"""

from __future__ import annotations

from typing import Any

import pytest
from agent.execution.contracts import ExecutionRequest, ExecutionResult, WorkflowExecutor
from agent.execution.production_factory import build_production_execution_runtime
from agent.execution.startup_validation import (
    StartupValidationError,
    validate_runtime,
)

MODES = ("legacy", "skill_shadow", "skill_canary", "skill_planned")


# ══════════════════════════════════════════════════════════════
# 桩：Settings / Legacy 执行器 / Mongo 假库
# ══════════════════════════════════════════════════════════════

class _ModeSettings:
    """依据 mode 动态生成 Settings 桩。"""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    AGENT_SHADOW_SAMPLE_PERCENT = 100
    AGENT_SHADOW_TIMEOUT_SECONDS = 60
    AGENT_SKILL_CANARY_PERCENT = 100
    AGENT_CANARY_HASH_SEED = "seccontent-agent-v1"
    KNOWLEDGE_BACKEND = "wiki"
    MONGODB_URI = "mongodb://localhost:27017/test"
    MONGODB_DB = "test"

    @property
    def AGENT_EXECUTION_MODE(self) -> str:  # noqa: N802
        return self._mode


class _LegacyStub(WorkflowExecutor):
    """可配置行为的旧链执行器桩。"""

    def __init__(self, *, status: str = "SUCCEEDED", boom: bool = False) -> None:
        self._status = status
        self._boom = boom
        self.executed: list[ExecutionRequest] = []
        self.resumed: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self._boom:
            raise RuntimeError("legacy boom")
        self.executed.append(request)
        return ExecutionResult(
            engine="legacy",
            status=self._status,  # type: ignore[arg-type]
            artifact_refs=["art:legacy-1"],
        )

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        self.resumed.append(request)
        return ExecutionResult(
            engine="legacy",
            status=self._status,  # type: ignore[arg-type]
            artifact_refs=["art:legacy-1"],
        )


class _SkillBoom(WorkflowExecutor):
    """Skill 侧必然失败的执行器桩（验证绝不 fallback）。"""

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("skill boom")

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        raise RuntimeError("skill boom")


class _MemoryCollection:
    """最小 async 内存集合（满足 MongoArtifactStore / pipeline_tasks 用到的方法）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self._docs: list[dict[str, Any]] = []
        self._next_id = 0

    async def create_indexes(self, indexes: list[Any]) -> list[str]:
        return [str(i) for i in indexes]

    async def insert_one(self, doc: dict[str, Any]) -> dict[str, Any]:
        d = dict(doc)
        d["_id"] = self._next_id
        self._next_id += 1
        self._docs.append(d)
        return d

    def _match(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(doc.get(k) == v for k, v in query.items())

    async def find_one(
        self, query: dict[str, Any], sort: list[tuple[str, int]] | None = None, **_: Any
    ) -> dict[str, Any] | None:
        matched = [d for d in self._docs if self._match(d, query)]
        if not matched:
            return None
        if sort:
            for key, order in sort:
                rev = order < 0
                matched = sorted(matched, key=lambda d: d.get(key, 0), reverse=rev)
        return matched[0]

    async def find_one_and_update(self, query: dict[str, Any], update: dict[str, Any], **_: Any) -> dict[str, Any]:
        doc = await self.find_one(query)
        if doc is None:
            return {"retry_count": 0}
        fields = update.get("$set", {})
        doc.update(fields)
        return doc

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], **_: Any) -> None:
        doc = await self.find_one(query)
        if doc is not None:
            doc.update(update.get("$set", {}))


class _MemoryDB:
    """内存假库：按集合名返回内存集合，模拟 Mongo 句柄（跨实例共享内存模拟重启持久）。"""

    def __init__(self) -> None:
        self._collections: dict[str, _MemoryCollection] = {}

    def __getitem__(self, name: str) -> _MemoryCollection:
        if name not in self._collections:
            self._collections[name] = _MemoryCollection(name)
        return self._collections[name]


def _build(mode: str, *, legacy_executor: WorkflowExecutor | None, db: Any) -> Any:
    return build_production_execution_runtime(
        settings=_ModeSettings(mode),
        db=db,
        legacy_executor=legacy_executor,
    )


# ══════════════════════════════════════════════════════════════
# §59 Startup Matrix：四模式
# ══════════════════════════════════════════════════════════════

class TestStartupMatrix:
    def test_legacy_mode(self) -> None:
        rt = _build("legacy", legacy_executor=_LegacyStub(), db=_MemoryDB())
        assert rt.legacy_loaded is True
        assert rt.skill_loaded is False
        assert rt.skill_executor is None
        assert validate_runtime(rt, "legacy", executes_tasks=True) is True

    def test_skill_shadow_mode(self) -> None:
        rt = _build("skill_shadow", legacy_executor=_LegacyStub(), db=_MemoryDB())
        assert rt.legacy_loaded is True
        assert rt.skill_loaded is True
        assert rt.shadow_executor is not None
        assert rt.skill_runtime is not None
        # Shadow 使用只读 adapter（§63 / §129）：skill 侧绝不写生产
        assert rt.skill_runtime.default_adapter == "production_readonly"
        assert validate_runtime(rt, "skill_shadow", executes_tasks=True) is True

    def test_skill_canary_mode(self) -> None:
        rt = _build("skill_canary", legacy_executor=_LegacyStub(), db=_MemoryDB())
        assert rt.legacy_loaded is True
        assert rt.skill_loaded is True
        assert rt.rollout is not None
        assert validate_runtime(rt, "skill_canary", executes_tasks=True) is True

    def test_skill_planned_mode(self) -> None:
        rt = _build("skill_planned", legacy_executor=None, db=_MemoryDB())
        assert rt.legacy_loaded is False
        assert rt.skill_loaded is True
        assert rt.skill_executor is not None
        assert rt.skill_runtime is not None
        assert rt.orchestration_runtime is not None
        assert rt.artifact_store is not None
        assert validate_runtime(rt, "skill_planned", executes_tasks=True) is True

    def test_main_never_requires_legacy_executor(self) -> None:
        """FastAPI（executes_tasks=False）不强制 legacy_executor（计划 §35）。"""
        rt = _build("legacy", legacy_executor=None, db=_MemoryDB())
        assert validate_runtime(rt, "legacy", executes_tasks=False) is True

    def test_skill_planned_rejects_injected_legacy_executor(self) -> None:
        """§94：即使误传 legacy_executor，skill_planned 也必须强制丢弃。"""
        rt = _build("skill_planned", legacy_executor=_LegacyStub(), db=_MemoryDB())
        assert rt.legacy_loaded is False
        assert rt.legacy_executor is None

    def test_invalid_mode_raises(self) -> None:
        rt = _build("legacy", legacy_executor=_LegacyStub(), db=_MemoryDB())
        with pytest.raises(StartupValidationError):
            validate_runtime(rt, "bogus", executes_tasks=True)


# ══════════════════════════════════════════════════════════════
# §60 / 61 Critical Test 1 / 2：skill_planned 不构造旧链
# ══════════════════════════════════════════════════════════════

class TestSkillPlannedNoOldChain:
    def test_worker_guards_old_constructs_behind_need_legacy(self) -> None:
        """worker.py 中 PipelineManagerV2 / build_multi_agent_runtime / LLMWrapper
        的构造必须落在 `if need_legacy:` 保护块内（§22 / §23 / §94）。"""
        src = _read_worker_source()
        guard = src.find("if need_legacy:")
        assert guard != -1, "worker.py 必须存在 `if need_legacy:` 保护块"
        legacy_block = src[guard:]
        for symbol in (
            "PipelineManagerV2(",
            "build_multi_agent_runtime(",
            "LLMWrapper(",
            "LegacyPipelineExecutor(",
        ):
            assert symbol in legacy_block, (
                f"worker.py 中 {symbol} 未落在 need_legacy 保护块内（§94）"
            )

    def test_factory_skill_planned_forced_drop_legacy(self) -> None:
        rt = _build("skill_planned", legacy_executor=_LegacyStub(), db=_MemoryDB())
        assert rt.legacy_executor is None
        assert rt.legacy_loaded is False


def _read_worker_source() -> str:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    return (root / "services" / "backend" / "worker.py").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# §62 Critical Test 3：legacy 模式匹配切机前行为
# ══════════════════════════════════════════════════════════════

class TestLegacyBehaviorMatch:
    async def test_legacy_selects_legacy_engine(self) -> None:
        legacy = _LegacyStub()
        rt = _build("legacy", legacy_executor=legacy, db=_MemoryDB())
        req = ExecutionRequest(task_id="t1", task_type="run-v2")
        assert rt.execution_router.select_engine(req) == "legacy"
        result = await rt.execution_router.execute(req)
        assert result.engine == "legacy"
        assert result.status == "SUCCEEDED"


# ══════════════════════════════════════════════════════════════
# §63 Critical Test 4：Shadow 只读，绝不写生产
# ══════════════════════════════════════════════════════════════

class TestShadowNoProductionWrite:
    def test_shadow_skill_runtime_is_readonly(self) -> None:
        rt = _build("skill_shadow", legacy_executor=_LegacyStub(), db=_MemoryDB())
        assert rt.shadow_executor is not None
        assert rt.shadow_executor._write_guarded is True
        assert rt.skill_runtime.default_adapter == "production_readonly"

    async def test_shadow_primary_result_is_legacy(self) -> None:
        """Shadow 模式下正式返回结果永远来自 Legacy primary（§25 / §47）。"""
        rt = _build("skill_shadow", legacy_executor=_LegacyStub(), db=_MemoryDB())
        req = ExecutionRequest(task_id="t1", task_type="run-v2")
        result = await rt.execution_router.execute(req)
        assert result.engine == "legacy"
        assert result.status == "SUCCEEDED"


# ══════════════════════════════════════════════════════════════
# §64 Critical Test 5：Skill 失败绝不自动 fallback Legacy
# ══════════════════════════════════════════════════════════════

class TestNoAutoFallback:
    async def test_skill_planned_skill_boom_propagates(self) -> None:
        rt = _build("skill_planned", legacy_executor=None, db=_MemoryDB())
        # 替换为必然失败的 skill executor，验证 Router 不落到 legacy
        rt.execution_router.skill = _SkillBoom()  # type: ignore[assignment]
        req = ExecutionRequest(task_id="t1", task_type="run-v2")
        with pytest.raises(RuntimeError, match="skill boom"):
            await rt.execution_router.execute(req)

    async def test_skill_shadow_skill_boom_does_not_affect_legacy(self) -> None:
        rt = _build("skill_shadow", legacy_executor=_LegacyStub(), db=_MemoryDB())
        rt.execution_router.shadow.skill = _SkillBoom()  # type: ignore[attribute-error]
        req = ExecutionRequest(task_id="t1", task_type="run-v2")
        result = await rt.execution_router.execute(req)
        assert result.engine == "legacy"
        assert result.status == "SUCCEEDED"


# ══════════════════════════════════════════════════════════════
# §65 / 66 Critical Test 6 / 7：Retry / Resume sticky
# ══════════════════════════════════════════════════════════════

class _RouterRecorder:
    """记录 select_engine / execute / resume 调用。"""

    def __init__(self) -> None:
        self.select_calls: list[ExecutionRequest] = []
        self.executed_engine: str | None = None
        self.resumed_engine: str | None = None

    def select_engine(self, request: ExecutionRequest) -> str:
        self.select_calls.append(request)
        return "legacy"

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.executed_engine = request.selected_engine
        return ExecutionResult(status="SUCCEEDED", engine="legacy")

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        self.resumed_engine = request.selected_engine
        return ExecutionResult(status="SUCCEEDED", engine=request.selected_engine or "legacy")


def _task(**extra: Any) -> dict[str, Any]:
    base = {
        "task_id": "t1",
        "user_id": "usr",
        "task_type": "run-v2",
        "status": "pending",
    }
    base.update(extra)
    return base


class TestRetryResumeSticky:
    @staticmethod
    def _memory_task(task: dict[str, Any]) -> Any:
        """写入内存库并返回 db（execute_pipeline 自建 PipelineStateManager(ctx["db"])）。"""
        db = _MemoryDB()
        db["pipeline_tasks"]._docs.append(dict(task))
        return db

    @pytest.mark.asyncio
    async def test_retry_keeps_selected_engine(self) -> None:
        from agent.task_queue import execute_pipeline

        db = self._memory_task(_task())
        router = _RouterRecorder()
        ctx = {"db": db, "execution_router": router}  # type: ignore[dict-item]

        # 首跑：select_engine 调一次，并把 selected_engine 持久化到 task state
        await execute_pipeline(ctx, "t1", "usr", "run-v2")
        doc = db["pipeline_tasks"]._docs[0]
        assert len(router.select_calls) == 1
        assert doc.get("selected_engine") == "legacy"
        assert router.executed_engine == "legacy"

        # 重试：任务已带 selected_engine，不再调用 select_engine（sticky 复用 §32 / §48）
        router.select_calls.clear()
        await execute_pipeline(ctx, "t1", "usr", "run-v2")
        assert not router.select_calls, "retry 不应重新 rollout / select_engine"
        assert router.executed_engine == "legacy"

    @pytest.mark.asyncio
    async def test_resume_keeps_selected_engine(self) -> None:
        from agent.task_queue import resume_pipeline

        db = self._memory_task(_task(selected_engine="skill_planned"))
        router = _RouterRecorder()
        ctx = {"db": db, "execution_router": router}  # type: ignore[dict-item]

        await resume_pipeline(ctx, "t1", "usr")
        assert router.resumed_engine == "skill_planned"

    @pytest.mark.asyncio
    async def test_resume_historic_task_defaults_legacy(self) -> None:
        """历史任务没有 selected_engine → Router 回退 legacy（§49），不抛错。"""
        from agent.task_queue import resume_pipeline

        db = self._memory_task(_task())  # 无 selected_engine
        router = _RouterRecorder()
        ctx = {"db": db, "execution_router": router}  # type: ignore[dict-item]

        result = await resume_pipeline(ctx, "t1", "usr")
        assert result["status"] == "completed"
        assert router.resumed_engine in (None, "legacy")


# ══════════════════════════════════════════════════════════════
# §67 Critical Test 8：Worker 与 Main 共用统一 production builder
# ══════════════════════════════════════════════════════════════

class TestWorkerMainSharedBuilder:
    def test_worker_source_uses_builder(self) -> None:
        src = _read_worker_source()
        assert "build_production_execution_runtime" in src
        assert "validate_runtime" in src

    def test_main_source_uses_builder(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (root / "services" / "backend" / "main.py").read_text(encoding="utf-8")
        assert "build_production_execution_runtime" in src
        assert "validate_runtime" in src


# ══════════════════════════════════════════════════════════════
# §68 Critical Test 9：Worker skill_planned 具备完整 Skill Runtime
# ══════════════════════════════════════════════════════════════

class TestWorkerSkillPlannedHasSkillRuntime:
    def test_skill_planned_runtime_components(self) -> None:
        rt = _build("skill_planned", legacy_executor=None, db=_MemoryDB())
        assert rt.skill_executor is not None
        assert rt.skill_runtime is not None
        assert rt.orchestration_runtime is not None
        assert rt.business_executor is not None
        assert rt.artifact_store is not None


# ══════════════════════════════════════════════════════════════
# §69 Critical Test 10：生产 Artifact 跨 Worker 重启持久
# ══════════════════════════════════════════════════════════════

class TestProductionArtifactPersists:
    @pytest.mark.asyncio
    async def test_artifact_survives_store_recreation(self) -> None:
        from agent.artifacts.mongo_store import MongoArtifactStore

        db = _MemoryDB()  # 同一份持久介质，模拟 Mongo 跨进程存活
        store1 = MongoArtifactStore(db)
        await store1.ensure_indexes()
        await store1.put(
            artifact_type="TriageArtifact",
            payload={"artifact_id": "art-1", "category": "MCP协议漏洞"},
            producer="skill:article-triage",
            run_id="run-1",
            step_id="triage",
            parent_ref=None,
            shadow=False,
            tenant_id="ten-1",
            user_id="usr-1",
        )

        # 模拟 Worker 重启：以同一持久介质新建 store 实例（连接地址不变则数据仍在）
        store2 = MongoArtifactStore(db)
        record = await store2.get_record(
            artifact_id="art-1", artifact_type="TriageArtifact", version=1
        )
        assert record["producer"] == "skill:article-triage"
        assert record["shadow"] is False
        assert record["tenant_id"] == "ten-1"
        assert record["user_id"] == "usr-1"

        payload = await store2.get(
            artifact_id="art-1", artifact_type="TriageArtifact", version=1
        )
        assert payload["category"] == "MCP协议漏洞"
