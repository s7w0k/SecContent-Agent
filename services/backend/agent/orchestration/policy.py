"""Orchestrator 分层策略 - INTENT_SKILLS 服务端白名单（计划 §66 / §30 / §31）。

安全不变量：
  - LLM 只能输出受约束的 OrchestratorChoice（intent）。
  - 具体 Skill 名 / 步骤由服务端白名单 INTENT_SKILLS + INTENT_STEPS 决定。
  - LLM 无法自造 Skill 名或 Tool 调用（DoD §66：LLM 不创造 Skill 名）。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.orchestration.contracts import Intent

# 计划 §30：每意图允许执行的 Skill 白名单（值必须与 ExecutableSkillRegistry 注册名一致）。
INTENT_SKILLS: dict[Intent, tuple[str, ...]] = {
    "curate_news": ("article-triage",),
    "score_article": ("article-triage", "product-scoring"),
    "generate_draft": ("article-triage", "product-scoring", "draft-writing"),
    "review_draft": ("draft-revision",),
    "revise_draft": ("draft-revision",),
    "full_workflow": ("article-triage", "product-scoring", "draft-writing"),
}


@dataclass(frozen=True)
class StepSpec:
    """单个 Skill Step 的服务端规格。"""

    skill_name: str
    required: bool = True  # 必选失败 → 阻断下游；可选失败/条件不满足 → 跳过
    depends_on: tuple[str, ...] = ()
    condition: str = ""  # 空条件视为恒真（默认放行）
    max_attempts: int = 1


# 计划 §31：每意图的 Skill Step 序列（s1 → s2 → … 依赖递增）。
# generate_draft / full_workflow 的写稿是"可选 + 评分达标才执行"。
INTENT_STEPS: dict[Intent, tuple[StepSpec, ...]] = {
    "curate_news": (StepSpec(skill_name="article-triage"),),
    "score_article": (
        StepSpec(skill_name="article-triage"),
        StepSpec(skill_name="product-scoring", depends_on=("s1",)),
    ),
    "generate_draft": (
        StepSpec(skill_name="article-triage"),
        StepSpec(skill_name="product-scoring", depends_on=("s1",)),
        StepSpec(
            skill_name="draft-writing",
            required=False,
            depends_on=("s2",),
            condition="score_sufficient",
        ),
    ),
    "review_draft": (StepSpec(skill_name="draft-revision"),),
    "revise_draft": (StepSpec(skill_name="draft-revision"),),
    "full_workflow": (
        StepSpec(skill_name="article-triage"),
        StepSpec(skill_name="product-scoring", depends_on=("s1",)),
        StepSpec(
            skill_name="draft-writing",
            required=False,
            depends_on=("s2",),
            condition="score_sufficient",
        ),
    ),
}


def authorize_skill(intent: Intent, skill_name: str) -> bool:
    """intent 是否授权执行 skill_name（白名单判定）。"""
    return skill_name in INTENT_SKILLS.get(intent, ())


def _step_index(step_id: str) -> int | None:
    """把 'sN' 转为 0 基序号；非法则 None。"""
    if step_id.startswith("s") and step_id[1:].isdigit():
        return int(step_id[1:]) - 1
    return None


def step_is_required(intent: Intent, step_id: str) -> bool:
    """某个 step 是否按策略为"必选"。非法 step 一律视为可跳过，避免误阻断。"""
    steps = INTENT_STEPS.get(intent, ())
    idx = _step_index(step_id)
    if idx is None or idx >= len(steps):
        return False
    return steps[idx].required


__all__ = [
    "INTENT_SKILLS",
    "INTENT_STEPS",
    "StepSpec",
    "authorize_skill",
    "step_is_required",
]
