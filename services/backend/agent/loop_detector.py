"""Loop 终止检测器 — 阶段1 1.3 节（WBS 1.1 子项）。

同时检测六类循环 / 无进展信号：
  1. exact_repeat      完全相同工具与参数重复；
  2. same_result       参数轻微变化但结果 hash 不变；
  3. no_new_evidence   连续 N 步没有新增证据；
  4. same_error        同一错误类别持续出现；
  5. plan_oscillation  计划在多个状态间振荡；
  6. stalled_coverage  Token 消耗增加但验收条件覆盖率不变。

终止策略（阶段1 1.3 节）：
  - 第一次命中 -> 受控 replan（should_replan 返回 True）；
  - 再次命中 -> 停止并返回可解释部分结果（should_stop 返回 True）。

设计约束：无随机因素、无外部状态，全部基于传入的观察记录，确定且可测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger("backend.agent.loop_detector")


class LoopSignal(StrEnum):
    """循环 / 无进展信号类型。"""

    EXACT_REPEAT = "exact_repeat"
    SAME_RESULT = "same_result"
    NO_NEW_EVIDENCE = "no_new_evidence"
    SAME_ERROR = "same_error"
    PLAN_OSCILLATION = "plan_oscillation"
    STALLED_COVERAGE = "stalled_coverage"


@dataclass
class Detection:
    """一次检测命中记录。"""

    signal: LoopSignal
    reason: str
    hit_count: int  # 该信号第几次命中（1=首次 -> replan；>=2 -> stop）
    detail: dict = field(default_factory=dict)

    @property
    def should_replan(self) -> bool:
        return self.hit_count == 1

    @property
    def should_stop(self) -> bool:
        return self.hit_count >= 2


@dataclass(frozen=True)
class ActionRecord:
    """一次动作观察（工具名 + 参数 hash + 结果 hash + 证据数 + 错误码）。"""

    tool_name: str
    args_hash: str = ""
    result_hash: str = ""
    new_evidence_count: int = 0
    error_code: str = ""
    plan_state: str = ""
    coverage: float = 0.0  # 验收条件覆盖率 0~1


class LoopDetector:
    """无进展 / 循环检测器（进程内，单 run 实例）。"""

    def __init__(
        self,
        *,
        max_no_progress_steps: int = 3,
        same_result_window: int = 3,
        same_error_window: int = 3,
        plan_oscillation_window: int = 4,
        stalled_coverage_window: int = 3,
    ) -> None:
        self.max_no_progress_steps = max(1, max_no_progress_steps)
        self.same_result_window = max(2, same_result_window)
        self.same_error_window = max(2, same_error_window)
        self.plan_oscillation_window = max(2, plan_oscillation_window)
        self.stalled_coverage_window = max(2, stalled_coverage_window)

        self.actions: list[ActionRecord] = []
        self.plan_states: list[str] = []
        self.evidence_counts: list[int] = []
        self.coverage_history: list[float] = []
        self._hits: dict[LoopSignal, int] = {}

    # ── 观察入口 ──────────────────────────────────────────

    def observe_action(
        self,
        *,
        tool_name: str,
        args_hash: str = "",
        result_hash: str = "",
        new_evidence_count: int = 0,
        error_code: str = "",
        plan_state: str = "",
        coverage: float = 0.0,
    ) -> Detection | None:
        """记录一次动作并检测是否命中循环信号。

        Returns:
            命中的 Detection（按优先级取第一个）；未命中返回 None。
        """
        record = ActionRecord(
            tool_name=tool_name,
            args_hash=args_hash,
            result_hash=result_hash,
            new_evidence_count=max(0, int(new_evidence_count)),
            error_code=error_code,
            plan_state=plan_state,
            coverage=max(0.0, min(1.0, float(coverage))),
        )
        self.actions.append(record)
        if plan_state:
            self.plan_states.append(plan_state)
        self.evidence_counts.append(record.new_evidence_count)
        if coverage > 0:
            self.coverage_history.append(record.coverage)

        for signal, reason, detail in self._checks(record):
            hit = self._register_hit(signal)
            detection = Detection(
                signal=signal,
                reason=reason,
                hit_count=hit,
                detail=detail,
            )
            logger.info(
                "[loop-detector] hit signal=%s hit_count=%d reason=%s",
                signal.value,
                hit,
                reason,
            )
            return detection
        return None

    def reset(self) -> None:
        """replan 后重置动作历史（plan 状态保留以检测持续振荡）。"""
        self.actions.clear()
        self.evidence_counts.clear()

    # ── 内部检测 ──────────────────────────────────────────

    def _register_hit(self, signal: LoopSignal) -> int:
        current = self._hits.get(signal, 0) + 1
        self._hits[signal] = current
        return current

    def _checks(self, record: ActionRecord) -> list[tuple[LoopSignal, str, dict]]:
        """按优先级顺序检测六类信号（确定性）。"""
        hits: list[tuple[LoopSignal, str, dict]] = []

        # 1. 完全相同工具与参数重复
        repeat_count = sum(
            1
            for a in self.actions
            if a.tool_name == record.tool_name
            and a.args_hash == record.args_hash
            and a.args_hash != ""
        )
        if repeat_count >= 2:
            hits.append(
                (
                    LoopSignal.EXACT_REPEAT,
                    f"tool={record.tool_name} 相同参数已出现 {repeat_count} 次",
                    {"repeat_count": repeat_count, "tool_name": record.tool_name},
                )
            )

        # 2. 参数轻微变化但结果 hash 不变
        if record.result_hash:
            same_results = [
                a for a in self.actions if a.result_hash == record.result_hash
            ]
            if len(same_results) >= self.same_result_window:
                args_hashes = {a.args_hash for a in same_results}
                hits.append(
                    (
                        LoopSignal.SAME_RESULT,
                        f"结果 hash 相同已出现 {len(same_results)} 次（参数变体 {len(args_hashes)} 个）",
                        {
                            "count": len(same_results),
                            "distinct_args": len(args_hashes),
                            "result_hash": record.result_hash,
                        },
                    )
                )

        # 3. 连续 N 步没有新增证据
        recent_evidence = self.evidence_counts[-self.max_no_progress_steps :]
        if (
            len(recent_evidence) >= self.max_no_progress_steps
            and all(c == 0 for c in recent_evidence)
        ):
            hits.append(
                (
                    LoopSignal.NO_NEW_EVIDENCE,
                    f"连续 {self.max_no_progress_steps} 步没有新增证据",
                    {"steps": self.max_no_progress_steps},
                )
            )

        # 4. 同一错误类别持续出现
        if record.error_code:
            recent_errors = [a.error_code for a in self.actions[-self.same_error_window :]]
            if (
                len(recent_errors) >= self.same_error_window
                and all(e == record.error_code for e in recent_errors)
            ):
                hits.append(
                    (
                        LoopSignal.SAME_ERROR,
                        f"同一错误 {record.error_code} 连续出现 {self.same_error_window} 次",
                        {"error_code": record.error_code, "count": self.same_error_window},
                    )
                )

        # 5. 计划在多个状态间振荡（A->B->A->B）
        if len(self.plan_states) >= self.plan_oscillation_window:
            tail = self.plan_states[-self.plan_oscillation_window :]
            if tail[:2] == tail[2:] and len(set(tail[:2])) == 2:
                hits.append(
                    (
                        LoopSignal.PLAN_OSCILLATION,
                        f"计划在状态间振荡: {' -> '.join(tail)}",
                        {"states": tail},
                    )
                )

        # 6. Token 消耗增加但验收条件覆盖率不变
        if len(self.coverage_history) >= self.stalled_coverage_window:
            recent_coverage = self.coverage_history[-self.stalled_coverage_window :]
            if len(set(recent_coverage)) == 1 and recent_coverage[0] > 0:
                hits.append(
                    (
                        LoopSignal.STALLED_COVERAGE,
                        f"连续 {self.stalled_coverage_window} 步覆盖率无增长",
                        {"coverage": recent_coverage[0]},
                    )
                )

        return hits
