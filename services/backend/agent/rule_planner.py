"""Rule-first Planner v1 for core conversational PR journeys."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from agent.business_tools.contracts import BusinessToolRegistry, build_business_tool_registry
from agent.contracts.task import SlotStatus, TaskEnvelope, TaskIntent
from agent.production_plan import (
    ArgumentBinding,
    BindingSource,
    Plan,
    PlanRetryPolicy,
    PlanStep,
    ProductionPlanValidation,
    ProductionPlanValidator,
    StepRisk,
)
from pydantic import BaseModel

PLANNER_VERSION = "rule-first-v1"


class OptionalBranchChoice(BaseModel):
    use_crawl: bool = False
    include_export: bool = False


class BranchChooser(Protocol):
    async def choose(self, task: TaskEnvelope) -> OptionalBranchChoice: ...


class RulePlannerOutcome(BaseModel):
    plan: Plan
    validation: ProductionPlanValidation
    source: str
    fallback_reason: str = ""


def _slot(key: str, *, path: str = "") -> ArgumentBinding:
    return ArgumentBinding(source=BindingSource.CONFIRMED_SLOT, key=key, path=path)


def _obs(key: str, step_id: str, path: str) -> ArgumentBinding:
    return ArgumentBinding(source=BindingSource.OBSERVATION, key=key, step_id=step_id, path=path)


def _server(key: str, value: Any) -> ArgumentBinding:
    return ArgumentBinding(source=BindingSource.SERVER_VALUE, key=key, value=value)


def _step(
    step_id: str,
    goal: str,
    tool: str,
    args: dict[str, ArgumentBinding],
    *,
    dependencies: list[str] | None = None,
    expected: list[str],
    acceptance: list[str],
    risk: StepRisk = StepRisk.LOW,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        goal=goal,
        tool=tool,
        args_binding=args,
        dependencies=list(dependencies or []),
        preconditions=["dependencies_completed"] if dependencies else ["task_snapshot_frozen"],
        expected_observation=expected,
        acceptance=acceptance,
        risk=risk,
        retry_policy=PlanRetryPolicy(max_attempts=2),
        timeout_seconds=180 if tool in {"generate_draft", "revise_draft"} else 90,
        estimated_cost_units=20 if tool in {"generate_draft", "revise_draft"} else 5,
    )


class RulePlannerV1:
    def __init__(
        self,
        registry: BusinessToolRegistry | None = None,
        *,
        branch_chooser: BranchChooser | None = None,
        validator: ProductionPlanValidator | None = None,
    ):
        self.registry = registry or build_business_tool_registry()
        self.branch_chooser = branch_chooser
        self.validator = validator or ProductionPlanValidator(self.registry)

    async def plan(
        self,
        task: TaskEnvelope,
        *,
        run_id: str | None = None,
        confirmed_slot_names: set[str] | None = None,
    ) -> RulePlannerOutcome:
        intent = str(task.intent.value or TaskIntent.UNKNOWN.value)
        confirmed = {
            name
            for name, slot in task.slot_states().items()
            if slot.status == SlotStatus.CONFIRMED and slot.value not in (None, "", [], {})
        }
        confirmed.update(confirmed_slot_names or set())
        choice = OptionalBranchChoice()
        source = "rules"
        fallback_reason = ""
        if self.branch_chooser is not None:
            try:
                choice = await self.branch_chooser.choose(task)
                source = "rules+model-branches"
            except Exception as exc:
                fallback_reason = f"branch_chooser_failed:{type(exc).__name__}"
        if bool(task.crawl_approved.value):
            choice = choice.model_copy(update={"use_crawl": True})
            source = "rules+user-approved-crawl"

        effective_run_id = run_id or task.task_id
        steps, direct_pipeline = self._build(intent, confirmed, choice, effective_run_id)
        plan = Plan(
            plan_id="plan-" + uuid.uuid4().hex[:12],
            run_id=effective_run_id,
            intent=intent,
            planner_version=PLANNER_VERSION,
            task_snapshot_hash=task.fingerprint(),
            steps=steps,
            direct_pipeline=direct_pipeline,
            estimated_cost_units=sum(step.estimated_cost_units for step in steps),
        )
        validation = self.validator.validate(
            plan,
            confirmed_slots=confirmed,
            expected_run_id=effective_run_id,
            expected_task_snapshot_hash=task.fingerprint(),
        )
        if not validation.accepted:
            raise ValueError(f"rule plan rejected: {validation.reason_code}: {validation.reason}")
        return RulePlannerOutcome(
            plan=plan, validation=validation, source=source, fallback_reason=fallback_reason
        )

    def _build(
        self,
        intent: str,
        confirmed: set[str],
        choice: OptionalBranchChoice,
        run_id: str,
    ) -> tuple[list[PlanStep], str]:
        if intent in {TaskIntent.REVIEW_DRAFT.value}:
            return [
                _step(
                    "review",
                    "Review the immutable draft artifact",
                    "review_draft",
                    {"artifact": _slot("draft_artifact")},
                    expected=["artifact.artifact_id", "content_hash", "passed"],
                    acceptance=["review result is bound to the draft content hash"],
                )
            ], ""
        if intent in {TaskIntent.REVISE_DRAFT.value, TaskIntent.REVISE.value}:
            return self._revision_steps(run_id), ""
        if intent in {TaskIntent.SAVE_DRAFT.value, TaskIntent.SAVE.value}:
            return [self._save_step(run_id)], ""
        if intent == TaskIntent.EXPORT_DRAFT.value:
            return [
                _step(
                    "export",
                    "Export one immutable draft version",
                    "export_draft",
                    {
                        "artifact": _slot("draft_artifact"),
                        "format": _server("format", "markdown"),
                        "filename": _server("filename", "draft"),
                        "idempotency_key": _server("idempotency_key", f"{run_id}-export"),
                    },
                    expected=["artifact.artifact_id", "export_ref", "content_hash"],
                    acceptance=["export references the requested immutable version"],
                    risk=StepRisk.MEDIUM,
                )
            ], ""

        search = (
            intent
            in {
                TaskIntent.SEARCH_AND_DRAFT.value,
                TaskIntent.CURATE_NEWS.value,
                TaskIntent.SEARCH_AND_RANK.value,
            }
            and "selected_article_ids" not in confirmed
        )
        draft = intent in {TaskIntent.GENERATE_DRAFT.value, TaskIntent.SEARCH_AND_DRAFT.value}
        if intent not in {
            TaskIntent.GENERATE_DRAFT.value,
            TaskIntent.SEARCH_AND_DRAFT.value,
            TaskIntent.CURATE_NEWS.value,
            TaskIntent.SEARCH_AND_RANK.value,
        }:
            raise ValueError(f"unsupported core intent: {intent}")
        steps: list[PlanStep] = []
        article_step = "article"
        if search:
            search_tool = "crawl_news" if choice.use_crawl else "search_news"
            search_args: dict[str, ArgumentBinding] = {
                "query": _slot("news_query"),
            }
            if search_tool == "crawl_news":
                search_args.update(
                    {
                        "max_results": _server("max_results", 20),
                        "idempotency_key": _server("idempotency_key", f"{run_id}-crawl"),
                    }
                )
            else:
                search_args["limit"] = _server("limit", 10)
            steps.append(
                _step(
                    "discover",
                    "Discover bounded news candidates",
                    search_tool,
                    search_args,
                    expected=["items" if search_tool == "search_news" else "task_ref"],
                    acceptance=["candidate set is traceable and bounded"],
                    risk=StepRisk.MEDIUM if search_tool == "crawl_news" else StepRisk.LOW,
                )
            )
            article_binding = _obs(
                "article_id",
                "discover",
                "articles.0.article_id" if search_tool == "crawl_news" else "items.0.article_id",
            )
            dependencies = ["discover"]
        else:
            article_binding = _slot("selected_article_ids", path="0")
            dependencies = []
        steps.append(
            _step(
                article_step,
                "Load the selected article",
                "get_article",
                {
                    "article_id": article_binding,
                    "include_content": _server("include_content", True),
                },
                dependencies=dependencies,
                expected=["found", "article.article_id"],
                acceptance=["article exists and content reference is immutable"],
            )
        )
        steps.extend(self._assessment_and_draft_steps(article_step, draft, run_id, confirmed))
        direct = (
            "pipeline_v2.known_article_draft"
            if draft and not search and {"selected_article_ids", "product_ids"}.issubset(confirmed)
            else ""
        )
        if choice.include_export and draft:
            steps.append(self._export_after_review(run_id))
        return steps, direct

    def _assessment_and_draft_steps(
        self, article_step: str, include_draft: bool, run_id: str, confirmed: set[str]
    ) -> list[PlanStep]:
        # 用户明确指定了类别时，把 user_category 传入，使分类结果与用户要求不符时可在编排层触发"停下让用户决定"
        classify_args: dict[str, ArgumentBinding] = {
            "article": _obs("article", article_step, "article")
        }
        if "category" in confirmed:
            classify_args["user_category"] = _slot("category")
        steps = [
            _step(
                "classify",
                "Classify the immutable article",
                "classify_article",
                classify_args,
                dependencies=[article_step],
                expected=["article.article_id", "category", "model_version"],
                acceptance=["classification has model and prompt provenance"],
            ),
            _step(
                "products",
                "Match authorized products",
                "match_products",
                {"article": _obs("article", "classify", "article")},
                dependencies=["classify"],
                expected=["article.article_id", "catalog_hash", "outcome"],
                acceptance=["only catalog-authorized products are returned"],
            ),
        ]
        product_binding = (
            _slot("product_ids")
            if "product_ids" in confirmed
            else _obs("product_ids", "products", "candidates.*.product_id")
        )
        steps.append(
            _step(
                "score",
                "Score article relevance and event impact",
                "score_article",
                {
                    "article": _obs("article", "products", "article"),
                    "product_ids": product_binding,
                    "user_requested_draft": _server("user_requested_draft", include_draft),
                },
                dependencies=["products"],
                expected=["article.article_id", "total_score", "model_version"],
                acceptance=["score dimensions have evidence and frozen versions"],
            )
        )
        if not include_draft:
            return steps
        steps.extend(
            [
                _step(
                    "draft",
                    "Generate a new immutable draft artifact",
                    "generate_draft",
                    {
                        "article": _obs("article", "score", "article"),
                        "product_ids": product_binding.model_copy(update={"step_id": "products"})
                        if product_binding.source == BindingSource.OBSERVATION
                        else product_binding,
                        "template_key": _slot("template_key")
                        if "template_key" in confirmed
                        else _server("template_key", "default"),
                        "angle": _slot("angle") if "angle" in confirmed else _server("angle", ""),
                        "tone": _slot("tone")
                        if "tone" in confirmed
                        else _server("tone", "professional"),
                        "target_length": _slot("length")
                        if "length" in confirmed
                        else _server("target_length", 1200),
                        "idempotency_key": _server("idempotency_key", f"{run_id}-draft"),
                    },
                    dependencies=["score", "products"],
                    expected=["artifact.artifact_id", "artifact.content_hash", "context_hash"],
                    acceptance=["draft is non-empty, versioned and evidence-linked"],
                    risk=StepRisk.MEDIUM,
                ),
                _step(
                    "review",
                    "Review the immutable draft content hash",
                    "review_draft",
                    {"artifact": _obs("artifact", "draft", "artifact")},
                    dependencies=["draft"],
                    expected=["artifact.artifact_id", "content_hash", "passed"],
                    acceptance=["review passes and references the generated content hash"],
                ),
            ]
        )
        return steps

    def _revision_steps(self, run_id: str) -> list[PlanStep]:
        return [
            _step(
                "revise",
                "Create a new draft version from the requested revision",
                "revise_draft",
                {
                    "artifact": _slot("draft_artifact"),
                    "instruction": _slot("revision_instruction"),
                    "expected_version": _slot("draft_version"),
                    "idempotency_key": _server("idempotency_key", f"{run_id}-revise"),
                },
                expected=["source_artifact.artifact_id", "artifact.artifact_id", "review.passed"],
                acceptance=["source artifact is unchanged and new version is reviewed"],
                risk=StepRisk.MEDIUM,
            )
        ]

    def _save_step(self, run_id: str) -> PlanStep:
        return _step(
            "save",
            "Save the exact immutable draft version with optimistic locking",
            "save_draft_version",
            {
                "artifact": _slot("draft_artifact"),
                "expected_version": _slot("draft_version"),
                "kind": _server("kind", "business_version"),
                "confirmed_by_user": _slot("save_confirmed"),
                "idempotency_key": _server("idempotency_key", f"{run_id}-save"),
            },
            expected=["artifact.artifact_id", "saved", "kind"],
            acceptance=["save is idempotent and version precondition matched"],
            risk=StepRisk.HIGH,
        )

    def _export_after_review(self, run_id: str) -> PlanStep:
        return _step(
            "export",
            "Export the reviewed immutable draft",
            "export_draft",
            {
                "artifact": _obs("artifact", "review", "artifact"),
                "format": _server("format", "markdown"),
                "filename": _server("filename", "draft"),
                "idempotency_key": _server("idempotency_key", f"{run_id}-export"),
            },
            dependencies=["review"],
            expected=["artifact.artifact_id", "export_ref", "content_hash"],
            acceptance=["export content hash matches reviewed draft"],
            risk=StepRisk.MEDIUM,
        )
