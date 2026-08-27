"""LLM Navigation Decision Layer（Final 计划 PR-A）。

职责边界（极其窄）：
  - LLM 只做"选择"，绝不直接访问文件 / 操作 WikiStore。
  - LLM 只能从系统给定的 Candidate Page 中选 target，不得发明 page_id。
  - 任何 timeout / 异常 / 非法结构化输出 / 非法 action，都由调用方回退到
    deterministic policy（fallback 决策在本文件也提供 `deterministic_decision`）。
  - Candidate 只传 PageDescriptor（page_id/title/page_type/summary/...），
    禁止把整页正文或整个 Wiki 发给 Navigator LLM。

调用方（WikiNavigator）负责：
  - validate_action（Action Validator / Harness）
  - 防循环（repeated action / invalid action / llm failure 计数）
  - budget（max_pages / max_depth / max_tool_calls / token_budget）在 harness 层强制
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.navigation_decider")

# LLM 允许选择的下一步动作（白名单，§4.1）。比 Navigator 全量 ALLOWED_ACTIONS 更窄，
# 且不含 RESOLVE_ENTITY / LIST_LINKS / READ_SOURCE 这类"访问类"动作。
DecoderAction = Literal[
    "OPEN_PAGE",
    "FOLLOW_LINK",
    "OPEN_SECTION",
    "SEARCH_PAGES",
    "ASSESS_EVIDENCE",
    "STOP_SUFFICIENT",
    "STOP_INSUFFICIENT",
]

ALLOWED_NAVIGATION_ACTIONS: frozenset[str] = frozenset(
    {
        "OPEN_PAGE",
        "FOLLOW_LINK",
        "OPEN_SECTION",
        "SEARCH_PAGES",
        "ASSESS_EVIDENCE",
        "STOP_SUFFICIENT",
        "STOP_INSUFFICIENT",
    }
)


class NavigationAction(BaseModel):
    """LLM 提议的下一步导航动作（§4.1）。"""

    action: DecoderAction
    target: str | None = Field(default=None, description="Candidate 页面 id；STOP/ASSESS 可不填")
    requirement_id: str | None = Field(default=None, description="针对哪个缺失 Requirement")
    reason: str = Field(default="", description="决策理由（仅观测，不参与执行）")


class PageDescriptor(BaseModel):
    """只向 LLM 暴露的最小页面画像（§4.3）。"""

    page_id: str
    title: str = Field(default="")
    page_type: str = Field(default="")
    summary: str = Field(default="", description="≤300 字符摘要")
    via_relation: str = Field(default="")
    depth: int = Field(default=0)
    task_affinity: list[str] = Field(default_factory=list, description="可能满足的 Requirement id")


class NavigationDecisionContext(BaseModel):
    """交给 LLM 的决策上下文（全部来自 Wrap，LLM 无法越权）。"""

    query: str
    task_type: str
    requirements: list[dict] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    visited_pages: list[str] = Field(default_factory=list)
    candidate_pages: list[dict] = Field(default_factory=list)
    pages_remaining: int = Field(default=0)
    tool_calls_remaining: int = Field(default=0)
    tokens_remaining: int = Field(default=0)


class NavigationDecisionError(Exception):
    """LLM 导航决策失败（timeout/exception/malformed），调用方应走 deterministic fallback。"""


class LLMNavigationDecider:
    """把 NavigationDecisionContext 交给 LLM，返回受白名单约束的 NavigationAction。

    Structured Output 由 `llm.with_structured_output(NavigationAction)` 提供。
    timeout、异常、非法结构化输出统一转成 `NavigationDecisionError`，
    由 `WikiNavigator` 计数并回退 deterministic policy。
    """

    def __init__(
        self,
        llm: Any | None = None,
        *,
        timeout: float | None = 10.0,
        max_candidates: int = 8,
        max_summary: int = 300,
    ):
        self.llm = llm
        self.timeout = timeout
        self.max_candidates = max_candidates
        self.max_summary = max_summary

    async def decide(self, context: NavigationDecisionContext) -> NavigationAction:
        """调用 LLM 并做一次结构校验；失败抛 NavigationDecisionError。"""
        if self.llm is None:
            raise NavigationDecisionError("LLM 未配置")

        prompt = self._user_prompt(context)
        try:
            if self.timeout is not None:
                response = await asyncio.wait_for(
                    self._invoke_structured(prompt), timeout=self.timeout
                )
            else:
                response = await self._invoke_structured(prompt)
        except TimeoutError as exc:
            raise NavigationDecisionError("LLM navigation timeout") from exc
        except Exception as exc:
            logger.warning("LLM navigation failed: %s", exc)
            raise NavigationDecisionError(f"LLM navigation failure: {exc}") from exc

        if not self._validate_structured(response, context):
            raise NavigationDecisionError("LLM navigation malformed output")
        return response

    # ── 可替换的 LLM 调用点（便于测试注入）─────────────────
    async def _invoke_structured(self, prompt: str):
        model = self.llm.with_structured_output(NavigationAction)
        return await model.ainvoke(prompt)

    def _user_prompt(self, context: NavigationDecisionContext) -> str:
        lines = [
            "你不是回答用户问题。你只负责选择下一步知识导航动作。",
            "规则：",
            "1. 只能选择 Candidate Pages 中存在的 target，不得生成新 page_id。",
            "2. 优先补齐 missing_requirements。",
            "3. 证据充分时可 STOP_SUFFICIENT。",
            "4. 没有可补齐证据时 STOP_INSUFFICIENT。",
            "5. 不允许依赖模型常识补充产品事实。",
            "",
            "任务类型: " + context.task_type,
            "查询: " + context.query,
            "缺失需求: " + ",".join(context.missing_requirements or ["<none>"]) ,
            "已访问页面: " + ",".join(context.visited_pages[: self.max_candidates]),
            "页面预算剩余: " + str(context.pages_remaining),
            "动作预算剩余: " + str(context.tool_calls_remaining),
            "Token 预算剩余: " + str(context.tokens_remaining),
            "",
            "Candidate Pages:",
        ]
        for c in context.candidate_pages[: self.max_candidates]:
            lines.append(
                f"- {c.get('page_id')} | type={c.get('page_type','')} "
                f"| title={c.get('title','')} | affinity={c.get('task_affinity', [])}"
            )
        return "\n".join(lines)

    def _validate_structured(self, response: Any, context: NavigationDecisionContext) -> bool:
        """结构化输出校验：action 白名单 + target 必须存在于 Candidate（§4.2/§4.3）。"""
        if response is None:
            return False
        action: str = getattr(response, "action", "")
        if action not in ALLOWED_NAVIGATION_ACTIONS:
            return False
        if action in {"STOP_SUFFICIENT", "STOP_INSUFFICIENT", "ASSESS_EVIDENCE", "SEARCH_PAGES"}:
            return True  # 无需 target
        target = getattr(response, "target", None)
        if not target:
            return False
        candidate_ids = {c.get("page_id") for c in context.candidate_pages}
        return target in candidate_ids


def deterministic_decision(
    candidates: list[dict],
    *,
    missing_requirements: list[str],
    prefer_types_by_requirement: dict[str, list[str]] | None = None,
) -> NavigationAction:
    """确定性的兜底决策（harness 回退 / LLM 关闭时）。

    优先选择能补齐缺失 Requirement 的 Candidate（按 task_affinity 匹配），
    否则选择第一个 Candidate。无 Candidate 时返回 STOP_INSUFFICIENT。
    """
    prefer_types_by_requirement = prefer_types_by_requirement or {}
    # 优先挑选缺失 requirement 相关的 candidate
    for c in candidates:
        affinity = set(c.get("task_affinity", []))
        if affinity & set(missing_requirements):
            return NavigationAction(
                action="OPEN_PAGE",
                target=c.get("page_id"),
                requirement_id=(affinity & set(missing_requirements)).pop(),
                reason="deterministic:补齐缺失需求",
            )
    if not candidates:
        return NavigationAction(action="STOP_INSUFFICIENT", reason="deterministic:无候选可导航")
    first = candidates[0]
    return NavigationAction(
        action="OPEN_PAGE",
        target=first.get("page_id"),
        reason="deterministic:按候选顺序",
    )
