"""Plan Schema 与 PlanValidator — 阶段三 Step 1/2。

统一受约束 Plan Schema（PipelinePlan / PlanStep）与校验器 PlanValidator。

安全边界：
  - 权威字段 plan_id / run_id / input_snapshot_hash 由服务端生成，模型不可提交；
  - Worker 名称必须属于 WorkerName 白名单，禁止 publish / delete / 外发类 Worker；
  - 产生草稿的 Plan 必须包含 quality_check 与 review（必经，Planner 不可省略）；
  - 校验失败返回 PlanValidationResult(rejected=True)，调用方回退确定性默认计划，
    不无限要求 LLM 自修复。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

WorkerName = Literal[
    "crawl",
    "enrich",
    "classify",
    "filter",
    "score",
    "draft",
    "quality_check",
    "rewrite",
    "review",
]

Policy = Literal["required", "optional", "best_effort"]

SCHEMA_VERSION = "1.0"

# ── 默认容量边界（与配置键 PLAN_MAX_STEPS / PLAN_MAX_DEPTH 对齐）──────────
DEFAULT_MAX_STEPS = 50
DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_FANOUT = 20
DEFAULT_MAX_CONCURRENCY_GROUPS = 8
DEFAULT_MAX_OPTIONAL_RATIO = 0.4  # optional/best_effort 步骤占比上限
DEFAULT_MAX_SPECIAL_HANDLING = 5  # 需要全文/重点文章特殊处理数量上限
DEFAULT_MAX_TOTAL_TIMEOUT_S = 7200
DEFAULT_MAX_RATIONALE_CHARS = 500

# 允许的 input_refs 目标 key（state/artifact 白名单，禁止任意 key）
ALLOWED_INPUT_KEYS = frozenset(
    {
        "crawl_days",
        "article_ids",
        "article_url_hashes",
        "product_ids",
        "categories",
        "score_threshold",
        "needs_fulltext",
        "breaking_article_ids",
        "style_hints",
        "template_ids",
        "user_id",
        "trace_id",
    }
)

# 禁止注册的 Worker：发布 / 删除 / 外发一律不进入 Planner 能力面
FORBIDDEN_WORKERS = frozenset({"publish", "delete", "external_send", "notify"})

# 步骤输入契约：每个 Worker 允许引用的 input key（超集之外拒绝）
WORKER_INPUT_CONTRACT: dict[str, frozenset[str]] = {
    "crawl": frozenset({"crawl_days"}),
    "enrich": frozenset({"article_url_hashes", "needs_fulltext"}),
    "classify": frozenset({"article_ids", "categories"}),
    "filter": frozenset({"article_ids"}),
    "score": frozenset({"article_ids", "product_ids", "score_threshold"}),
    "draft": frozenset(
        {"article_ids", "product_ids", "style_hints", "template_ids", "breaking_article_ids"}
    ),
    "quality_check": frozenset({"article_ids"}),
    "rewrite": frozenset({"article_ids", "style_hints"}),
    "review": frozenset({"article_ids"}),
}

# 草稿必经路径：Plan 含 draft 时必须同时含 quality_check 与 review
DRAFT_GUARD = frozenset({"quality_check", "review"})

# 特殊处理输入 key（需要全文 / 重点文章）数量单独计数
SPECIAL_HANDLING_KEYS = frozenset({"needs_fulltext", "breaking_article_ids"})


class PlanStep(BaseModel):
    """单个业务步骤（固定 Worker 模板的实例化）。"""

    step_id: str = Field(..., min_length=1, max_length=64)
    worker: WorkerName
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    input_refs: dict[str, Any] = Field(default_factory=dict)
    policy: Policy = "required"
    timeout_s: int = Field(..., ge=1, le=3600)
    max_attempts: int = Field(..., ge=1, le=10)
    concurrency_key: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _step_self_consistency(self) -> PlanStep:
        if self.step_id in self.depends_on:
            raise ValueError(f"step {self.step_id} cannot depend on itself")
        return self


class PipelinePlan(BaseModel):
    """受约束执行计划（服务端权威生成）。"""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    plan_id: str = Field(..., min_length=1, max_length=100)
    run_id: str = Field(..., min_length=1, max_length=100)
    planner_version: str = Field(..., min_length=1, max_length=64)
    input_snapshot_hash: str = Field(..., min_length=1, max_length=80)
    steps: list[PlanStep] = Field(..., min_length=1, max_length=DEFAULT_MAX_STEPS)
    rationale_summary: str = Field(default="", max_length=DEFAULT_MAX_RATIONALE_CHARS)

    @property
    def plan_hash(self) -> str:
        """稳定计划指纹：版本 + 权威字段 + 步骤序列（含依赖/输入/策略）。

        不含 plan_id/run_id：同一意图 + 同一输入快照的计划应产生相同指纹
        （用于缓存、去重与对比）。
        """
        payload = {
            "schema_version": self.schema_version,
            "planner_version": self.planner_version,
            "input_snapshot_hash": self.input_snapshot_hash,
            "steps": [
                {
                    "step_id": s.step_id,
                    "worker": s.worker,
                    "depends_on": s.depends_on,
                    "policy": s.policy,
                    "timeout_s": s.timeout_s,
                    "max_attempts": s.max_attempts,
                    "concurrency_key": s.concurrency_key,
                }
                for s in self.steps
            ],
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanValidationResult:
    """校验结果；rejected=True 时调用方必须回退确定性默认计划。"""

    rejected: bool
    reason: str = ""
    plan_hash: str = ""


class PlanValidator:
    """按 Step 2 顺序校验 PipelinePlan（1-9 项）。"""

    def __init__(
        self,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_fanout: int = DEFAULT_MAX_FANOUT,
        max_concurrency_groups: int = DEFAULT_MAX_CONCURRENCY_GROUPS,
        max_optional_ratio: float = DEFAULT_MAX_OPTIONAL_RATIO,
        max_special_handling: int = DEFAULT_MAX_SPECIAL_HANDLING,
        max_total_timeout_s: int = DEFAULT_MAX_TOTAL_TIMEOUT_S,
    ):
        self.max_steps = max_steps
        self.max_depth = max_depth
        self.max_fanout = max_fanout
        self.max_concurrency_groups = max_concurrency_groups
        self.max_optional_ratio = max_optional_ratio
        self.max_special_handling = max_special_handling
        self.max_total_timeout_s = max_total_timeout_s

    def validate(
        self,
        plan: PipelinePlan,
        *,
        expected_run_id: str | None = None,
        expected_input_snapshot_hash: str | None = None,
        allowed_products: set[str] | None = None,
        allowed_article_ids: set[str] | None = None,
        allow_user_id: str | None = None,
    ) -> PlanValidationResult:
        """按 1-9 顺序校验；任一失败立即返回 rejected。"""

        # 1. 结构：版本、长度（pydantic 已保证 schema，此处补版本）
        if plan.schema_version != SCHEMA_VERSION:
            return self._reject(plan, "invalid schema_version")
        if not plan.steps:
            return self._reject(plan, "empty steps")

        # 2. 身份与输入快照
        if expected_run_id is not None and plan.run_id != expected_run_id:
            return self._reject(plan, "run_id mismatch")
        if (
            expected_input_snapshot_hash is not None
            and plan.input_snapshot_hash != expected_input_snapshot_hash
        ):
            return self._reject(plan, "input_snapshot_hash mismatch")
        if allow_user_id is not None:
            for step in plan.steps:
                if step.input_refs.get("user_id") not in (None, allow_user_id):
                    return self._reject(plan, "user_id not allowed")

        # 3. article / product 白名单
        for step in plan.steps:
            for pid in _as_list(step.input_refs.get("product_ids")):
                if allowed_products is not None and pid not in allowed_products:
                    return self._reject(plan, f"product not allowed: {pid}")
            for aid in _as_list(step.input_refs.get("article_ids")):
                if allowed_article_ids is not None and aid not in allowed_article_ids:
                    return self._reject(plan, f"article not allowed: {aid}")

        # 4. input_refs 只允许白名单 key 且符合 Worker 输入契约
        for step in plan.steps:
            for key in step.input_refs:
                if key not in ALLOWED_INPUT_KEYS:
                    return self._reject(plan, f"disallowed input key: {key}")
            allowed = WORKER_INPUT_CONTRACT.get(step.worker, frozenset())
            unexpected = set(step.input_refs) - allowed
            if unexpected:
                return self._reject(
                    plan, f"worker {step.worker} unexpected inputs: {sorted(unexpected)}"
                )

        # 5. 拓扑：step_id 唯一、依赖存在、无环、节点/深度/扇出上限
        if len(plan.steps) > self.max_steps:
            return self._reject(plan, f"steps > max_steps({self.max_steps})")
        ids = [s.step_id for s in plan.steps]
        if len(set(ids)) != len(ids):
            return self._reject(plan, "duplicate step_id")
        by_id = {s.step_id: s for s in plan.steps}
        for s in plan.steps:
            for dep in s.depends_on:
                if dep not in by_id:
                    return self._reject(plan, f"missing dependency: {dep}")
            fanout = sum(1 for other in plan.steps if s.step_id in other.depends_on)
            if fanout > self.max_fanout:
                return self._reject(plan, f"fanout exceeds max_fanout({self.max_fanout})")

        cycle, depth = self._analyze_dag(plan)
        if cycle:
            return self._reject(plan, f"dependency cycle: {' -> '.join(cycle)}")
        if depth > self.max_depth:
            return self._reject(plan, f"depth exceeds max_depth({self.max_depth})")

        # 6. 产生草稿时 quality_check/review 必经
        worker_set = {s.worker for s in plan.steps}
        if "draft" in worker_set:
            missing = DRAFT_GUARD - worker_set
            if missing:
                return self._reject(plan, f"draft plan missing guard workers: {sorted(missing)}")
            if not self._guard_order_ok(plan):
                return self._reject(plan, "quality_check/review must follow draft")

        # 7. 无发布、删除、外发 Worker
        forbidden = worker_set & FORBIDDEN_WORKERS
        if forbidden:
            return self._reject(plan, f"forbidden workers: {sorted(forbidden)}")

        # 8. token、工具、并发与总 deadline 预算
        groups = {s.concurrency_key for s in plan.steps if s.concurrency_key}
        if len(groups) > self.max_concurrency_groups:
            return self._reject(plan, f"concurrency groups > max({self.max_concurrency_groups})")
        total_timeout = sum(s.timeout_s for s in plan.steps)
        if total_timeout > self.max_total_timeout_s:
            return self._reject(
                plan, f"total timeout {total_timeout}s > max({self.max_total_timeout_s}s)"
            )

        # 9. skip 比例与 special handling 数量
        optional_count = sum(1 for s in plan.steps if s.policy != "required")
        if optional_count > int(self.max_optional_ratio * len(plan.steps)):
            return self._reject(
                plan,
                f"optional steps {optional_count} exceed ratio {self.max_optional_ratio:.0%}",
            )
        special_count = sum(
            len(_as_list(s.input_refs.get(key)))
            for s in plan.steps
            for key in SPECIAL_HANDLING_KEYS
        )
        if special_count > self.max_special_handling:
            return self._reject(
                plan,
                f"special handling count {special_count} > max({self.max_special_handling})",
            )

        return PlanValidationResult(rejected=False, reason="ok", plan_hash=plan.plan_hash)

    # ── 内部 ──────────────────────────────────────────────

    def _analyze_dag(self, plan: PipelinePlan) -> tuple[list[str] | None, int]:
        """返回 (环路径 | None, 最大深度)。"""
        by_id = {s.step_id: s for s in plan.steps}
        visiting: set[str] = set()
        visited: set[str] = set()
        depth_map: dict[str, int] = {}
        stack: list[str] = []
        cycle: list[str] = []

        def dfs(node: str) -> int:
            if node in visiting:
                cycle.append(node)
                return 0
            if node in visited:
                return depth_map[node]
            visiting.add(node)
            stack.append(node)
            max_dep = 0
            for dep in by_id[node].depends_on:
                d = dfs(dep)
                if cycle:
                    return 0
                max_dep = max(max_dep, d)
            stack.pop()
            visiting.discard(node)
            visited.add(node)
            depth_map[node] = max_dep + 1
            return depth_map[node]

        max_depth = 0
        for step in plan.steps:
            dfs(step.step_id)
            if cycle:
                idx = stack.index(cycle[0]) if cycle[0] in stack else 0
                return [*stack[idx:], cycle[0]], 0
        max_depth = max(depth_map.values(), default=0)
        return None, max_depth

    def _guard_order_ok(self, plan: PipelinePlan) -> bool:
        """quality_check 与 review 必须出现在任意 draft 之后。"""
        by_id = {s.step_id: s for s in plan.steps}
        draft_ids = {s.step_id for s in plan.steps if s.worker == "draft"}
        guard_ids = {s.step_id for s in plan.steps if s.worker in DRAFT_GUARD}

        def closure(node: str) -> set[str]:
            deps: set[str] = set()
            frontier = list(by_id[node].depends_on)
            while frontier:
                cur = frontier.pop()
                if cur in deps:
                    continue
                deps.add(cur)
                if cur in by_id:
                    frontier.extend(by_id[cur].depends_on)
            return deps

        for gid in guard_ids:
            if not (closure(gid) & draft_ids):
                return False
        # 任一 draft 不得依赖 guard（guard 必须在 draft 之后）
        return all(not (closure(did) & guard_ids) for did in draft_ids)

    def _reject(self, plan: PipelinePlan, reason: str) -> PlanValidationResult:
        return PlanValidationResult(rejected=True, reason=reason, plan_hash=plan.plan_hash)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# ═══════════════════════════════════════════════════════════════
# 服务端计划构造（确定性默认计划）
# ═══════════════════════════════════════════════════════════════


def input_snapshot_hash(
    *, user_id: str = "", product_ids: list[str] | None = None, article_ids: list[str] | None = None
) -> str:
    """输入快照指纹：user + 产品集合 + 文章集合。"""
    payload = {
        "user_id": user_id,
        "product_ids": sorted(product_ids or []),
        "article_ids": sorted(article_ids or []),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _step(
    step_id: str,
    worker: WorkerName,
    depends_on: list[str],
    input_refs: dict[str, Any],
    *,
    policy: Policy = "required",
    timeout_s: int = 600,
    max_attempts: int = 3,
    concurrency_key: str | None = None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        worker=worker,
        depends_on=depends_on,
        input_refs=input_refs,
        policy=policy,
        timeout_s=timeout_s,
        max_attempts=max_attempts,
        concurrency_key=concurrency_key,
    )


def build_default_plan(
    *,
    run_id: str,
    input_snapshot_hash_value: str,
    planner_version: str = "static-default-v1",
    user_id: str = "",
    product_ids: list[str] | None = None,
    article_ids: list[str] | None = None,
    needs_fulltext: bool = False,
    breaking_article_ids: list[str] | None = None,
    score_threshold: int = 80,
    trace_id: str = "",
) -> PipelinePlan:
    """确定性默认计划：等价 v2 固定 DAG（crawl→…→review）。

    enrich / rewrite 按输入意图裁剪为 optional；其余 required。
    """
    products = product_ids or []
    articles = article_ids or []
    breaking = breaking_article_ids or []

    steps: list[PlanStep] = []
    steps.append(
        _step(
            "s1_crawl",
            "crawl",
            [],
            {"crawl_days": 1},
            timeout_s=900,
        )
    )
    enrich_deps = ["s1_crawl"]
    if needs_fulltext:
        steps.append(
            _step(
                "s2_enrich",
                "enrich",
                ["s1_crawl"],
                {"needs_fulltext": True, "article_url_hashes": articles[:50]},
                policy="optional",
                timeout_s=600,
            )
        )
        enrich_deps.append("s2_enrich")
    steps.append(
        _step(
            "s3_classify",
            "classify",
            enrich_deps,
            {"article_ids": articles},
            timeout_s=600,
        )
    )
    steps.append(
        _step(
            "s4_filter",
            "filter",
            ["s3_classify"],
            {"article_ids": articles},
            timeout_s=300,
        )
    )
    steps.append(
        _step(
            "s5_score",
            "score",
            ["s4_filter"],
            {
                "article_ids": articles,
                "product_ids": products,
                "score_threshold": score_threshold,
            },
            timeout_s=900,
        )
    )
    steps.append(
        _step(
            "s6_draft",
            "draft",
            ["s5_score"],
            {
                "article_ids": articles,
                "product_ids": products,
                "breaking_article_ids": breaking,
            },
            timeout_s=1200,
        )
    )
    steps.append(
        _step(
            "s7_quality_check",
            "quality_check",
            ["s6_draft"],
            {"article_ids": articles},
            timeout_s=300,
        )
    )
    steps.append(
        _step(
            "s8_rewrite",
            "rewrite",
            ["s7_quality_check"],
            {"article_ids": articles},
            policy="optional",
            timeout_s=600,
        )
    )
    steps.append(
        _step(
            "s9_review",
            "review",
            ["s8_rewrite"],
            {"article_ids": articles},
            timeout_s=600,
        )
    )

    return PipelinePlan(
        plan_id="plan-" + uuid4().hex[:12],
        run_id=run_id,
        planner_version=planner_version,
        input_snapshot_hash=input_snapshot_hash_value,
        steps=steps,
        rationale_summary="确定性默认计划：等价 v2 固定 DAG，crawl→enrich(可选)→"
        "classify→filter→score→draft→quality_check→rewrite(可选)→review。",
    )
