"""MultiAgent 安全测试 — 阶段三 Step 10。

覆盖运行时安全边界：
  - 违规 Plan 100% fallback（planner 层拒绝 → 确定性默认计划）；
  - review 必经 / 不可注销 / 不可绕过；
  - article / product 白名单与 user 隔离；
  - 人工重放授权（非 failed/dead_lettered 拒绝、输入变更拒绝、Worker 未注册拒绝）；
  - FORBIDDEN_WORKERS 不可注册；
  - 客户端不可提交 Worker / step args / owner（PlannerChoice 只含业务选择）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pymongo import ReturnDocument

from agent.execution_step_ledger import ExecutionStepLedger
from agent.multi_agent import MultiAgentReplayError, MultiAgentRuntime
from agent.orchestrator import Orchestrator
from agent.plan_contracts import PlanValidator, PlanStep, PipelinePlan, build_default_plan
from agent.planner import PLANNER_VERSION, Planner, PlannerArticleInput, PlannerChoice
from agent.worker_registry import (
    FORBIDDEN_WORKERS,
    WorkerAdapter,
    WorkerRegistry,
    WorkerResult,
    WorkerSpec,
)


# ═══════════════════════════════════════════════════════════════
# Fake MongoDB（与 test_execution_step_ledger 同构）
# ═══════════════════════════════════════════════════════════════


def _match(doc: dict, query: dict) -> bool:
    for key, cond in query.items():
        value = doc.get(key)
        if isinstance(cond, dict):
            for op, operand in cond.items():
                if op == "$in":
                    if value not in operand:
                        return False
                elif op == "$ne":
                    if value == operand:
                        return False
                elif op == "$lte":
                    if not (value is not None and value <= operand):
                        return False
                elif op == "$gte":
                    if not (value is not None and value >= operand):
                        return False
                else:
                    raise AssertionError(f"unsupported operator: {op}")
        else:
            if value != cond:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    async def to_list(self, length: int | None = None) -> list[dict]:
        return list(self._docs) if length is None else list(self._docs[:length])

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeCol:
    def __init__(self):
        self.docs: list[dict] = []
        self.created_indexes: list[str] = []
        self._next_id = 0

    def find(self, query: dict):
        return _FakeCursor([dict(d) for d in self.docs if _match(d, query)])

    async def find_one(self, query: dict):
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    async def insert_one(self, doc: dict):
        doc = dict(doc)
        doc["_id"] = self._next_id
        self._next_id += 1
        self.docs.append(doc)
        return SimpleNamespace(acknowledged=True, inserted_id=doc["_id"])

    async def find_one_and_update(self, filter_query: dict, update: dict, return_document=None):
        for doc in self.docs:
            if _match(doc, filter_query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                if return_document == ReturnDocument.AFTER:
                    return dict(doc)
                return dict(doc)
        return None

    async def update_many(self, filter_query: dict, update: dict):
        matched = 0
        for doc in self.docs:
            if _match(doc, filter_query):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                matched += 1
        return SimpleNamespace(matched_count=matched, modified_count=matched)

    async def update_one(self, filter_query: dict, update: dict):
        result = await self.find_one_and_update(
            filter_query, update, return_document=ReturnDocument.AFTER
        )
        if result is None:
            return SimpleNamespace(matched_count=0, modified_count=0)
        return SimpleNamespace(matched_count=1, modified_count=1)

    async def create_indexes(self, indexes):
        for index in indexes:
            name = index.document.get("name")
            if name not in self.created_indexes:
                self.created_indexes.append(name)
        return list(self.created_indexes)


class _FakeDB:
    def __init__(self):
        self._cols: dict[str, _FakeCol] = {}

    def __getitem__(self, name: str) -> _FakeCol:
        return self._cols.setdefault(name, _FakeCol())


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _step(step_id: str, worker: str, depends_on: list[str] | None = None, policy: str = "required"):
    return PlanStep(
        step_id=step_id,
        worker=worker,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        input_refs={},
        policy=policy,  # type: ignore[arg-type]
        timeout_s=600,
        max_attempts=3,
    )


def _full_plan() -> PipelinePlan:
    """合法默认计划（crawl→classify→score→draft→quality_check→review）。"""
    return PipelinePlan(
        plan_id="plan-sec",
        run_id="run-sec",
        planner_version="test-v1",
        input_snapshot_hash="sha256:snap",
        steps=[
            _step("s1_crawl", "crawl"),
            _step("s2_classify", "classify", ["s1_crawl"]),
            _step("s3_score", "score", ["s2_classify"]),
            _step("s4_draft", "draft", ["s3_score"]),
            _step("s5_quality_check", "quality_check", ["s4_draft"]),
            _step("s6_review", "review", ["s5_quality_check"]),
        ],
    )


def _draft_without_review_plan() -> PipelinePlan:
    return PipelinePlan(
        plan_id="plan-bad",
        run_id="run-bad",
        planner_version="test-v1",
        input_snapshot_hash="sha256:snap",
        steps=[
            _step("s1_crawl", "crawl"),
            _step("s2_draft", "draft", ["s1_crawl"]),
        ],
    )


class _FakeAdapter(WorkerAdapter):
    def __init__(self, name: str, **spec_kwargs):
        self.name = name  # type: ignore[assignment]
        self.spec = WorkerSpec(name=name, **spec_kwargs)  # type: ignore[arg-type]

    async def execute(self, state, ctx, lease=None) -> WorkerResult:
        return WorkerResult(
            step_id=ctx["step_id"],
            worker=ctx["worker"],
            idempotency_key="k",
            input_hash="i",
            result_hash="r",
            status="succeeded",
            attempt=ctx["attempt"],
        )


def _registry() -> WorkerRegistry:
    registry = WorkerRegistry()
    for name in ("crawl", "classify", "score", "draft", "quality_check", "review"):
        registry.register(_FakeAdapter(name, version="v1", concurrency_group="local"))
    return registry


def _runtime(db=None, registry=None) -> MultiAgentRuntime:
    db = db or _FakeDB()
    registry = registry or _registry()
    validator = PlanValidator()
    ledger = ExecutionStepLedger(db)
    orchestrator = Orchestrator(registry, ledger=ledger)
    planner = Planner(
        db=db,
        enabled=True,
        planner_model="fake",
        validator=validator,
        planner_version=PLANNER_VERSION,
    )
    return MultiAgentRuntime(
        planner=planner,
        validator=validator,
        orchestrator=orchestrator,
        ledger=ledger,
        registry=registry,
        db=db,
    )


# ═══════════════════════════════════════════════════════════════
# 1. 违规 Plan 100% fallback
# ═══════════════════════════════════════════════════════════════


class TestViolationFallsBack:
    def test_draft_without_review_is_rejected(self):
        validator = PlanValidator()
        result = validator.validate(_draft_without_review_plan())
        assert result.rejected is True
        assert "missing guard workers" in result.reason

    def test_forbidden_worker_plan_is_rejected(self):
        """schema 层即拒绝 forbidden worker（字面量枚举），validator 兜底。"""
        from pydantic import ValidationError

        plan = _full_plan()
        payload = plan.model_dump(mode="json")
        payload["steps"].append(
            {
                "step_id": "s7_publish",
                "worker": "publish",
                "depends_on": ["s6_review"],
                "input_refs": {},
                "policy": "required",
                "timeout_s": 600,
                "max_attempts": 3,
            }
        )
        with pytest.raises(ValidationError):
            PipelinePlan.model_validate(payload)

    @pytest.mark.asyncio
    async def test_planner_rejected_choice_falls_back_to_default(self):
        """LLM 选择越权产品 → 白名单拒绝 → 100% 回退确定性默认计划。"""
        class RogueWrapper:
            async def invoke_structured(self, **kwargs):
                return PlannerChoice(
                    needs_fulltext=False,
                    breaking_article_ids=[],
                    article_ids=["art-1"],
                    product_ids=["hacked-product"],  # 越权产品
                    score_threshold=80,
                    rationale_summary="越权尝试",
                )

        planner = Planner(
            llm_wrapper=RogueWrapper(),
            enabled=True,
            planner_model="fake",
            validator=PlanValidator(),
        )
        outcome = await planner.plan(
            run_id="run-sec",
            user_id="user-a",
            products=[{"id": "agent-identity-security", "name": "合法产品"}],
            articles=[PlannerArticleInput(id="art-1", title="t", summary="s", status="crawled")],
        )
        assert outcome.rejected is True
        assert outcome.source == "fallback"
        # fallback 计划必须含 review 且产品白名单被强制为合法集合
        workers = {s.worker for s in outcome.plan.steps}
        assert "review" in workers


# ═══════════════════════════════════════════════════════════════
# 2. review 必经 / 不可注销 / 不可绕过
# ═══════════════════════════════════════════════════════════════


class TestReviewGuard:
    def test_review_cannot_be_unregistered(self):
        registry = _registry()
        with pytest.raises(ValueError):
            registry.unregister("review")

    def test_review_is_required_in_default_plan(self):
        plan = build_default_plan(
            run_id="run-1",
            input_snapshot_hash_value="sha256:snap",
            product_ids=[],
            article_ids=["art-1"],
            needs_fulltext=False,
        )
        workers = {s.worker for s in plan.steps}
        assert "review" in workers
        assert "quality_check" in workers

    def test_plan_coverage_requires_registered_review(self):
        registry = WorkerRegistry()
        for name in ("crawl", "score"):
            registry.register(_FakeAdapter(name, version="v1", concurrency_group="local"))
        # review 未注册 → 计划覆盖校验失败
        assert registry.validate_plan_coverage([_step("s1", "crawl"), _step("s2", "review", ["s1"])]) is False


# ═══════════════════════════════════════════════════════════════
# 3. 白名单与 user 隔离（validator 层）
# ═══════════════════════════════════════════════════════════════


class TestWhitelistAndIsolation:
    def test_foreign_product_rejected(self):
        validator = PlanValidator()
        plan = _full_plan()
        for s in plan.steps:
            if s.worker == "draft":
                s.input_refs["product_ids"] = ["evil-product"]
        result = validator.validate(plan, allowed_products={"agent-identity-security"})
        assert result.rejected is True
        assert "product not allowed" in result.reason

    def test_foreign_article_rejected(self):
        validator = PlanValidator()
        plan = _full_plan()
        for s in plan.steps:
            s.input_refs["article_ids"] = ["evil-article"]
        result = validator.validate(plan, allowed_article_ids={"art-1"})
        assert result.rejected is True
        assert "article not allowed" in result.reason

    def test_user_id_impersonation_rejected(self):
        validator = PlanValidator()
        plan = _full_plan()
        plan.steps[0].input_refs["user_id"] = "other-user"
        result = validator.validate(plan, allow_user_id="victim-user")
        assert result.rejected is True
        assert "user_id not allowed" in result.reason


# ═══════════════════════════════════════════════════════════════
# 4. FORBIDDEN_WORKERS 不可注册
# ═══════════════════════════════════════════════════════════════


class TestForbiddenWorkers:
    @pytest.mark.parametrize("name", sorted(FORBIDDEN_WORKERS))
    def test_forbidden_worker_register_rejected(self, name):
        registry = WorkerRegistry()
        with pytest.raises(ValueError):
            registry.register(_FakeAdapter(name, version="v1", concurrency_group="local"))


# ═══════════════════════════════════════════════════════════════
# 5. 人工重放授权
# ═══════════════════════════════════════════════════════════════


class TestReplayAuthorization:
    async def _failed_step(self, db, step_id: str = "s3_score") -> ExecutionStepLedger:
        ledger = ExecutionStepLedger(db)
        await ledger.init_run(_full_plan())
        claim = await ledger.begin_attempt(
            run_id="run-sec", step_id=step_id, owner_id="worker-1", attempt=1
        )
        await ledger.fail(
            run_id="run-sec",
            step_id=step_id,
            owner_id="worker-1",
            fencing_token=claim.fencing_token,
            status="failed",
            error_type="boom",
            error_message="recoverable",
            retryable=True,
            result_hash="sha256:r",
        )
        return ledger

    @pytest.mark.asyncio
    async def test_replay_rejects_non_failed_step(self):
        runtime = _runtime()
        db = runtime.db
        ledger = ExecutionStepLedger(db)
        await ledger.init_run(_full_plan())
        with pytest.raises(MultiAgentReplayError) as exc:
            await runtime.replay_step(run_id="run-sec", step_id="s1_crawl")
        assert exc.value.code == "NOT_REPLAYABLE"

    @pytest.mark.asyncio
    async def test_replay_rejects_unknown_step(self):
        runtime = _runtime()
        with pytest.raises(MultiAgentReplayError) as exc:
            await runtime.replay_step(run_id="run-sec", step_id="nope")
        assert exc.value.code == "STEP_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_replay_rejects_changed_input(self):
        runtime = _runtime()
        await self._failed_step(runtime.db)

        async def verify_latest_input(entry) -> bool:
            return False  # 输入已变更

        with pytest.raises(MultiAgentReplayError) as exc:
            await runtime.replay_step(
                run_id="run-sec", step_id="s3_score", verify_latest_input=verify_latest_input
            )
        assert exc.value.code == "INPUT_CHANGED"

    @pytest.mark.asyncio
    async def test_replay_rejects_unregistered_worker(self):
        db = _FakeDB()
        registry = _registry()
        registry.unregister("score")
        runtime = _runtime(db=db, registry=registry)
        await self._failed_step(db)
        with pytest.raises(MultiAgentReplayError) as exc:
            await runtime.replay_step(run_id="run-sec", step_id="s3_score")
        assert exc.value.code == "UNREGISTERED_WORKER"

    @pytest.mark.asyncio
    async def test_replay_succeeds_for_failed_step(self):
        runtime = _runtime()
        await self._failed_step(runtime.db)
        outcome = await runtime.replay_step(run_id="run-sec", step_id="s3_score")
        assert outcome.status == "succeeded"


# ═══════════════════════════════════════════════════════════════
# 6. 客户端不可提交 Worker / step args / owner
# ═══════════════════════════════════════════════════════════════


class TestClientCannotSubmitExecutionDetails:
    def test_planner_choice_has_no_execution_fields(self):
        """模型输出模型只有业务选择字段，无 worker/owner/step 参数。"""
        fields = set(PlannerChoice.model_fields)
        assert "worker" not in fields
        assert "owner" not in fields
        assert "step" not in fields
        assert "input_refs" not in fields
        assert fields <= {
            "needs_fulltext",
            "breaking_article_ids",
            "article_ids",
            "product_ids",
            "score_threshold",
            "style_hints",
            "rationale_summary",
        }

    def test_plan_step_does_not_expose_owner(self):
        """PipelinePlan 步骤无 owner 字段，owner 由运行时（ledger/orchestrator）确定。"""
        assert "owner_id" not in PlanStep.model_fields
        assert "owner" not in PlanStep.model_fields
