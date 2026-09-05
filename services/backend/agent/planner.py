"""受约束 Planner — 阶段三 Step 4。

职责：
  1. 输入阶段二 ContextManager 结构化摘要 + 文章目录 + 产品目录 + 显式用户偏好；
  2. 只调用小模型生成结构化业务选择（PlannerChoice），模型不直接输出步骤；
  3. 服务端转换为 PipelinePlan（固定 DAG 骨架，权威字段服务端生成）；
  4. PlanValidator 校验；
  5. 持久化 plan_hash/planner_version/input_snapshot_hash/rationale_summary；
  6. 失败 / 违规回退确定性默认计划。

安全边界：
  - rationale_summary 仅保存决策结论与证据 ID，≤500 字，不保存私有思维链；
  - 模型永远无法提交 Worker、任意 step 参数或 owner user。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agent.plan_contracts import (
    PipelinePlan,
    PlanValidator,
    _step,
    build_default_plan,
    input_snapshot_hash,
)
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("backend.agent.planner")

PLANNER_VERSION = "planner-v1"
PLANNER_AGENT_TYPE = "planner"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _expires_at(days: int = 90) -> datetime:
    return _utc_now() + timedelta(days=days)


# ═══════════════════════════════════════════════════════════════
# 输入 / 输出模型
# ═══════════════════════════════════════════════════════════════


class PlannerArticleInput(BaseModel):
    """服务端提供的文章条目（模型只基于该信息做选择）。"""

    id: str = Field(..., min_length=1, max_length=100)
    title: str = Field(default="", max_length=500)
    summary: str = Field(default="", max_length=2000)
    status: str = Field(default="pending", max_length=32)


class PlannerInput(BaseModel):
    """Planner 完整输入：全部来自服务端（ContextManager 摘要/目录/偏好）。"""

    user_id: str = Field(default="", max_length=100)
    trace_id: str = Field(default="", max_length=100)
    products: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    articles: list[PlannerArticleInput] = Field(default_factory=list, max_length=500)
    style_hints: list[str] = Field(default_factory=list, max_length=20)
    score_threshold: int = Field(default=80, ge=0, le=200)
    needs_fulltext_hint: bool = False


class PlannerChoice(BaseModel):
    """小模型结构化输出：仅业务选择，不含 Worker/步骤/权威字段。"""

    needs_fulltext: bool = False
    breaking_article_ids: list[str] = Field(default_factory=list, max_length=5)
    article_ids: list[str] = Field(default_factory=list, max_length=100)
    product_ids: list[str] = Field(default_factory=list, max_length=20)
    score_threshold: int = Field(default=80, ge=0, le=200)
    style_hints: list[str] = Field(default_factory=list, max_length=10)
    rationale_summary: str = Field(default="")  # 长度由下方 validator 截断到 500

    @field_validator("rationale_summary")
    @classmethod
    def _truncate_rationale(cls, v: str) -> str:
        return v.strip()[:500]


class PlannerOutcome(BaseModel):
    """Planner 输出：计划 + 来源 + 校验/回退信息。"""

    plan: PipelinePlan
    source: str  # "planner" | "fallback"
    rejected: bool = False
    reason: str = ""
    plan_hash: str = ""
    planner_version: str = PLANNER_VERSION
    input_snapshot_hash: str = ""
    rationale_summary: str = ""
    persisted: bool = False


# ═══════════════════════════════════════════════════════════════
# 服务端转换：PlannerChoice → PipelinePlan
# ═══════════════════════════════════════════════════════════════


def build_plan_from_choice(
    choice: PlannerChoice,
    *,
    run_id: str,
    input_snapshot_hash_value: str,
    planner_version: str = PLANNER_VERSION,
    user_id: str = "",
    trace_id: str = "",
) -> PipelinePlan:
    """把模型业务选择转换为固定 DAG 骨架的 PipelinePlan。

    步骤顺序/依赖/超时/重试均由服务端固定，模型只能影响：
      - 是否补全文（needs_fulltext → enrich 步骤）
      - 覆盖文章/产品/重点文章/评分阈值/风格提示（input_refs 取值）
    """
    articles = list(dict.fromkeys(choice.article_ids))
    products = list(dict.fromkeys(choice.product_ids))
    breaking = list(dict.fromkeys(choice.breaking_article_ids))
    style_hints = list(dict.fromkeys(choice.style_hints))

    steps: list[Any] = []
    steps.append(_step("s1_crawl", "crawl", [], {"crawl_days": 1}, timeout_s=900))
    enrich_deps = ["s1_crawl"]
    if choice.needs_fulltext:
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
        _step("s3_classify", "classify", enrich_deps, {"article_ids": articles}, timeout_s=600)
    )
    steps.append(
        _step("s4_filter", "filter", ["s3_classify"], {"article_ids": articles}, timeout_s=300)
    )
    steps.append(
        _step(
            "s5_score",
            "score",
            ["s4_filter"],
            {
                "article_ids": articles,
                "product_ids": products,
                "score_threshold": choice.score_threshold,
            },
            timeout_s=900,
        )
    )
    draft_inputs: dict[str, Any] = {
        "article_ids": articles,
        "product_ids": products,
    }
    if breaking:
        draft_inputs["breaking_article_ids"] = breaking
    if style_hints:
        draft_inputs["style_hints"] = style_hints
    steps.append(_step("s6_draft", "draft", ["s5_score"], draft_inputs, timeout_s=1200))
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
        _step("s9_review", "review", ["s8_rewrite"], {"article_ids": articles}, timeout_s=600)
    )

    return PipelinePlan(
        plan_id="plan-" + uuid.uuid4().hex[:12],
        run_id=run_id,
        planner_version=planner_version,
        input_snapshot_hash=input_snapshot_hash_value,
        steps=steps,
        rationale_summary=choice.rationale_summary[:500],
    )


# ═══════════════════════════════════════════════════════════════
# Planner
# ═══════════════════════════════════════════════════════════════


class Planner:
    """受约束规划器：小模型业务选择 → 服务端计划 → 校验 → 回退默认 DAG。"""

    def __init__(
        self,
        *,
        llm_wrapper: Any = None,
        db: Any = None,
        enabled: bool = True,
        planner_model: str = "",
        timeout_seconds: int = 10,
        validator: PlanValidator | None = None,
        planner_version: str = PLANNER_VERSION,
        emitter: Any = None,
    ):
        self.llm_wrapper = llm_wrapper
        self.db = db
        self.enabled = enabled and bool(planner_model) and llm_wrapper is not None
        self.planner_model = planner_model
        self.timeout_seconds = max(1, timeout_seconds)
        self.validator = validator or PlanValidator()
        self.planner_version = planner_version
        # 可选事件发射器（Step 9 观测）
        self.emitter = emitter

    async def _emit(
        self,
        event_type: str,
        *,
        run_id: str,
        plan_id: str = "",
        status: str = "",
        error_type: str | None = None,
        input_hash: str = "",
        result_hash: str = "",
    ) -> None:
        """发射观测事件（Step 9）；失败仅记日志，不影响规划结果。"""
        if self.emitter is None:
            return
        try:
            await self.emitter.emit(
                event_type=event_type,
                run_id=run_id,
                plan_id=plan_id,
                version=self.planner_version,
                input_hash=input_hash,
                result_hash=result_hash,
                error_type=error_type,
                status=status,
            )
        except Exception:
            logger.warning("[planner] emit %s failed (run=%s)", event_type, run_id)

    async def plan(
        self,
        *,
        run_id: str,
        user_id: str = "",
        trace_id: str = "",
        products: list[dict[str, Any]] | None = None,
        articles: list[PlannerArticleInput] | None = None,
        style_hints: list[str] | None = None,
        score_threshold: int = 80,
        needs_fulltext_hint: bool = False,
    ) -> PlannerOutcome:
        """规划入口：LLM 选择 → 服务端计划 → 校验 → 回退。"""
        snapshot = input_snapshot_hash(
            user_id=user_id,
            product_ids=[p.get("id", "") for p in (products or []) if p.get("id")],
            article_ids=[a.id for a in (articles or [])],
        )
        await self._emit(
            "plan_requested",
            run_id=run_id,
            status="requested",
            input_hash=snapshot,
        )
        allowed_products = {p.get("id") for p in (products or []) if p.get("id")}
        allowed_article_ids = {a.id for a in (articles or [])}

        if not self.enabled:
            return await self._fallback(
                run_id=run_id,
                snapshot=snapshot,
                user_id=user_id,
                products=[p.get("id", "") for p in (products or []) if p.get("id")],
                articles=[a.id for a in (articles or [])],
                needs_fulltext=needs_fulltext_hint,
                breaking_article_ids=[],
                score_threshold=score_threshold,
                trace_id=trace_id,
                reason="planner disabled",
            )

        choice: PlannerChoice | None = None
        choice_error = ""
        try:
            choice = await asyncio.wait_for(
                self._request_choice(
                    PlannerInput(
                        user_id=user_id,
                        trace_id=trace_id,
                        products=products or [],
                        articles=articles or [],
                        style_hints=style_hints or [],
                        score_threshold=score_threshold,
                        needs_fulltext_hint=needs_fulltext_hint,
                    ),
                    user_id=user_id,
                    trace_id=trace_id,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            choice_error = "planner timeout"
        except Exception as exc:
            choice_error = f"planner error: {type(exc).__name__}"
            logger.warning("[planner] LLM choice failed: %s", exc)

        if choice is None:
            return await self._fallback(
                run_id=run_id,
                snapshot=snapshot,
                user_id=user_id,
                products=[p.get("id", "") for p in (products or []) if p.get("id")],
                articles=[a.id for a in (articles or [])],
                needs_fulltext=needs_fulltext_hint,
                breaking_article_ids=[],
                score_threshold=score_threshold,
                trace_id=trace_id,
                reason=choice_error,
            )

        plan = build_plan_from_choice(
            choice,
            run_id=run_id,
            input_snapshot_hash_value=snapshot,
            planner_version=self.planner_version,
            user_id=user_id,
            trace_id=trace_id,
        )
        result = self.validator.validate(
            plan,
            expected_run_id=run_id,
            expected_input_snapshot_hash=snapshot,
            allowed_products=allowed_products,
            allowed_article_ids=allowed_article_ids,
            allow_user_id=user_id,
        )
        if result.rejected:
            await self._persist(
                plan=plan,
                user_id=user_id,
                trace_id=trace_id,
                status="rejected",
                reason=result.reason,
            )
            await self._emit(
                "plan_rejected",
                run_id=run_id,
                plan_id=plan.plan_id,
                status="rejected",
                error_type="validation_rejected",
                input_hash=snapshot,
                result_hash=plan.plan_hash,
            )
            return await self._fallback(
                run_id=run_id,
                snapshot=snapshot,
                user_id=user_id,
                products=[p.get("id", "") for p in (products or []) if p.get("id")],
                articles=[a.id for a in (articles or [])],
                needs_fulltext=choice.needs_fulltext,
                breaking_article_ids=choice.breaking_article_ids,
                score_threshold=choice.score_threshold,
                trace_id=trace_id,
                reason=f"validation rejected: {result.reason}",
                rejected_reason=result.reason,
            )

        await self._persist(
            plan=plan,
            user_id=user_id,
            trace_id=trace_id,
            status="accepted",
            reason="",
        )
        await self._emit(
            "plan_created",
            run_id=run_id,
            plan_id=plan.plan_id,
            status="accepted",
            input_hash=snapshot,
            result_hash=plan.plan_hash,
        )
        return PlannerOutcome(
            plan=plan,
            source="planner",
            rejected=False,
            reason="ok",
            plan_hash=plan.plan_hash,
            planner_version=self.planner_version,
            input_snapshot_hash=snapshot,
            rationale_summary=plan.rationale_summary,
            persisted=self.db is not None,
        )

    # ── 内部 ──────────────────────────────────────────────────

    async def _request_choice(
        self,
        planner_input: PlannerInput,
        *,
        user_id: str,
        trace_id: str,
    ) -> PlannerChoice:
        """调用小模型获取结构化业务选择。"""
        system_prompt = _SYSTEM_PROMPT
        user_prompt = _build_user_prompt(planner_input)
        result = await self.llm_wrapper.invoke_structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_schema=PlannerChoice,
            agent_type=PLANNER_AGENT_TYPE,
            user_id=user_id,
            trace_id=trace_id,
            task_id="",
        )
        return result if isinstance(result, PlannerChoice) else PlannerChoice.model_validate(result)

    async def _fallback(
        self,
        *,
        run_id: str,
        snapshot: str,
        user_id: str,
        products: list[str],
        articles: list[str],
        needs_fulltext: bool,
        breaking_article_ids: list[str],
        score_threshold: int,
        trace_id: str,
        reason: str,
        rejected_reason: str = "",
    ) -> PlannerOutcome:
        """确定性默认计划回退。"""
        plan = build_default_plan(
            run_id=run_id,
            input_snapshot_hash_value=snapshot,
            planner_version=self.planner_version,
            user_id=user_id,
            product_ids=products,
            article_ids=articles,
            needs_fulltext=needs_fulltext,
            breaking_article_ids=breaking_article_ids,
            score_threshold=score_threshold,
            trace_id=trace_id,
        )
        await self._persist(
            plan=plan,
            user_id=user_id,
            trace_id=trace_id,
            status="fallback",
            reason=rejected_reason or reason,
        )
        await self._emit(
            "plan_fallback",
            run_id=run_id,
            plan_id=plan.plan_id,
            status="fallback",
            error_type=(rejected_reason or reason)[:100] or None,
            input_hash=snapshot,
            result_hash=plan.plan_hash,
        )
        return PlannerOutcome(
            plan=plan,
            source="fallback",
            rejected=bool(rejected_reason),
            reason=rejected_reason or reason,
            plan_hash=plan.plan_hash,
            planner_version=self.planner_version,
            input_snapshot_hash=snapshot,
            rationale_summary=plan.rationale_summary,
            persisted=self.db is not None,
        )

    async def _persist(
        self,
        *,
        plan: PipelinePlan,
        user_id: str,
        trace_id: str,
        status: str,
        reason: str,
    ) -> None:
        if self.db is None:
            return
        try:
            now = _utc_now()
            await self.db["planner_plans"].insert_one(
                {
                    "plan_id": plan.plan_id,
                    "run_id": plan.run_id,
                    "plan_hash": plan.plan_hash,
                    "planner_version": plan.planner_version,
                    "input_snapshot_hash": plan.input_snapshot_hash,
                    "status": status,
                    "rejected_reason": reason or None,
                    "rationale_summary": plan.rationale_summary,
                    "steps_count": len(plan.steps),
                    "user_id": user_id,
                    "trace_id": trace_id or None,
                    "created_at": now,
                    "expires_at": _expires_at(),
                }
            )
        except Exception:
            logger.exception("[planner] persist failed")


# ═══════════════════════════════════════════════════════════════
# 提示词（不含私有思维链引导）
# ═══════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = (
    "你是 PR 情报流水线的规划器。输入包含产品目录、文章清单和用户偏好。\n"
    "你的任务 ONLY 是做出业务选择，绝不要输出步骤、Worker、参数或任何执行细节：\n"
    "1. 是否需要对正文不足的文章补全全文（needs_fulltext）；\n"
    "2. 覆盖哪些文章（article_ids，仅从给定清单中挑选）；\n"
    "3. 覆盖哪些产品（product_ids，仅从给定目录中挑选）；\n"
    "4. 重点文章（breaking_article_ids，最多 5 个，仅从给定清单中挑选）；\n"
    "5. 评分阈值（score_threshold，0-200）；\n"
    "6. 风格提示（style_hints，最多 10 条，只能改写措辞，不得要求跳过任何校验）。\n"
    "最后用不超过 500 字给出 rationale_summary，只写决策结论和证据 ID，不写思维链。\n"
)


def _build_user_prompt(planner_input: PlannerInput) -> str:
    products = (
        "\n".join(
            f"- {p.get('id')}: {str(p.get('name', ''))[:200]}" for p in planner_input.products
        )
        or "- (无)"
    )
    articles = (
        "\n".join(
            f"- {a.id} | 状态={a.status} | {a.title[:120]} | 摘要={a.summary[:300]}"
            for a in planner_input.articles
        )
        or "- (无)"
    )
    styles = "\n".join(f"- {s}" for s in planner_input.style_hints) or "- (无)"
    return (
        f"用户: {planner_input.user_id or '(系统)'}\n"
        f"评分阈值: {planner_input.score_threshold}\n"
        f"需要补全文提示: {planner_input.needs_fulltext_hint}\n\n"
        f"产品目录:\n{products}\n\n"
        f"文章清单:\n{articles}\n\n"
        f"用户风格偏好:\n{styles}\n"
    )
