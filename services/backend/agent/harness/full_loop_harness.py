"""End-to-end production acceptance harness for conversational Agent v2."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent.production_plan import (
    ArgumentBinding,
    BindingSource,
    Plan,
    ProductionPlanValidator,
)

CORE_JOURNEY_KINDS = (
    "known_article_draft",
    "search_select_draft",
    "product_clarification",
    "low_score_continue",
    "revise_version_save",
    "cancel_recover",
)

SECURITY_SCENARIOS = (
    "direct_prompt_injection",
    "indirect_article_injection",
    "tool_argument_injection",
    "arbitrary_identifier",
    "path_traversal",
    "ssrf",
    "cross_tenant_article",
    "cross_tenant_draft",
    "cross_tenant_run",
    "approval_replay",
    "trace_secret_leak",
    "resource_exhaustion",
)

RECOVERY_SCENARIOS = (
    "timeout",
    "rate_limit_429",
    "server_5xx",
    "connection_drop",
    "invalid_schema",
    "mongo_unavailable",
    "redis_unavailable",
    "arq_unavailable",
    "worker_kill",
    "lease_expiry",
    "fencing_conflict",
    "duplicate_event",
    "out_of_order_event",
    "checkpoint_write_failure",
    "log_write_failure",
    "artifact_write_failure",
    "cancel_completion_race",
)

CAPACITY_SCENARIOS = (
    "single_article_no_clarification",
    "multi_turn_clarification",
    "batch_crawl_top_n",
    "long_context",
    "retry_storm",
    "50_concurrent_users",
    "slow_provider",
    "provider_rate_limit",
)

FORBIDDEN_METRIC_TAGS = frozenset(
    {"user_id", "tenant_id", "content", "body", "prompt", "raw_text", "email"}
)


@dataclass(frozen=True)
class JourneyCase:
    case_id: str
    kind: str
    user_behavior: str
    expected_terminal: str
    requires_clarification: bool
    forbidden_write_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class JourneyExecution:
    case_id: str
    terminal_status: str
    used_tools: tuple[str, ...] = ()
    actual_clarification: bool = False
    reason_code: str = ""


@dataclass(frozen=True)
class JourneyHarnessReport:
    total: int
    passed: int
    failed: int
    success_rate: float
    reproductions: tuple[dict[str, Any], ...] = ()


class JourneyHarnessRunner:
    """Executes every journey and emits a privacy-safe minimal reproduction on failure."""

    def __init__(
        self,
        execute: Callable[[JourneyCase], Awaitable[JourneyExecution]],
    ) -> None:
        self.execute = execute

    async def run(self, cases: list[JourneyCase]) -> JourneyHarnessReport:
        reproductions: list[dict[str, Any]] = []
        passed = 0
        for case in cases:
            result = await self.execute(case)
            forbidden = sorted(set(result.used_tools) & set(case.forbidden_write_tools))
            reasons: list[str] = []
            if result.terminal_status != case.expected_terminal:
                reasons.append("terminal_status_mismatch")
            if forbidden:
                reasons.append("forbidden_write_tool")
            if case.requires_clarification and not result.actual_clarification:
                reasons.append("required_clarification_missing")
            if reasons:
                reproductions.append(
                    {
                        "case_id": case.case_id,
                        "kind": case.kind,
                        "behavior": case.user_behavior,
                        "expected_terminal": case.expected_terminal,
                        "actual_terminal": result.terminal_status,
                        "reason_codes": reasons,
                        "runtime_reason_code": result.reason_code,
                        "forbidden_tools_used": forbidden,
                    }
                )
            else:
                passed += 1
        total = len(cases)
        return JourneyHarnessReport(
            total=total,
            passed=passed,
            failed=total - passed,
            success_rate=passed / total if total else 0.0,
            reproductions=tuple(reproductions),
        )


def build_core_60_journeys() -> list[JourneyCase]:
    behaviors = (
        "cooperative",
        "ambiguous",
        "natural_language_reply",
        "structured_selection",
        "reject_suggestion",
        "change_requirement",
        "timeout_then_resume",
        "duplicate_reply",
        "refresh_mid_run",
        "cancel_then_recover",
    )
    cases: list[JourneyCase] = []
    for kind in CORE_JOURNEY_KINDS:
        for index, behavior in enumerate(behaviors, start=1):
            needs_clarification = kind in {"search_select_draft", "product_clarification"}
            expected = "canceled" if behavior == "cancel_then_recover" else "completed"
            cases.append(
                JourneyCase(
                    case_id=f"{kind}-{index:02d}",
                    kind=kind,
                    user_behavior=behavior,
                    expected_terminal=expected,
                    requires_clarification=needs_clarification,
                    forbidden_write_tools=("save_draft",) if kind != "revise_version_save" else (),
                )
            )
    return cases


@dataclass(frozen=True)
class ConversationSample:
    expected_intent: str
    predicted_intent: str
    expected_slots: frozenset[str] = frozenset()
    predicted_slots: frozenset[str] = frozenset()
    clarification_required: bool = False
    clarification_asked: bool = False
    repeated_clarification: bool = False
    incorrect_execution: bool = False


def conversation_metrics(samples: list[ConversationSample]) -> dict[str, float]:
    if not samples:
        raise ValueError("conversation harness requires samples")
    intent_ok = sum(item.expected_intent == item.predicted_intent for item in samples)
    true_positive = sum(len(item.expected_slots & item.predicted_slots) for item in samples)
    false_positive = sum(len(item.predicted_slots - item.expected_slots) for item in samples)
    false_negative = sum(len(item.expected_slots - item.predicted_slots) for item in samples)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    slot_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    required = [item for item in samples if item.clarification_required]
    unnecessary = [item for item in samples if not item.clarification_required]
    return {
        "intent_accuracy": round(intent_ok / len(samples), 4),
        "slot_f1": round(slot_f1, 4),
        "clarification_recall": round(
            sum(item.clarification_asked for item in required) / len(required), 4
        ) if required else 1.0,
        "over_clarification_rate": round(
            sum(item.clarification_asked for item in unnecessary) / len(unnecessary), 4
        ) if unnecessary else 0.0,
        "repeated_clarification_rate": round(
            sum(item.repeated_clarification for item in samples) / len(samples), 4
        ),
        "incorrect_execution_rate": round(
            sum(item.incorrect_execution for item in samples) / len(samples), 4
        ),
    }


class PlannerMutationHarness:
    """Mutates a valid Plan and proves every invalid variant is rejected."""

    def __init__(self, validator: ProductionPlanValidator):
        self.validator = validator

    def variants(self, plan: Plan) -> dict[str, Plan]:
        first = plan.steps[0]
        last = plan.steps[-1]
        unknown = plan.model_copy(
            deep=True,
            update={"steps": [first.model_copy(update={"tool": "unknown_tool"}), *plan.steps[1:]]},
        )
        missing_dependency = plan.model_copy(
            deep=True,
            update={
                "steps": [
                    *plan.steps[:-1],
                    last.model_copy(update={"dependencies": [*last.dependencies, "missing_step"]}),
                ]
            },
        )
        cycle_steps = list(plan.steps)
        cycle_steps[0] = first.model_copy(update={"dependencies": [last.step_id]})
        cycle = plan.model_copy(deep=True, update={"steps": cycle_steps})
        sensitive_steps = list(plan.steps)
        sensitive_args = dict(first.args_binding)
        argument_name = next(iter(sensitive_args))
        sensitive_args[argument_name] = ArgumentBinding(
            source=BindingSource.SERVER_VALUE,
            key="authorization",
            value={"token": "must-not-enter-plan"},
        )
        sensitive_steps[0] = first.model_copy(update={"args_binding": sensitive_args})
        sensitive = plan.model_copy(deep=True, update={"steps": sensitive_steps})
        return {
            "unknown_tool": unknown,
            "missing_dependency": missing_dependency,
            "dependency_cycle": cycle,
            "sensitive_identity": sensitive,
        }

    def run(self, plan: Plan, *, confirmed_slots: set[str]) -> dict[str, Any]:
        outcomes: dict[str, str] = {}
        for name, mutated in self.variants(plan).items():
            validation = self.validator.validate(mutated, confirmed_slots=confirmed_slots)
            outcomes[name] = validation.reason_code if not validation.accepted else "accepted"
        blocked = sum(value != "accepted" for value in outcomes.values())
        return {
            "total": len(outcomes),
            "blocked": blocked,
            "blocked_rate": blocked / len(outcomes),
            "outcomes": outcomes,
        }


def compare_domain_metrics(
    baseline: dict[str, float], candidate: dict[str, float]
) -> dict[str, dict[str, float]]:
    keys = sorted(set(baseline) | set(candidate))
    return {
        key: {
            "legacy": float(baseline.get(key, 0.0)),
            "candidate": float(candidate.get(key, 0.0)),
            "delta": round(float(candidate.get(key, 0.0)) - float(baseline.get(key, 0.0)), 6),
        }
        for key in keys
    }


def validate_metric_tags(definitions: list[dict[str, Any]]) -> list[str]:
    violations: list[str] = []
    for definition in definitions:
        for tag in definition.get("tags", ()):
            if str(tag).lower() in FORBIDDEN_METRIC_TAGS:
                violations.append(f"{definition.get('name', 'unknown')}:{tag}")
    return sorted(violations)


@dataclass(frozen=True)
class FullLoopAcceptanceReport:
    journey_count: int
    journey_success_rate: float
    planner_mutation_block_rate: float
    security_failures: int
    recovery_failures: int
    observability_violations: tuple[str, ...] = ()
    domain_metrics: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (
            self.journey_count == 60
            and self.journey_success_rate == 1.0
            and self.planner_mutation_block_rate == 1.0
            and self.security_failures == 0
            and self.recovery_failures == 0
            and not self.observability_violations
        )
