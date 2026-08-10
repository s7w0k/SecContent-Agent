"""WorkerRegistry — 阶段三 Step 3。标准化 v2 Worker 包装。

把 v2 现有节点（crawl→enrich→classify→filter→score→draft→
quality_check→rewrite→review）包装为 WorkerAdapter，不重写业务逻辑。

安全/一致性边界：
  - Adapter 只从服务端 state / ctx(input_refs，已过 PlanValidator 白名单)
    解析输入，不直接使用 Planner 自由文本；
  - 写操作幂等键：``user_id:run_id:step_id:input_hash``；
  - 业务 upsert 沿用 v2 节点自身的唯一键 / CAS（如 user_drafts 按
    (user_id, article_url_hash) upsert）；
  - 每个 Worker 通过 WorkerSpec.timeout_s/max_attempts 暴露
    timeout/retry policy，通过 WorkerResult 暴露结果 Schema；
  - review 是 required Worker，不可被 Planner 省略、不可注销；
  - publish/delete/external_send/notify 等外部发布/外发不注册为本阶段 Worker。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from agent.plan_contracts import ALLOWED_INPUT_KEYS, FORBIDDEN_WORKERS, WORKER_INPUT_CONTRACT, WorkerName

logger = logging.getLogger("backend.agent.worker_registry")

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

REVIEW_WORKER: WorkerName = "review"
DRAFT_WORKER: WorkerName = "draft"

# 并发组：provider/资源级并发配额的分组依据
CONCURRENCY_GROUP_LLM = "llm"
CONCURRENCY_GROUP_CRAWL = "crawl"
CONCURRENCY_GROUP_LOCAL = "local"

SideEffect = Literal["none", "internal_write", "external_write"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _hash_json(payload: dict[str, Any]) -> str:
    """与 plan_contracts 一致的 sha256 指纹格式。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════
# WorkerSpec / WorkerLease / WorkerResult
# ═══════════════════════════════════════════════════════════════


class WorkerSpec(BaseModel):
    """Worker 注册描述：timeout/retry policy 与资源需求。"""

    name: WorkerName
    version: str = Field(default="v1", min_length=1, max_length=64)
    side_effect: SideEffect = "none"
    retry_safe: bool = False
    timeout_s: int = Field(default=600, ge=1, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    required_scopes: set[str] = Field(default_factory=set)
    concurrency_group: str = Field(default=CONCURRENCY_GROUP_LOCAL, min_length=1, max_length=64)


class WorkerLease(BaseModel):
    """步骤租约：接管后旧 Worker 的迟到写入必须因 fencing token 过期被拒绝。"""

    owner_id: str = Field(..., min_length=1, max_length=100)
    run_id: str = Field(..., min_length=1, max_length=100)
    step_id: str = Field(..., min_length=1, max_length=64)
    expires_at: datetime = Field(..., description="租约到期时间")
    fencing_token: int = Field(default=0, ge=0)

    @property
    def expired(self) -> bool:
        return self.expires_at <= _utc_now()


class WorkerResult(BaseModel):
    """Worker 执行结果 Schema（统一契约，供 orchestrator 决策）。"""

    step_id: str = Field(..., min_length=1, max_length=64)
    worker: WorkerName
    idempotency_key: str = Field(
        default="", max_length=500,
        description="写操作幂等键；失败/跳过/超时时为空串",
    )
    input_hash: str = Field(
        default="", max_length=100,
        description="输入指纹；失败/跳过/超时时为空串",
    )
    result_hash: str = Field(
        default="", max_length=100,
        description="业务结果指纹；失败/跳过时为空串",
    )
    status: Literal["succeeded", "failed", "skipped", "dead_lettered", "canceled"] = "succeeded"
    error_type: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=2000)
    retryable: bool = False
    attempt: int = Field(default=1, ge=1)
    duration_ms: int = Field(default=0, ge=0)
    output: dict[str, Any] = Field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# WorkerAdapter
# ═══════════════════════════════════════════════════════════════


class WorkerAdapter(ABC):
    """标准化 Worker 接口。

    约定：
      - ``name`` 必须是 plan_contracts.WorkerName 白名单成员；
      - ``execute(state, ctx, lease)`` 返回 WorkerResult（不抛异常）；
      - 输入只从服务端 state / ctx 解析，绝不直接消费 Planner 自由文本；
      - 幂等键 = ``user_id:run_id:step_id:input_hash``。
    """

    name: WorkerName
    spec: WorkerSpec
    version: str

    @abstractmethod
    async def execute(self, state: dict, ctx: dict, lease: WorkerLease | None = None) -> WorkerResult:
        """执行步骤。ctx 含 run_id/plan_id/step_id/user_id/attempt/input_refs。"""

    # ── 输入解析（服务端权威，不信任 Planner 自由文本）──────────
    def resolve_input(self, state: dict, ctx: dict) -> dict[str, Any]:
        """从 state + ctx.input_refs 解析 Worker 实际输入。

        仅保留 PlanValidator 白名单 key（防御纵深），并让服务端权威字段
        user_id/trace_id 以 state 为准。
        """
        refs = dict(ctx.get("input_refs") or {})
        allowed = WORKER_INPUT_CONTRACT.get(self.name, frozenset())
        refs = {k: v for k, v in refs.items() if k in ALLOWED_INPUT_KEYS and k in allowed}
        # 服务端权威字段覆盖，不信任 Planner 提交
        refs["user_id"] = state.get("user_id") or ctx.get("user_id", "")
        refs["trace_id"] = state.get("trace_id") or ctx.get("trace_id", "")
        return refs

    def compute_input_hash(self, resolved: dict[str, Any]) -> str:
        return _hash_json(resolved)

    def compute_result_hash(self, output: dict[str, Any]) -> str:
        return _hash_json(output)

    def idempotency_key(self, ctx: dict, input_hash: str) -> str:
        """写操作幂等键：user_id:run_id:step_id:input_hash。"""
        user_id = ctx.get("user_id", "")
        run_id = ctx.get("run_id", "")
        step_id = ctx.get("step_id", "")
        return f"{user_id}:{run_id}:{step_id}:{input_hash}"


# ═══════════════════════════════════════════════════════════════
# V2NodeAdapter — 包装 v2 节点，不重写业务逻辑
# ═══════════════════════════════════════════════════════════════


class V2NodeAdapter(WorkerAdapter):
    """通用 v2 节点包装器。

    ``handler`` 复用 v2 节点函数（如 crawl_node_v2），``bound_kwargs``
    绑定节点所需依赖（tools/classifier/db 等）。节点内部已有的唯一键/CAS
    写操作保持不变。
    """

    def __init__(
        self,
        *,
        spec: WorkerSpec,
        handler: Callable[..., Awaitable[dict]],
        bound_kwargs: dict[str, Any] | None = None,
        output_keys: tuple[str, ...] | None = None,
        input_resolver: Callable[[dict, dict], dict[str, Any]] | None = None,
    ):
        if spec.name in FORBIDDEN_WORKERS:
            raise ValueError(f"forbidden worker cannot be registered: {spec.name}")
        self.spec = spec
        self.name = spec.name
        self.version = spec.version
        self._handler = handler
        self._bound_kwargs = bound_kwargs or {}
        self._output_keys = output_keys or ()
        self._input_resolver = input_resolver

    def resolve_input(self, state: dict, ctx: dict) -> dict[str, Any]:
        if self._input_resolver is not None:
            return self._input_resolver(state, ctx)
        return WorkerAdapter.resolve_input(self, state, ctx)

    async def execute(
        self,
        state: dict,
        ctx: dict,
        lease: WorkerLease | None = None,
    ) -> WorkerResult:
        del lease  # 租约/fencing 校验由 Step 6 ledger 层负责
        started = time.perf_counter()
        step_id = ctx.get("step_id", "")
        attempt = int(ctx.get("attempt", 1))
        resolved = self.resolve_input(state, ctx)
        input_hash = self.compute_input_hash(resolved)
        idem_key = self.idempotency_key(ctx, input_hash)

        try:
            new_state = await self._handler(state, **self._bound_kwargs)
            output = {k: new_state.get(k) for k in self._output_keys}
            output["current_phase"] = new_state.get("current_phase")
            result_hash = self.compute_result_hash(output)
            return WorkerResult(
                step_id=step_id,
                worker=self.name,
                idempotency_key=idem_key,
                input_hash=input_hash,
                result_hash=result_hash,
                status="succeeded",
                attempt=attempt,
                duration_ms=int((time.perf_counter() - started) * 1000),
                output=output,
            )
        except Exception as exc:  # 节点异常兜底（v2 节点多数自捕获，此处防御）
            logger.warning("[worker:%s] step %s failed: %s", self.name, step_id, exc)
            return WorkerResult(
                step_id=step_id,
                worker=self.name,
                idempotency_key=idem_key,
                input_hash=input_hash,
                result_hash="",
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc)[:2000],
                retryable=self.spec.retry_safe,
                attempt=attempt,
                duration_ms=int((time.perf_counter() - started) * 1000),
                output={},
            )


# ═══════════════════════════════════════════════════════════════
# WorkerRegistry
# ═══════════════════════════════════════════════════════════════


class WorkerRegistry:
    """name → WorkerAdapter 注册表。

    不变式：
      - 禁止注册 FORBIDDEN_WORKERS（publish/delete/external_send/notify）；
      - review 是 required Worker，validate_plan_coverage 强制要求已注册；
      - Plan 中每个 step 的 worker 都必须已注册。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, WorkerAdapter] = {}

    def register(self, adapter: WorkerAdapter) -> WorkerAdapter:
        if adapter.name in FORBIDDEN_WORKERS:
            raise ValueError(f"forbidden worker cannot be registered: {adapter.name}")
        self._adapters[adapter.name] = adapter
        return adapter

    def unregister(self, name: str) -> None:
        if name == REVIEW_WORKER:
            raise ValueError("review is a required worker and cannot be unregistered")
        self._adapters.pop(name, None)

    def get(self, name: str) -> WorkerAdapter | None:
        return self._adapters.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._adapters)

    def required_workers(self) -> frozenset[str]:
        return frozenset({REVIEW_WORKER})

    def validate_plan_coverage(self, steps: list[Any]) -> bool:
        """校验计划所有 step 的 worker 均已注册，且 review 已注册。"""
        if REVIEW_WORKER not in self._adapters:
            return False
        registered = set(self._adapters)
        return all(getattr(step, "worker", None) in registered for step in steps)


# ═══════════════════════════════════════════════════════════════
# build_default_registry — 包装 v2 现有 9 节点
# ═══════════════════════════════════════════════════════════════

_DEFAULT_SPEC: dict[str, dict[str, Any]] = {
    "crawl": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=900, max_attempts=3,
        required_scopes={"articles"}, concurrency_group=CONCURRENCY_GROUP_CRAWL,
    ),
    "enrich": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=600, max_attempts=3,
        required_scopes={"articles"}, concurrency_group=CONCURRENCY_GROUP_CRAWL,
    ),
    "classify": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=600, max_attempts=3,
        required_scopes={"articles"}, concurrency_group=CONCURRENCY_GROUP_LLM,
    ),
    "filter": dict(
        side_effect="none", retry_safe=True, timeout_s=300, max_attempts=2,
        required_scopes={"articles"}, concurrency_group=CONCURRENCY_GROUP_LOCAL,
    ),
    "score": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=900, max_attempts=3,
        required_scopes={"articles", "knowledge"}, concurrency_group=CONCURRENCY_GROUP_LLM,
    ),
    "draft": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=1200, max_attempts=3,
        required_scopes={"articles", "user_drafts", "knowledge", "templates", "user_profile"},
        concurrency_group=CONCURRENCY_GROUP_LLM,
    ),
    "quality_check": dict(
        side_effect="none", retry_safe=True, timeout_s=300, max_attempts=2,
        required_scopes={"user_drafts"}, concurrency_group=CONCURRENCY_GROUP_LOCAL,
    ),
    "rewrite": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=600, max_attempts=3,
        required_scopes={"articles", "user_drafts", "knowledge", "templates", "user_profile"},
        concurrency_group=CONCURRENCY_GROUP_LLM,
    ),
    "review": dict(
        side_effect="internal_write", retry_safe=True, timeout_s=600, max_attempts=3,
        required_scopes={"articles", "user_drafts"}, concurrency_group=CONCURRENCY_GROUP_LLM,
    ),
}

_OUTPUT_KEYS: dict[str, tuple[str, ...]] = {
    "crawl": ("crawled_count", "needs_enrich", "incomplete_article_count"),
    "enrich": ("enriched_count",),
    "classify": ("classified_v2_count", "low_confidence_count"),
    "filter": ("pr_eligible_count",),
    "score": ("scored_v2_count", "score_anomaly", "score_threshold"),
    "draft": ("draft_count",),
    "quality_check": ("needs_rewrite",),
    "rewrite": ("rewritten_count",),
    "review": ("review_count", "review_failed_count", "review_reused_count"),
}


def _crawl_input_resolver(state: dict, ctx: dict) -> dict[str, Any]:
    """crawl 的 crawl_days 以服务端 state 为准。"""
    refs = dict(ctx.get("input_refs") or {})
    return {
        "crawl_days": int(state.get("crawl_days") or refs.get("crawl_days", 1)),
        "user_id": state.get("user_id") or ctx.get("user_id", ""),
        "trace_id": state.get("trace_id") or ctx.get("trace_id", ""),
    }


def build_default_registry(manager: Any, db: Any = None) -> WorkerRegistry:
    """把 v2 现有 9 节点包装为标准 Worker（不重写业务逻辑）。

    manager: PipelineManagerV2（或提供同名属性的替代实现）；
    db: Mongo 数据库（默认取 manager.db）。
    """
    from agent import pipeline_v2

    database = db if db is not None else getattr(manager, "db", None)
    tools = getattr(manager, "tools", None)
    classifier = getattr(manager, "classifier_v2", None)
    scorer = getattr(manager, "scorer_v2", None)
    draft_gen = getattr(manager, "draft_gen", None)
    knowledge = getattr(manager, "knowledge", None)
    crawl_client = getattr(manager, "crawl_client", None)
    template_repository = getattr(manager, "template_repository", None)
    reviewer = getattr(manager, "reviewer", None)

    handlers = {
        "crawl": pipeline_v2.crawl_node_v2,
        "enrich": pipeline_v2.enrich_node,
        "classify": pipeline_v2.classify_v2_node,
        "filter": pipeline_v2.filter_node,
        "score": pipeline_v2.score_v2_node,
        "draft": pipeline_v2.draft_node,
        "quality_check": pipeline_v2.quality_check_node,
        "rewrite": pipeline_v2.rewrite_node,
        "review": pipeline_v2.review_node,
    }

    registry = WorkerRegistry()
    for name in _DEFAULT_SPEC:
        spec = WorkerSpec(name=name, version="v2", **_DEFAULT_SPEC[name])  # type: ignore[arg-type]
        registry.register(
            V2NodeAdapter(
                spec=spec,
                handler=handlers[name],
                bound_kwargs=_bound_kwargs(
                    name,
                    tools=tools,
                    database=database,
                    classifier=classifier,
                    scorer=scorer,
                    knowledge=knowledge,
                    draft_gen=draft_gen,
                    crawl_client=crawl_client,
                    template_repository=template_repository,
                    reviewer=reviewer,
                ),
                output_keys=_OUTPUT_KEYS[name],
                input_resolver=_crawl_input_resolver if name == "crawl" else None,
            )
        )
    return registry


def _bound_kwargs(
    name: str,
    *,
    tools: Any,
    database: Any,
    classifier: Any,
    scorer: Any,
    knowledge: Any,
    draft_gen: Any,
    crawl_client: Any,
    template_repository: Any,
    reviewer: Any,
) -> dict[str, Any]:
    """节点所需依赖绑定（与 pipeline_v2._build_graph 一致）。"""
    if name == "crawl":
        return {"tools": tools, "db": database, "crawl_client": crawl_client}
    if name == "enrich":
        return {"tools": tools, "db": database, "crawl_client": crawl_client}
    if name == "classify":
        return {"classifier": classifier, "db": database}
    if name == "filter":
        return {"db": database}
    if name == "score":
        return {"scorer": scorer, "knowledge": knowledge, "db": database}
    if name in {"draft", "rewrite"}:
        return {
            "draft_gen": draft_gen,
            "knowledge": knowledge,
            "db": database,
            "template_repository": template_repository,
        }
    if name == "quality_check":
        return {"db": database}
    if name == "review":
        return {"reviewer": reviewer, "db": database}
    raise ValueError(f"unknown worker: {name}")
