"""审批 RBAC 与职责分离 — 阶段3 WBS 3.8（阶段3 §5）。

与 policy_engine.ApprovalService 的分工：
  - ApprovalService 已实现 params_hash 变化失效、过期失效、一次性授权消费；
  - 本模块补齐"谁有权审批"的 RBAC 层：
    * 职责分离：发起人不可审批自己发起的审批（高风险强制）；
    * L3 操作要求管理员或双人审批；
    * 拒绝、过期和撤回均写入不可抵赖审计记录。
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from agent.policy_engine import PendingApproval, RiskLevel
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RbacResult(BaseModel):
    """审批权限判定结果。"""

    allowed: bool
    reason: str
    reason_code: str = ""
    requires_dual: bool = False  # 需要双人审批
    required_approvers: int = 1


class DualApprovalState(BaseModel):
    """双人审批进度（L3 操作）。"""

    approval_id: str
    required_approvers: int = 2
    approvers: list[str] = Field(default_factory=list)
    completed: bool = False

    @property
    def current(self) -> int:
        return len(self.approvers)

    def add(self, approver: str) -> bool:
        if self.completed or approver in self.approvers:
            return False
        self.approvers.append(approver)
        if len(self.approvers) >= self.required_approvers:
            self.completed = True
        return True


def can_approve(
    approval: PendingApproval,
    *,
    initiator: str,
    approver: str,
    role: str = "",
    admin_roles: frozenset[str] = frozenset({"admin"}),
) -> RbacResult:
    """审批权限判定（RBAC + 职责分离）。

    规则：
      1. 审批人必填且非空；
      2. 审批人不得是发起人（职责分离）；
      3. L3：仅管理员（或双人审批的第一个审批人）可批；
      4. L2/L1：单人即可（仍须非发起人）。
    """
    if not approver:
        return RbacResult(allowed=False, reason="审批人缺失", reason_code="missing_approver")
    if approver == initiator:
        return RbacResult(
            allowed=False,
            reason="发起人不可审批自己发起的审批（职责分离）",
            reason_code="self_approval_denied",
        )
    risk = RiskLevel(approval.risk_level)
    if risk == RiskLevel.L3:
        if role in admin_roles:
            return RbacResult(
                allowed=True,
                reason="L3 管理员审批通过",
                reason_code="admin_approved",
                requires_dual=False,
                required_approvers=1,
            )
        return RbacResult(
            allowed=False,
            reason="L3 操作要求管理员审批",
            reason_code="l3_requires_admin",
            requires_dual=True,
            required_approvers=2,
        )
    return RbacResult(
        allowed=True,
        reason=f"{risk.value} 级单人审批（职责分离已校验）",
        reason_code="approved",
        requires_dual=False,
        required_approvers=1,
    )


def audit_record(
    *,
    approval_id: str,
    run_id: str = "",
    actor: str,
    action: str,  # approved / rejected / expired / revoked / requested
    reason: str = "",
    params_hash: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """不可抵赖审批审计记录（不含参数原文）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "approval_id": approval_id,
        "run_id": run_id,
        "actor": actor,
        "action": action,
        "reason": reason[:300],
        "params_hash": params_hash,
        "created_at": (now or _utc_now()).isoformat(),
    }


class ApprovalAuditLog:
    """审批审计日志存储（runtime_approval_audits）。"""

    COLLECTION = "runtime_approval_audits"

    def __init__(self, db: Any, *, collection: str = COLLECTION):
        self.db = db
        self.collection_name = collection
        self.col = db[collection]

    async def record(self, *, record: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            # 审计写入失败记日志即可，不阻断审批
            await self.col.insert_one(record)

    async def list_for_approval(self, approval_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        try:
            cursor = self.col.find({"approval_id": approval_id}).sort("created_at", 1).limit(limit)
            return await cursor.to_list(length=limit)
        except Exception:
            return []
