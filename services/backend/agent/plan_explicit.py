"""显式 Plan（形态 A）— 对话引擎执行前的用户可见步骤计划（改造计划 P1 切片）。

职责：
  1. 定义 ExplicitPlan / ExplicitPlanStep：执行前产出的步骤模型；
  2. build_deterministic_plan：确定性兜底计划（无需 LLM，按 PR 生产惯例排序）；
  3. sanitize_plan：只保留白名单内的工具/字段，上游（模型/调用方）不能注入
     任意工具或步骤 —— 与服务端白名单约束原则一致。

安全不变量：
  - 步骤工具名必须 ∈ allowed_tools，否则丢弃；
  - 计划仅描述"要做什么"，身份/预算/策略门仍由运行时执行层负责；
  - 步骤上限由 max_steps 硬约束，超出部分合并进最终兜底步骤。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# PR 生产链路工具的确定性优先顺序（可执行 PR 旅程时的默认排布）
PR_TOOL_ORDER: tuple[str, ...] = (
    "get_article",
    "list_articles",
    "search_news",
    "crawl_news",
    "classify_article",
    "match_products",
    "collect_product_evidence",
    "score_article",
    "generate_draft",
    "review_draft",
    "revise_draft",
    "save_draft_version",
    "export_draft",
)

# 已知工具的步骤标题（默认文案，UI 直接展示）
_TOOL_TITLES: dict[str, str] = {
    "get_article": "读取指定文章",
    "list_articles": "查看可用文章",
    "search_news": "检索候选新闻",
    "crawl_news": "抓取补充新闻",
    "classify_article": "分类并确认主题相关",
    "match_products": "匹配相关安全产品",
    "collect_product_evidence": "收集产品知识证据",
    "score_article": "双维度评分",
    "generate_draft": "生成 PR 初稿",
    "review_draft": "内容与红线审查",
    "revise_draft": "按意见修订稿件",
    "save_draft_version": "保存稿件版本",
    "export_draft": "导出稿件",
}

_GENERIC_TITLE = "执行其余必要操作"
_MAX_KNOWN_STEPS = 8


class ExplicitPlanStep(BaseModel):
    """单个计划步骤：step_id 稳定、tools 白名单内、expected_output 描述交付物。"""

    step_id: str
    title: str = Field(default="", max_length=160)
    tools: list[str] = Field(default_factory=list)
    expected_output: str = Field(default="", max_length=300)


class ExplicitPlan(BaseModel):
    """执行前产出的显式计划。"""

    plan_id: str
    steps: list[ExplicitPlanStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def fingerprint(self) -> str:
        """计划指纹：同一 goal/工具集产出同指纹，便于 Trace 还原与幂等展示。"""
        raw = json.dumps(
            [s.model_dump(mode="json") for s in self.steps],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _step_title_for(tools: list[str]) -> str:
    first = next((_TOOL_TITLES.get(t) for t in tools if t in _TOOL_TITLES), None)
    if first:
        return first
    return _GENERIC_TITLE


def sanitize_plan(
    raw: Any,
    *,
    allowed_tools: set[str],
    max_steps: int = 6,
    run_id: str = "",
) -> ExplicitPlan | None:
    """清洗上游计划：仅保留白名单工具、非空步骤、受 max_steps 约束。"""
    if not raw:
        return None
    raw_steps = raw.get("steps") if isinstance(raw, dict) else None
    if not isinstance(raw_steps, list) or not raw_steps:
        return None
    cleaned: list[ExplicitPlanStep] = []
    for idx, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        tools = [str(t) for t in item.get("tools") or [] if str(t) in allowed_tools]
        if not tools:
            continue
        cleaned.append(
            ExplicitPlanStep(
                step_id=str(item.get("step_id") or f"s{idx}"),
                title=str(item.get("title") or _step_title_for(tools))[:160],
                tools=tools,
                expected_output=str(item.get("expected_output") or "")[:300],
            )
        )
        if len(cleaned) >= max_steps:
            break
    if not cleaned:
        return None
    return ExplicitPlan(plan_id=run_id or f"plan-{idx}", steps=cleaned)


def build_deterministic_plan(
    allowed_tools: set[str],
    *,
    max_steps: int = 6,
    run_id: str = "",
) -> ExplicitPlan:
    """确定性兜底计划：按 PR 生产惯例分阶段排布白名单工具。

    每个阶段一个步骤（title=tools 中已知工具标题）；若阶段数超过 max_steps，
    将尾部合并进一个通用步骤，保证计划不超过上限且不漏掉必要的写工具。
    """
    ordered = [t for t in PR_TOOL_ORDER if t in allowed_tools]
    # 阶段：读 → 搜/抓 → 分类 → 产品/证据 → 评分 → 写 → 审 → 改/存/导出
    phases: list[list[str]] = [
        [t for t in ("get_article", "list_articles") if t in ordered],
        [t for t in ("search_news", "crawl_news") if t in ordered],
        [t for t in ("classify_article",) if t in ordered],
        [t for t in ("match_products", "collect_product_evidence") if t in ordered],
        [t for t in ("score_article",) if t in ordered],
        [t for t in ("generate_draft",) if t in ordered],
        [t for t in ("review_draft",) if t in ordered],
        [t for t in ("revise_draft", "save_draft_version", "export_draft") if t in ordered],
    ]
    phases = [phase for phase in phases if phase]
    # 白名单中存在但未在惯例顺序里的工具（自定义/新增）追加到通用兜底步骤
    leftovers = sorted(allowed_tools - set(PR_TOOL_ORDER))

    steps: list[ExplicitPlanStep] = []
    for phase in phases:
        steps.append(
            ExplicitPlanStep(
                step_id=f"s{len(steps) + 1}",
                title=_step_title_for(phase),
                tools=phase,
                expected_output=_TOOL_TITLES.get(phase[0], ""),
            )
        )
    reserve_leftovers = 1 if leftovers else 0
    if len(steps) + reserve_leftovers > max_steps:
        # 超限：从尾部向前收，前 keep 段保留，其余阶段并入一个收尾步骤；
        # 收尾步骤自身占用一个名额，且要为 leftovers 兜底步骤预留名额。
        keep = len(steps)
        while keep > 1 and keep + 1 + reserve_leftovers > max_steps:
            keep -= 1
        merged_tools = [tool for phase in phases[keep:] for tool in phase]
        steps = steps[:keep]
        if merged_tools:
            steps.append(
                ExplicitPlanStep(
                    step_id=f"s{len(steps) + 1}",
                    title=_GENERIC_TITLE,
                    tools=merged_tools,
                    expected_output="",
                )
            )
    if reserve_leftovers and len(steps) >= max_steps:
        # 预算已满：把兜底工具并入最后一个步骤，保证不超出上限
        steps[-1] = steps[-1].model_copy(
            update={"tools": steps[-1].tools + leftovers}
        )
        leftovers = []
    elif leftovers:
        steps.append(
            ExplicitPlanStep(
                step_id=f"s{len(steps) + 1}",
                title=_GENERIC_TITLE,
                tools=leftovers,
                expected_output="",
            )
        )
    if not steps:
        steps.append(
            ExplicitPlanStep(
                step_id="s1",
                title="按用户请求执行",
                tools=sorted(allowed_tools),
                expected_output="",
            )
        )
    return ExplicitPlan(plan_id=run_id or "plan-deterministic", steps=steps)
