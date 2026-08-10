"""WorkerRegistry 单元测试 — 阶段三 Step 3。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from agent.plan_contracts import build_default_plan, input_snapshot_hash
from agent.worker_registry import (
    CONCURRENCY_GROUP_LLM,
    CONCURRENCY_GROUP_LOCAL,
    REVIEW_WORKER,
    V2NodeAdapter,
    WorkerAdapter,
    WorkerLease,
    WorkerRegistry,
    WorkerResult,
    WorkerSpec,
    _crawl_input_resolver,
    build_default_registry,
)

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


async def _ok_handler(state: dict, **kwargs) -> dict:
    state["current_phase"] = "crawl"
    state["crawled_count"] = 7
    return state


async def _raise_handler(state: dict, **kwargs) -> dict:
    raise RuntimeError("boom")


def _adapter(
    name: str = "crawl",
    handler=None,
    spec_kwargs: dict | None = None,
    output_keys: tuple[str, ...] = ("crawled_count",),
) -> V2NodeAdapter:
    spec = WorkerSpec(name=name, **spec_kwargs or {})  # type: ignore[arg-type]
    return V2NodeAdapter(
        spec=spec,
        handler=handler or _ok_handler,
        output_keys=output_keys,
        input_resolver=_crawl_input_resolver if name == "crawl" else None,
    )


def _ctx(**overrides) -> dict:
    ctx = {
        "run_id": "run-1",
        "plan_id": "plan-1",
        "step_id": "s1",
        "user_id": "u-1",
        "worker": "crawl",
        "attempt": 1,
        "input_refs": {"crawl_days": 1},
    }
    ctx.update(overrides)
    return ctx


# ═══════════════════════════════════════════════════════════════
# WorkerSpec
# ═══════════════════════════════════════════════════════════════


class TestWorkerSpec:
    def test_defaults(self):
        spec = WorkerSpec(name="review")
        assert spec.version == "v1"
        assert spec.side_effect == "none"
        assert spec.retry_safe is False
        assert spec.timeout_s == 600
        assert spec.max_attempts == 3
        assert spec.required_scopes == set()
        assert spec.concurrency_group == CONCURRENCY_GROUP_LOCAL

    def test_invalid_name_rejected(self):
        with pytest.raises(ValueError):
            WorkerSpec(name="publish")  # type: ignore[arg-type]

    def test_timeout_bounds(self):
        with pytest.raises(ValueError):
            WorkerSpec(name="crawl", timeout_s=0)
        with pytest.raises(ValueError):
            WorkerSpec(name="crawl", timeout_s=99999)


# ═══════════════════════════════════════════════════════════════
# WorkerLease
# ═══════════════════════════════════════════════════════════════


class TestWorkerLease:
    def test_expired(self):
        past = datetime.now(UTC) - timedelta(seconds=10)
        assert (
            WorkerLease(
                owner_id="w1",
                run_id="run-1",
                step_id="s1",
                expires_at=past,
                fencing_token=1,
            ).expired
            is True
        )

    def test_active(self):
        future = datetime.now(UTC) + timedelta(seconds=120)
        assert (
            WorkerLease(
                owner_id="w1",
                run_id="run-1",
                step_id="s1",
                expires_at=future,
                fencing_token=2,
            ).expired
            is False
        )


# ═══════════════════════════════════════════════════════════════
# WorkerResult
# ═══════════════════════════════════════════════════════════════


class TestWorkerResult:
    def test_defaults(self):
        result = WorkerResult(
            step_id="s1",
            worker="crawl",
            idempotency_key="k",
            input_hash="i",
            result_hash="r",
        )
        assert result.status == "succeeded"
        assert result.retryable is False
        assert result.output == {}

    def test_status_enum(self):
        with pytest.raises(ValueError):
            WorkerResult(
                step_id="s1",
                worker="crawl",
                idempotency_key="k",
                input_hash="i",
                result_hash="r",
                status="running",  # type: ignore[arg-type]
            )


# ═══════════════════════════════════════════════════════════════
# 幂等键 / 输入 / 结果哈希
# ═══════════════════════════════════════════════════════════════


class TestIdempotencyKey:
    def test_format(self):
        adapter = _adapter()
        resolved = adapter.resolve_input(
            {"crawl_days": 1, "user_id": "u-1", "trace_id": "t"}, _ctx()
        )
        input_hash = adapter.compute_input_hash(resolved)
        key = adapter.idempotency_key(_ctx(), input_hash)
        assert key.startswith("u-1:run-1:s1:")
        assert key.endswith(":" + input_hash)
        assert key.count(":") == 4  # user:run:step:sha256-prefixed hash 也含冒号 → 共 5 段

    def test_same_input_same_key(self):
        adapter = _adapter()
        ra = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        rb = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        assert ra.idempotency_key == rb.idempotency_key
        assert ra.input_hash == rb.input_hash

    def test_different_input_different_key(self):
        adapter = _adapter()
        ra = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        rb = asyncio.run(adapter.execute({"crawl_days": 2, "user_id": "u-1"}, _ctx()))
        assert ra.idempotency_key != rb.idempotency_key


class TestInputContract:
    def test_drops_planner_free_text(self):
        adapter = _adapter(name="crawl")
        ctx = _ctx(input_refs={"crawl_days": 1, "style_hints": "自由文本注入", "arbitrary": "x"})
        resolved = adapter.resolve_input({"crawl_days": 3, "user_id": "u-1", "trace_id": "t"}, ctx)
        # 自由文本/任意 key 被丢弃；服务端 crawl_days 优先
        assert "style_hints" not in resolved
        assert "arbitrary" not in resolved
        assert resolved["crawl_days"] == 3
        assert resolved["user_id"] == "u-1"

    def test_server_authoritative_user(self):
        adapter = _adapter(name="filter")
        ctx = _ctx(input_refs={"article_ids": ["a1"]})
        resolved = adapter.resolve_input({"user_id": "u-srv", "trace_id": "t-srv"}, ctx)
        assert resolved["user_id"] == "u-srv"
        assert resolved["trace_id"] == "t-srv"

    def test_hash_stable(self):
        adapter = _adapter()
        r1 = adapter.resolve_input({"crawl_days": 1, "user_id": "u-1"}, _ctx())
        r2 = adapter.resolve_input({"crawl_days": 1, "user_id": "u-1"}, _ctx())
        assert adapter.compute_input_hash(r1) == adapter.compute_input_hash(r2)
        assert adapter.compute_input_hash(r1).startswith("sha256:")


# ═══════════════════════════════════════════════════════════════
# V2NodeAdapter.execute
# ═══════════════════════════════════════════════════════════════


class TestV2NodeAdapterExecute:
    def test_success_result(self):
        adapter = _adapter(name="crawl", output_keys=("crawled_count",))
        result = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        assert result.status == "succeeded"
        assert result.worker == "crawl"
        assert result.output["crawled_count"] == 7
        assert result.result_hash.startswith("sha256:")
        assert result.idempotency_key.startswith("u-1:run-1:s1:")

    def test_handler_failure_returns_failed_result(self):
        adapter = _adapter(name="crawl", handler=_raise_handler, spec_kwargs={"retry_safe": True})
        result = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        assert result.status == "failed"
        assert result.error_type == "RuntimeError"
        assert result.retryable is True  # 来自 spec.retry_safe

    def test_result_hash_stable_for_same_output(self):
        adapter = _adapter(name="crawl")
        r1 = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        r2 = asyncio.run(adapter.execute({"crawl_days": 1, "user_id": "u-1"}, _ctx()))
        assert r1.result_hash == r2.result_hash


# ═══════════════════════════════════════════════════════════════
# WorkerRegistry
# ═══════════════════════════════════════════════════════════════


class _ForbiddenAdapter(WorkerAdapter):
    """绕过 Pydantic Literal，直接验证 registry 层防御。"""

    def __init__(self, name: str):
        self.name = name  # type: ignore[assignment]
        self.spec = WorkerSpec.model_construct(name=name, timeout_s=60, max_attempts=1)
        self.version = "v1"

    async def execute(self, state, ctx, lease=None) -> WorkerResult:
        raise NotImplementedError


class TestWorkerRegistry:
    def test_register_get_names(self):
        registry = WorkerRegistry()
        adapter = _adapter(name="filter", handler=_ok_handler)
        registry.register(adapter)
        assert registry.get("filter") is adapter
        assert "filter" in registry.names()

    def test_forbidden_worker_rejected(self):
        registry = WorkerRegistry()
        with pytest.raises(ValueError, match="forbidden"):
            registry.register(_ForbiddenAdapter("publish"))
        assert "publish" not in registry.names()

    def test_review_required_constant(self):
        assert REVIEW_WORKER == "review"
        registry = WorkerRegistry()
        assert registry.required_workers() == frozenset({"review"})
        assert registry.validate_plan_coverage([]) is False  # review 未注册即失败

    def test_unregister_review_forbidden(self):
        registry = WorkerRegistry()
        registry.register(_adapter(name="review", handler=_ok_handler))
        with pytest.raises(ValueError, match="required"):
            registry.unregister(REVIEW_WORKER)

    def test_unregister_other_ok(self):
        registry = WorkerRegistry()
        registry.register(_adapter(name="filter"))
        registry.unregister("filter")
        assert registry.get("filter") is None

    def test_validate_plan_coverage(self):
        registry = WorkerRegistry()
        for name in (
            "crawl",
            "classify",
            "filter",
            "score",
            "draft",
            "quality_check",
            "rewrite",
            "review",
        ):
            registry.register(_adapter(name=name, handler=_ok_handler))
        plan = build_default_plan(
            run_id="run-1",
            input_snapshot_hash_value=input_snapshot_hash(user_id="u-1"),
            needs_fulltext=False,
        )
        assert registry.validate_plan_coverage(plan.steps) is True
        # 引入未注册 worker 的步骤 → False
        steps = list(plan.steps)
        steps.append(_unknown_step())
        assert registry.validate_plan_coverage(steps) is False


def _unknown_step():
    """未注册的 Worker（enrich 不在 coverage 测试的注册表里）。"""
    from agent.plan_contracts import PlanStep

    return PlanStep(
        step_id="sx",
        worker="enrich",
        depends_on=[],
        input_refs={},
        timeout_s=60,
        max_attempts=1,
    )


# ═══════════════════════════════════════════════════════════════
# build_default_registry
# ═══════════════════════════════════════════════════════════════


class TestBuildDefaultRegistry:
    def _manager(self):
        class _Manager:
            db = None
            tools: ClassVar[dict] = {}
            classifier_v2 = None
            scorer_v2 = None
            draft_gen = None
            knowledge = None
            crawl_client = None
            template_repository = None
            reviewer = None

        return _Manager()

    def test_all_nine_workers_registered(self):
        registry = build_default_registry(self._manager())
        assert registry.names() == frozenset(
            {
                "crawl",
                "enrich",
                "classify",
                "filter",
                "score",
                "draft",
                "quality_check",
                "rewrite",
                "review",
            }
        )

    def test_review_registered_and_required(self):
        registry = build_default_registry(self._manager())
        assert registry.get("review") is not None
        assert REVIEW_WORKER in registry.required_workers()

    def test_no_forbidden_workers(self):
        registry = build_default_registry(self._manager())
        assert not (registry.names() & {"publish", "delete", "external_send", "notify"})

    def test_side_effect_and_retry_policy(self):
        registry = build_default_registry(self._manager())
        crawl = registry.get("crawl").spec
        filter_spec = registry.get("filter").spec
        review = registry.get("review").spec
        assert crawl.side_effect == "internal_write"
        assert crawl.timeout_s == 900
        assert filter_spec.side_effect == "none"
        assert filter_spec.concurrency_group == CONCURRENCY_GROUP_LOCAL
        assert review.concurrency_group == CONCURRENCY_GROUP_LLM
        assert registry.get("draft").spec.required_scopes == {
            "articles",
            "user_drafts",
            "knowledge",
            "templates",
            "user_profile",
        }

    def test_default_plan_coverage(self):
        registry = build_default_registry(self._manager())
        plan = build_default_plan(
            run_id="run-1",
            input_snapshot_hash_value=input_snapshot_hash(user_id="u-1"),
            needs_fulltext=False,
        )
        assert registry.validate_plan_coverage(plan.steps) is True

    def test_crawl_uses_server_crawl_days(self):
        registry = build_default_registry(self._manager())
        adapter = registry.get("crawl")
        ctx = _ctx(input_refs={"crawl_days": 5})
        resolved = adapter.resolve_input({"crawl_days": 2, "user_id": "u-1"}, ctx)
        assert resolved["crawl_days"] == 2  # state 优先
