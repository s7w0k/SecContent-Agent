"""Production migration routing for sandbox, shadow and percentage cohorts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MigrationStage(StrEnum):
    LEGACY = "legacy"
    SANDBOX = "sandbox"
    SHADOW = "shadow"
    INTERNAL = "internal"
    PERCENT_1 = "1%"
    PERCENT_10 = "10%"
    PERCENT_50 = "50%"
    FULL = "100%"


_PERCENT = {
    MigrationStage.LEGACY: 0,
    MigrationStage.SANDBOX: 0,
    MigrationStage.SHADOW: 0,
    MigrationStage.INTERNAL: 0,
    MigrationStage.PERCENT_1: 1,
    MigrationStage.PERCENT_10: 10,
    MigrationStage.PERCENT_50: 50,
    MigrationStage.FULL: 100,
}


def stable_user_bucket(*, tenant_id: str, user_id: str, salt: str = "agent-v2") -> int:
    raw = f"{salt}:{tenant_id}:{user_id}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:12], 16) % 100


@dataclass(frozen=True)
class RouteDecision:
    path: str
    stage: MigrationStage
    cohort: str
    bucket: int
    adapter: str
    write_tools_allowed: bool
    legacy_fallback_available: bool = True


@dataclass(frozen=True)
class PromotionGate:
    g0_passed: bool
    g1_passed: bool
    e2e_success_rate: float
    minimum_e2e_success_rate: float
    security_failures: int
    duplicate_writes: int
    unauthorized_actions: int
    zombie_runs: int
    rollback_drill_passed: bool

    @property
    def passed(self) -> bool:
        return (
            self.g0_passed
            and self.g1_passed
            and self.e2e_success_rate >= self.minimum_e2e_success_rate
            and self.security_failures == 0
            and self.duplicate_writes == 0
            and self.unauthorized_actions == 0
            and self.zombie_runs == 0
            and self.rollback_drill_passed
        )


@dataclass
class MigrationController:
    stage: MigrationStage = MigrationStage.LEGACY
    internal_users: set[str] = field(default_factory=set)
    salt: str = "agent-v2"
    legacy_enabled: bool = True
    _frozen_runs: dict[str, dict[str, str]] = field(default_factory=dict, init=False)

    def route(self, *, tenant_id: str, user_id: str) -> RouteDecision:
        bucket = stable_user_bucket(tenant_id=tenant_id, user_id=user_id, salt=self.salt)
        selected = False
        adapter = "production"
        writes = True
        cohort = self.stage.value
        if self.stage == MigrationStage.SANDBOX:
            selected, adapter = True, "sandbox"
        elif self.stage == MigrationStage.SHADOW:
            selected, adapter, writes = True, "production_readonly", False
        elif self.stage == MigrationStage.INTERNAL:
            selected = user_id in self.internal_users
            cohort = "internal" if selected else "legacy"
        elif self.stage in {
            MigrationStage.PERCENT_1,
            MigrationStage.PERCENT_10,
            MigrationStage.PERCENT_50,
            MigrationStage.FULL,
        }:
            selected = bucket < _PERCENT[self.stage]
            cohort = self.stage.value if selected else "legacy"
        if self.stage == MigrationStage.LEGACY:
            selected = False
            cohort = "legacy"
        return RouteDecision(
            path="agent" if selected else "legacy",
            stage=self.stage,
            cohort=cohort,
            bucket=bucket,
            adapter=adapter if selected else "legacy",
            write_tools_allowed=writes if selected else True,
            legacy_fallback_available=self.legacy_enabled,
        )

    def promote(self, target: MigrationStage, gate: PromotionGate) -> None:
        if not gate.passed:
            raise ValueError("rollout promotion blocked by hard gate")
        self.stage = target

    def rollback(self) -> None:
        self.stage = MigrationStage.LEGACY

    def freeze_run(self, run_id: str, versions: dict[str, str]) -> dict[str, str]:
        frozen = self._frozen_runs.setdefault(run_id, dict(versions))
        return dict(frozen)

    def frozen_versions(self, run_id: str) -> dict[str, str]:
        return dict(self._frozen_runs[run_id])

    @staticmethod
    def enforce_shadow_write_policy(decision: RouteDecision, *, side_effect_level: str) -> None:
        if not decision.write_tools_allowed and side_effect_level not in {"L0", "L1"}:
            raise PermissionError("shadow route blocks business write tools")

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "legacy_enabled": self.legacy_enabled,
            "internal_user_count": len(self.internal_users),
            "frozen_run_count": len(self._frozen_runs),
        }
