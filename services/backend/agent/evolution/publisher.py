"""候选策略发布状态机。

状态流转：
draft -> evaluating -> gate_failed | ready_for_review -> approved -> shadow -> canary -> active -> retired | rolled_back

未审批候选不能进入 Active。
不自动发布。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("backend.agent.evolution.publisher")


# 合法状态转换
TRANSITIONS = {
    "draft": {"evaluating"},
    "evaluating": {"gate_failed", "ready_for_review"},
    "gate_failed": {"draft"},
    "ready_for_review": {"approved", "draft"},
    "approved": {"shadow", "rolled_back"},
    "shadow": {"canary", "rolled_back"},
    "canary": {"active", "rolled_back"},
    "active": {"retired", "rolled_back"},
    "retired": set(),
    "rolled_back": set(),
}

# 需要审批才能进入的状态
APPROVAL_REQUIRED = {"approved", "shadow", "canary", "active"}


class Publisher:
    """候选策略发布状态机。"""

    def __init__(self, db: Any):
        self.db = db

    async def transition(
        self,
        candidate_id: str,
        target_status: str,
        approved_by: str | None = None,
    ) -> dict:
        """执行状态转换。

        Args:
            candidate_id: 候选 ID
            target_status: 目标状态
            approved_by: 审批人（需要审批的状态必须提供）

        Returns:
            更新后的候选文档
        """
        candidate = await self.db["personalization_candidates"].find_one(
            {
                "candidate_id": candidate_id,
            }
        )
        if candidate is None:
            raise ValueError(f"Candidate not found: {candidate_id}")

        current = candidate.get("status", "draft")

        # 检查合法转换
        if target_status not in TRANSITIONS.get(current, set()):
            raise ValueError(f"Invalid transition: {current} -> {target_status}")

        # 检查审批
        if target_status in APPROVAL_REQUIRED and not approved_by:
            raise ValueError(f"Approval required for transition to {target_status}")

        update: dict[str, Any] = {
            "status": target_status,
            "updated_at": datetime.now(UTC),
        }
        if approved_by:
            update["approved_by"] = approved_by

        await self.db["personalization_candidates"].update_one(
            {"candidate_id": candidate_id},
            {"$set": update},
        )

        logger.info(
            "candidate transitioned: id=%s %s -> %s approved_by=%s",
            candidate_id,
            current,
            target_status,
            approved_by or "N/A",
        )

        candidate.update(update)
        candidate.pop("_id", None)
        return candidate

    async def rollback(self, candidate_id: str, approved_by: str) -> dict:
        """回滚候选到上一版本。"""
        return await self.transition(candidate_id, "rolled_back", approved_by)

    async def get_active(self, target_type: str) -> dict | None:
        """获取当前 Active 的候选。"""
        return await self.db["personalization_candidates"].find_one(
            {
                "target_type": target_type,
                "status": "active",
            }
        )
