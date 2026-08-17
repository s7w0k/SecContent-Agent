"""Structured production Plan v2 and deterministic pre-execution validation."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from agent.business_tools.contracts import BusinessToolRegistry
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLAN_SCHEMA_VERSION = "2.0"
SENSITIVE_PLAN_KEYS = frozenset(
    {"user_id", "tenant_id", "api_key", "token", "password", "secret", "credential", "authorization", "cookie"}
)


class BindingSource(StrEnum):
    CONFIRMED_SLOT = "confirmed_slot"
    OBSERVATION = "observation"
    SERVER_VALUE = "server_value"


class StepRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_APPROVAL = "waiting_approval"


class ArgumentBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: BindingSource
    key: str = Field(..., min_length=1, max_length=160)
    step_id: str = Field(default="", max_length=100)
    path: str = Field(default="", max_length=300)
    value: Any = None

    @model_validator(mode="after")
    def validate_source(self) -> ArgumentBinding:
        if self.source == BindingSource.OBSERVATION and not self.step_id:
            raise ValueError("observation binding requires step_id")
        if self.source != BindingSource.OBSERVATION and self.step_id:
            raise ValueError("only observation binding may specify step_id")
        if self.source != BindingSource.SERVER_VALUE and self.value is not None:
            raise ValueError("only server_value binding may carry a literal")
        return self


class PlanRetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=10)
    retry_on: tuple[str, ...] = ("timeout", "rate_limit", "upstream_5xx")
    backoff_seconds: float = Field(default=0, ge=0, le=60)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(..., pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    goal: str = Field(..., min_length=1, max_length=500)
    tool: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,63}$")
    args_binding: dict[str, ArgumentBinding] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    preconditions: list[str] = Field(default_factory=list, max_length=20)
    expected_observation: list[str] = Field(..., min_length=1, max_length=30)
    acceptance: list[str] = Field(..., min_length=1, max_length=30)
    risk: StepRisk = StepRisk.LOW
    status: StepStatus = StepStatus.PENDING
    retry_policy: PlanRetryPolicy = Field(default_factory=PlanRetryPolicy)
    timeout_seconds: int = Field(default=60, ge=1, le=900)
    estimated_cost_units: int = Field(default=1, ge=0, le=1000)

    @model_validator(mode="after")
    def validate_self_reference(self) -> PlanStep:
        if self.step_id in self.dependencies:
            raise ValueError("step cannot depend on itself")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = PLAN_SCHEMA_VERSION
    plan_id: str = Field(..., min_length=1, max_length=100)
    run_id: str = Field(..., min_length=1, max_length=100)
    intent: str = Field(..., min_length=1, max_length=100)
    planner_version: str = Field(..., min_length=1, max_length=100)
    task_snapshot_hash: str = Field(..., min_length=1, max_length=100)
    steps: list[PlanStep] = Field(..., min_length=1, max_length=50)
    direct_pipeline: str = Field(default="", max_length=100)
    estimated_cost_units: int = Field(default=0, ge=0, le=100_000)

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"plan_id", "run_id"})
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ProductionPlanValidation(BaseModel):
    accepted: bool
    reason_code: str = ""
    reason: str = ""
    plan_hash: str = ""
    estimated_cost_units: int = 0


def _contains_sensitive(value: Any, *, key: str = "") -> bool:
    if key.lower() in SENSITIVE_PLAN_KEYS:
        return True
    if isinstance(value, dict):
        return any(_contains_sensitive(v, key=str(k)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive(item) for item in value)
    return False


class ProductionPlanValidator:
    def __init__(
        self,
        registry: BusinessToolRegistry,
        *,
        max_steps: int = 50,
        max_cost_units: int = 5000,
        max_total_timeout_seconds: int = 7200,
    ):
        self.registry = registry
        self.max_steps = max_steps
        self.max_cost_units = max_cost_units
        self.max_total_timeout_seconds = max_total_timeout_seconds

    def validate(
        self,
        plan: Plan,
        *,
        confirmed_slots: set[str] | frozenset[str],
        expected_run_id: str = "",
        expected_task_snapshot_hash: str = "",
    ) -> ProductionPlanValidation:
        def reject(code: str, reason: str) -> ProductionPlanValidation:
            return ProductionPlanValidation(
                accepted=False,
                reason_code=code,
                reason=reason,
                plan_hash=plan.fingerprint,
            )
        if plan.schema_version != PLAN_SCHEMA_VERSION:
            return reject("invalid_plan_schema", "unsupported plan schema")
        if expected_run_id and plan.run_id != expected_run_id:
            return reject("run_id_mismatch", "plan run_id is not server-owned run_id")
        if expected_task_snapshot_hash and plan.task_snapshot_hash != expected_task_snapshot_hash:
            return reject("task_snapshot_mismatch", "task snapshot changed")
        if len(plan.steps) > self.max_steps:
            return reject("plan_budget_exceeded", "too many plan steps")

        ids = [step.step_id for step in plan.steps]
        if len(ids) != len(set(ids)):
            return reject("duplicate_step_id", "step ids must be unique")
        by_id = {step.step_id: step for step in plan.steps}
        order = {step.step_id: index for index, step in enumerate(plan.steps)}
        total_cost = 0
        total_timeout = 0
        for step in plan.steps:
            try:
                contract = self.registry.get(step.tool)
            except KeyError:
                return reject("unknown_tool", f"unknown business tool: {step.tool}")
            if set(step.args_binding) - set(contract.args_schema.model_fields):
                return reject("unknown_tool_argument", f"invalid arguments for {step.tool}")
            required_args = {
                name
                for name, field in contract.args_schema.model_fields.items()
                if field.is_required()
            }
            missing_args = required_args - set(step.args_binding)
            if missing_args:
                return reject(
                    "missing_tool_argument",
                    f"missing required arguments for {step.tool}: {sorted(missing_args)}",
                )
            for dep in step.dependencies:
                if dep not in by_id:
                    return reject("missing_dependency", f"missing dependency: {dep}")
            for arg, binding in step.args_binding.items():
                if arg.lower() in SENSITIVE_PLAN_KEYS or _contains_sensitive(binding.value, key=arg):
                    return reject("sensitive_plan_data", "plan cannot carry identity or credentials")
                if binding.source == BindingSource.CONFIRMED_SLOT and binding.key not in confirmed_slots:
                    return reject("unconfirmed_slot_binding", f"slot is not confirmed: {binding.key}")
                if binding.source == BindingSource.OBSERVATION:
                    if binding.step_id not in by_id or order[binding.step_id] >= order[step.step_id]:
                        return reject("invalid_observation_binding", "observation must come from an earlier step")
                    if binding.step_id not in step.dependencies:
                        return reject("undeclared_observation_dependency", "observation step must be a dependency")
            total_cost += step.estimated_cost_units
            total_timeout += step.timeout_seconds

        visiting: set[str] = set()
        visited: set[str] = set()

        def cyclic(step_id: str) -> bool:
            if step_id in visiting:
                return True
            if step_id in visited:
                return False
            visiting.add(step_id)
            if any(cyclic(dep) for dep in by_id[step_id].dependencies):
                return True
            visiting.remove(step_id)
            visited.add(step_id)
            return False

        if any(cyclic(step_id) for step_id in ids):
            return reject("plan_cycle", "plan dependencies contain a cycle")
        if total_cost > self.max_cost_units or total_timeout > self.max_total_timeout_seconds:
            return reject("plan_budget_exceeded", "plan cost or timeout exceeds budget")
        return ProductionPlanValidation(
            accepted=True,
            reason_code="plan_valid",
            reason="plan accepted",
            plan_hash=plan.fingerprint,
            estimated_cost_units=total_cost,
        )
