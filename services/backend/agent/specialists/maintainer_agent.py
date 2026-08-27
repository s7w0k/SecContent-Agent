"""MaintainerAgent - 受控知识自进化的 Specialist Agent（计划 §40 / §41 / §42 / §44）。

安全不变量（Hard Gate §44）：
  - user statement != trusted source：untrusted 事件绝不能成为 Production Wiki Fact。
  - Maintainer 只写 Staging / 下发提案；Publish 是唯一 Production 出口，
    且必须通过 Source Verification → Staging → Eval/Regression → Approval 全部门禁。
  - 普通用户 scope 不可触发 maintain 工具（scope=wiki:maintain, risk=HIGH）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

MaintenanceStatus = Literal[
    "OPEN",
    "NEEDS_SOURCE",
    "STAGED",
    "EVALUATING",
    "WAITING_APPROVAL",
    "PUBLISHED",
    "REJECTED",
]


class MaintenanceCase(BaseModel):
    """计划 §41：一次知识维护的端到端状态。"""

    case_id: str
    event_refs: list[str] = Field(default_factory=list)
    subject: str = ""
    source_refs: list[str] = Field(default_factory=list)
    trusted: bool = False
    proposed_actions: list[str] = Field(default_factory=list)
    status: MaintenanceStatus = "OPEN"
    reason: str = ""


SourceVerifier = Callable[[Any], list[str]]  # 事件 -> 已核实 source_refs
Evaluator = Callable[[MaintenanceCase], dict]  # 返回 {"ok": bool, "regressions": [...]}
Approval = Callable[[MaintenanceCase], bool]
Publisher = Callable[[MaintenanceCase], dict]


class MaintainerAgent:
    def __init__(
        self,
        *,
        journal: Any | None = None,
        source_verifier: SourceVerifier | None = None,
        evaluator: Evaluator | None = None,
        approval: Approval | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self.journal = journal  # ImprovementJournal（可选，去重）
        self.source_verifier = source_verifier or (
            lambda event: (
                [str(event.source_hint or "")] if getattr(event, "source_hint", "") else []
            )
        )
        self.evaluator = evaluator or (lambda case: {"ok": True, "regressions": []})
        self.approval = approval or (lambda case: True)
        self.publisher = publisher or (lambda case: {"published": False})
        # 审计：Production 唯一出口计数。
        self.production_write_attempts = 0
        self.published_cases: list[str] = []

    # ── 入口 ──────────────────────────────────────────────

    async def process(self, event: Any) -> MaintenanceCase:
        case = self._open_case(event)
        if self._is_duplicate(event):
            return self._settle(case, "REJECTED", "duplicate_event")

        # Hard Gate §44：untrusted（用户输入）不得进入事实链。
        if not getattr(event, "trusted", False):
            return self._settle(case, "NEEDS_SOURCE", "untrusted_user_statement")

        # Source Verification
        source_refs = self.source_verifier(event)
        if not source_refs:
            return self._settle(case, "NEEDS_SOURCE", "missing_trusted_source")
        case.source_refs = source_refs

        # 仅写 Staging（不触碰 Production）。
        case.status = "STAGED"
        self._apply_staging(case)

        # Eval / Golden Regression Gate
        case.status = "EVALUATING"
        evaluation = self.evaluator(case)
        if not evaluation.get("ok", False):
            regressions = evaluation.get("regressions", [])
            return self._settle(
                case, "REJECTED", f"regression: {','.join(regressions) or 'failed'}"
            )

        # Approval Gate
        if not self.approval(case):
            return self._settle(case, "WAITING_APPROVAL", "awaiting_approval")

        # Publish 是唯一 Production 出口（全部门禁通过才执行）。
        self.production_write_attempts += 1
        publish_result = self._publish(case)
        if publish_result.get("published", False):
            return self._settle(case, "PUBLISHED", publish_result.get("ref", ""))
        return self._settle(case, "REJECTED", "publish_rejected")

    # ── 内部 ──────────────────────────────────────────────

    def _open_case(self, event: Any) -> MaintenanceCase:
        event_id = str(getattr(event, "event_id", "") or uuid.uuid4().hex[:12])
        subject = str(getattr(event, "subject", "") or "")
        trusted = bool(getattr(event, "trusted", False))
        actions = (
            ["propose_wiki_fact", "compile_staging"]
            if trusted
            else ["source_discovery", "proposal_review"]
        )
        return MaintenanceCase(
            case_id=event_id,
            event_refs=[event_id],
            subject=subject,
            trusted=trusted,
            proposed_actions=actions,
            status="OPEN",
        )

    def _is_duplicate(self, event: Any) -> bool:
        if self.journal is None:
            return False
        recorded = self.journal.record(event)
        return not recorded

    def _apply_staging(self, case: MaintenanceCase) -> None:
        """把提案编译进 Staging（只读/临时区，未发布前不可见）。"""
        case.proposed_actions = [a for a in case.proposed_actions if a != "propose_wiki_fact"]

    def _settle(
        self, case: MaintenanceCase, status: MaintenanceStatus, reason: str
    ) -> MaintenanceCase:
        case.status = status
        case.reason = reason
        if status == "PUBLISHED" and reason:
            self.published_cases.append(reason)
        elif status == "PUBLISHED":
            self.published_cases.append(case.case_id)
        return case

    def _publish(self, case: MaintenanceCase) -> dict:
        result = self.publisher(case)
        return result if isinstance(result, dict) else {"published": bool(result)}

    def _demote(
        self, case: MaintenanceCase, status: MaintenanceStatus, reason: str
    ) -> MaintenanceCase:
        """供验证 staging-only 的分支使用。"""
        return self._settle(case, status, reason)


__all__ = [
    "MaintainerAgent",
    "MaintenanceCase",
    "MaintenanceStatus",
]
