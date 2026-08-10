"""MultiAgent 双轨运行时 — 阶段三 Step 7。

职责：
  - 执行模式决策：current（固定 DAG）/ planned（planner→validator→orchestrator）/
    shadow（planned 执行但不回填业务产物，仅记录差异）；
  - 组装 planner/validator/orchestrator/ledger/registry 运行时；
  - 影子模式 DB 代理：拦截业务写操作，只记录差异日志；
  - 人工重放：仅接受 dead-letter/failed 步骤，校验最新输入后通过
    ledger CAS 领取并执行单个 Worker。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from agent.context_bridge import user_in_rollout
from agent.execution_step_ledger import ExecutionStepLedger
from agent.orchestrator import Orchestrator, StepOutcome
from agent.plan_contracts import PlanValidator
from agent.planner import PLANNER_VERSION, Planner, PlannerArticleInput
from agent.worker_registry import WorkerLease, WorkerRegistry, build_default_registry

logger = logging.getLogger("backend.agent.multi_agent")

# 执行模式
MODE_CURRENT = "current"
MODE_PLANNED = "planned"
MODE_SHADOW = "shadow"

# 影子模式拦截的业务写操作
_WRITE_OPS = {
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "find_one_and_update",
    "find_one_and_replace",
    "find_one_and_delete",
    "delete_one",
    "delete_many",
    "bulk_write",
}
# 敏感字段：差异日志绝不记录正文/全文/prompt 内容
_SENSITIVE_KEYS = {"content_md", "content", "body", "prompt", "system_prompt", "text", "summary"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def decide_execution_mode(
    *,
    enabled: bool,
    shadow_enabled: bool,
    rollout_percent: int,
    user_id: str,
) -> str:
    """双轨决策：总开关关闭 → current；影子开 → shadow；灰度命中 → planned。"""
    if not enabled:
        return MODE_CURRENT
    if shadow_enabled:
        return MODE_SHADOW
    if user_in_rollout(user_id, rollout_percent):
        return MODE_PLANNED
    return MODE_CURRENT


# ═══════════════════════════════════════════════════════════════
# 影子模式 DB 代理
# ═══════════════════════════════════════════════════════════════


def _scalar_preview(value: Any, *, max_len: int = 80) -> Any:
    """差异日志的脱敏预览：dict 只保留键、截断字符串、隐藏敏感字段。"""
    if isinstance(value, dict):
        preview: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SENSITIVE_KEYS:
                preview[key] = "<redacted>"
            elif isinstance(item, str):
                preview[key] = item[:max_len]
            elif isinstance(item, (list, tuple)):
                preview[key] = f"<{len(item)} items>"
            else:
                preview[key] = item
        return preview
    if isinstance(value, str):
        return value[:max_len]
    if isinstance(value, (list, tuple)):
        return f"<{len(value)} items>"
    return value


class _ShadowCol:
    """影子集合：读操作透传，写操作拦截并记录差异。"""

    def __init__(self, col: Any, name: str, log_col: Any):
        self._col = col
        self._name = name
        self._log_col = log_col

    def find(self, *args: Any, **kwargs: Any):
        """motor find 是同步返回 cursor 的读操作，直接透传。"""
        return self._col.find(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name in _WRITE_OPS:
            return self._shadow_write(name)
        return getattr(self._col, name)

    def _shadow_write(self, op: str) -> Any:
        async def wrapped(*args: Any, **kwargs: Any) -> SimpleNamespace:
            try:
                await self._log_col.insert_one(
                    {
                        "op": op,
                        "collection": self._name,
                        "filter_or_doc": [_scalar_preview(a) for a in args][:8],
                        "kwargs_keys": list(kwargs.keys()),
                        "mode": MODE_SHADOW,
                        "created_at": _utc_now(),
                    }
                )
            except Exception:
                logger.exception("[shadow] diff log write failed")
            return SimpleNamespace(
                acknowledged=True,
                matched_count=0,
                modified_count=0,
                upserted_id=None,
            )

        return wrapped


class _ShadowDBProxy:
    """影子 DB：业务集合写操作被拦截，差异写入 planned_artifact_diffs。"""

    def __init__(self, db: Any):
        self._db = db
        self._log_col = db["planned_artifact_diffs"]
        self._cols: dict[str, _ShadowCol] = {}

    def __getitem__(self, name: str) -> Any:
        if name == "planned_artifact_diffs":
            return self._db[name]
        if name not in self._cols:
            self._cols[name] = _ShadowCol(self._db[name], name, self._log_col)
        return self._cols[name]


# ═══════════════════════════════════════════════════════════════
# MultiAgentRuntime
# ═══════════════════════════════════════════════════════════════


class MultiAgentReplayError(Exception):
    """人工重放被拒绝：步骤不可重放 / 输入已变更 / Worker 未注册。"""

    def __init__(self, message: str, *, code: str = "REPLAY_REJECTED"):
        super().__init__(message)
        self.code = code


class MultiAgentRuntime:
    """把 planner/validator/orchestrator/ledger/registry 组装为可复用运行时。

    影子模式通过 registry_for(shadow=True) 提供绑定到 shadow DB 的注册表，
    业务写操作不回填，仅记录差异。
    """

    def __init__(
        self,
        *,
        planner: Planner,
        validator: PlanValidator,
        orchestrator: Orchestrator,
        ledger: ExecutionStepLedger,
        registry: WorkerRegistry,
        db: Any = None,
        manager: Any = None,
    ):
        self.planner = planner
        self.validator = validator
        self.orchestrator = orchestrator
        self.ledger = ledger
        self.registry = registry
        self.db = db
        self.manager = manager
        self._shadow_registry: WorkerRegistry | None = None
        self._shadow_orchestrator: Orchestrator | None = None

    # ── 影子注册表（惰性构建，构建无 I/O）────────────────────

    def registry_for(self, shadow: bool = False) -> WorkerRegistry:
        if not shadow:
            return self.registry
        if self._shadow_registry is None:
            self._shadow_registry = build_default_registry(self.manager, _ShadowDBProxy(self.db))
        return self._shadow_registry

    def orchestrator_for(self, shadow: bool = False) -> Orchestrator:
        if not shadow:
            return self.orchestrator
        if self._shadow_orchestrator is None:
            self._shadow_orchestrator = Orchestrator(
                self.registry_for(shadow=True),
                owner_id=self.orchestrator.owner_id,
                max_concurrency=self.orchestrator.max_concurrency,
                user_concurrency=self.orchestrator.user_concurrency,
                provider_concurrency=self.orchestrator.provider_concurrency,
                worker_concurrency=self.orchestrator.worker_concurrency,
                lease_seconds=self.orchestrator.lease_seconds,
                default_max_attempts=self.orchestrator.default_max_attempts,
            )
        return self._shadow_orchestrator

    # ── Planner 上下文组装（全部来自服务端，模型不可提交）────

    async def build_planner_context(
        self,
        *,
        user_id: str,
        trace_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """从 DB 组装 PlannerInput 数据：产品目录 / 文章清单 / 风格偏好。"""
        state = state or {}
        products: list[dict[str, Any]] = []
        articles: list[PlannerArticleInput] = []
        if self.db is not None:
            try:
                cursor = self.db["user_products"].find({"user_id": user_id, "enabled": True})
                docs = await cursor.to_list(length=200)
                for doc in docs:
                    products.append(
                        {"id": str(doc.get("product_id") or ""), "name": str(doc.get("product_name") or "")}
                    )
            except Exception:
                logger.exception("[multi_agent] load products failed")
            try:
                cursor = self.db["articles"].find(
                    {"pipeline_status": {"$in": ["pending", "crawled"]}}
                )
                docs = await cursor.to_list(length=100)
                for doc in docs:
                    articles.append(
                        PlannerArticleInput(
                            id=str(doc.get("url_hash") or ""),
                            title=str(doc.get("title") or "")[:500],
                            summary=str(doc.get("summary") or doc.get("content_md") or "")[:2000],
                            status=str(doc.get("pipeline_status") or "pending"),
                        )
                    )
            except Exception:
                logger.exception("[multi_agent] load articles failed")

        style_hints: list[str] = []
        try:
            from agent.style_profiler import load_style_hints as _load_style_hints

            hint_text = await _load_style_hints(self.db, user_id)
            if hint_text:
                style_hints = [line.strip() for line in hint_text.splitlines() if line.strip()][:10]
        except Exception:
            logger.warning("[multi_agent] load style hints failed")

        return {
            "products": products,
            "articles": articles,
            "style_hints": style_hints,
            "score_threshold": int(state.get("score_threshold", 80)),
            "needs_fulltext_hint": bool(state.get("needs_enrich", False)),
        }

    # ── 人工重放 ──────────────────────────────────────────────

    async def replay_step(
        self,
        *,
        run_id: str,
        step_id: str,
        owner_id: str = "human-replay",
        user_id: str = "",
        trace_id: str = "",
        state: dict[str, Any] | None = None,
        verify_latest_input: Any = None,
    ) -> StepOutcome:
        """人工重放单个失败/死信步骤。

        - 只接受 ledger 中 status ∈ {failed, dead_lettered} 的步骤；
        - verify_latest_input(entry) 校验最新输入，拒绝过期重放；
        - 通过 ledger CAS 领取（fencing 递增），成功后 complete，失败回落账本。
        """
        entry = await self.ledger.get_step(run_id, step_id)
        if entry is None:
            raise MultiAgentReplayError(
                f"step not found: run={run_id} step={step_id}", code="STEP_NOT_FOUND"
            )
        if entry.status not in ("failed", "dead_lettered"):
            raise MultiAgentReplayError(
                f"only failed/dead_lettered steps are replayable, got {entry.status}",
                code="NOT_REPLAYABLE",
            )
        if verify_latest_input is not None:
            ok = await verify_latest_input(entry)
            if not ok:
                raise MultiAgentReplayError(
                    "input snapshot changed; replay rejected", code="INPUT_CHANGED"
                )
        adapter = self.registry.get(entry.worker)
        if adapter is None:
            raise MultiAgentReplayError(
                f"worker not registered: {entry.worker}", code="UNREGISTERED_WORKER"
            )

        claim = await self.ledger.begin_attempt(
            run_id=run_id,
            step_id=step_id,
            owner_id=owner_id,
            attempt=entry.attempt + 1,
            idempotency_key=entry.idempotency_key,
            input_hash=entry.input_hash,
        )
        lease = WorkerLease(
            owner_id=owner_id,
            run_id=run_id,
            step_id=step_id,
            expires_at=_utc_now() + timedelta(seconds=self.orchestrator.lease_seconds),
            fencing_token=claim.fencing_token,
        )
        ctx = {
            "run_id": run_id,
            "plan_id": entry.plan_id,
            "step_id": step_id,
            "worker": entry.worker,
            "attempt": claim.attempt,
            "user_id": user_id,
            "trace_id": trace_id,
            "input_refs": {},
        }
        result = await adapter.execute(state or {}, ctx, lease)

        if result.status == "succeeded":
            await self.ledger.complete(
                run_id=run_id,
                step_id=step_id,
                owner_id=owner_id,
                fencing_token=claim.fencing_token,
                result=result,
            )
            final_status = "succeeded"
        else:
            await self.ledger.fail(
                run_id=run_id,
                step_id=step_id,
                owner_id=owner_id,
                fencing_token=claim.fencing_token,
                status="dead_lettered" if result.retryable else "failed",
                error_type=result.error_type,
                error_message=result.error_message,
                retryable=result.retryable,
                result_hash=result.result_hash,
            )
            final_status = "dead_lettered" if result.retryable else "failed"
        return StepOutcome(
            step_id=step_id,
            worker=entry.worker,
            status=final_status,
            attempt=claim.attempt,
            error_type=result.error_type,
            error_message=result.error_message,
            idempotency_key=result.idempotency_key,
            input_hash=result.input_hash,
            result_hash=result.result_hash,
            duration_ms=result.duration_ms,
        )


# ═══════════════════════════════════════════════════════════════
# 运行时工厂
# ═══════════════════════════════════════════════════════════════


def build_multi_agent_runtime(
    *,
    db: Any,
    manager: Any,
    llm_wrapper: Any,
    settings: Any,
) -> MultiAgentRuntime:
    """按 settings 组装 MultiAgent 运行时（flag 默认关闭，灰度时开启）。"""
    registry = build_default_registry(manager, db)
    validator = PlanValidator(
        max_steps=settings.PLAN_MAX_STEPS,
        max_depth=settings.PLAN_MAX_DEPTH,
    )
    planner = Planner(
        llm_wrapper=llm_wrapper,
        db=db,
        enabled=True,
        planner_model=settings.PLANNER_MODEL,
        timeout_seconds=settings.PLANNER_TIMEOUT_SECONDS,
        validator=validator,
        planner_version=PLANNER_VERSION,
    )
    orchestrator = Orchestrator(
        registry,
        owner_id="multi-agent-worker",
        max_concurrency=settings.ORCHESTRATOR_MAX_CONCURRENCY,
        user_concurrency=settings.ORCHESTRATOR_USER_CONCURRENCY,
        lease_seconds=settings.WORKER_LEASE_SECONDS,
        default_max_attempts=settings.WORKER_MAX_ATTEMPTS,
    )
    ledger = ExecutionStepLedger(db, lease_seconds=settings.WORKER_LEASE_SECONDS)
    return MultiAgentRuntime(
        planner=planner,
        validator=validator,
        orchestrator=orchestrator,
        ledger=ledger,
        registry=registry,
        db=db,
        manager=manager,
    )
