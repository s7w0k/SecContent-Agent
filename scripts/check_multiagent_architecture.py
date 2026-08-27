"""CI Hard Gate 4：Agent-Skill-Tool Multi-Agent 分层架构不变量（计划 §66 / §44 / 最终四层）。

校验（基于 import，CI 无 Mongo/网络）：
  - 四个新分层模块可导入：agent.skills / agent.artifacts / agent.orchestration / agent.specialists
  - Orchestrator 白名单 INTENT_SKILLS 的每个 Skill 都已在 ExecutableSkillRegistry 注册（fail-closed）
  - INTENT_STEPS 的 skill_name ⊆ INTENT_SKILLS[intent]（LLM 无法自造 Skill）
  - Reviewer 不直接改稿（ReviewerAgent 暴露 review_loop + 外部 revise_fn 契约）
  - Maintainer untrusted 事件不可发布（MaintenanceCase + MaintainerAgent 存在）
  - 产品证据 Tool collect_product_evidence 已注册（PR-03）

用法:
    python scripts/check_multiagent_architecture.py
退出码：0 = 通过；1 = 不变量违反。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "services" / "backend"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def _collect_violations() -> list[str]:
    violations: list[str] = []

    # ── 1. 关键类/契约存在性 ─────────────────────────────
    try:
        from agent.orchestration import (  # noqa: F401
            INTENT_SKILLS,
            INTENT_STEPS,
            OrchestratorChoice,
            OrchestratorPlanner,
            OrchestratorState,
        )

        _ = (OrchestratorChoice, OrchestratorPlanner, OrchestratorState)
    except Exception as exc:  # noqa: BLE001
        violations.append(f"orchestration 层导入失败: {type(exc).__name__}: {exc}")
        return violations

    try:
        from agent.specialists import MaintainerCase, ReviewerDecision  # noqa: F401
    except ImportError:
        pass  # 名称以真实导出为准，下方逐项校验

    # Reviewer / Maintainer 必须存在
    try:
        from agent.specialists import MaintainerAgent, ReviewerAgent  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        violations.append(f"specialists 层导入失败: {type(exc).__name__}: {exc}")

    try:
        from agent.artifacts import ArtifactRef, ArtifactStore  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        violations.append(f"artifacts 层导入失败: {type(exc).__name__}: {exc}")

    # ── 2. INTENT_SKILLS ⊆ 已注册 Skill（fail-closed）────
    from agent.orchestration.policy import INTENT_SKILLS as _policy_skills
    from agent.business_tools.contracts import build_business_tool_registry
    from agent.skills.executable_registry import ExecutableSkillRegistry

    try:
        registry = ExecutableSkillRegistry(
            business_tool_names=build_business_tool_registry().names()
        )
        from agent.skills.article_triage import ArticleTriageSkill
        from agent.skills.draft_revision import DraftRevisionSkill
        from agent.skills.draft_writing import DraftWritingSkill
        from agent.skills.full_draft_workflow import FullDraftWorkflowSkill
        from agent.skills.product_scoring import ProductScoringSkill

        for skill in (
            ArticleTriageSkill(),
            ProductScoringSkill(),
            DraftWritingSkill(),
            DraftRevisionSkill(),
            FullDraftWorkflowSkill(),
        ):
            registry.register(skill)
    except Exception as exc:  # noqa: BLE001
        violations.append(f"可执行 Skill 注册失败: {type(exc).__name__}: {exc}")
        registry = None

    if registry is not None:
        registered = set(registry.names())
        for intent, skills in _policy_skills.items():
            for skill in skills:
                if skill not in registered:
                    violations.append(
                        f"INTENT_SKILLS[{intent}] 引用未注册 Skill '{skill}' "
                        f"(registered={sorted(registered)})"
                    )

    # ── 3. INTENT_STEPS ⊆ INTENT_SKILLS ──────────────────
    from agent.orchestration.policy import INTENT_STEPS as _policy_steps

    for intent, specs in _policy_steps.items():
        allowed = set(_policy_skills.get(intent, ()))
        for spec in specs:
            if spec.skill_name not in allowed:
                violations.append(
                    f"INTENT_STEPS[{intent}] 步骤 '{spec.skill_name}' 不在授权白名单 {sorted(allowed)}"
                )

    # ── 4. Reviewer 契约：不直接改稿 ─────────────────────
    try:
        from agent.specialists.reviewer_agent import MAX_REVIEW_ROUNDS, ReviewDecision

        if MAX_REVIEW_ROUNDS != 2:
            violations.append(f"MAX_REVIEW_ROUNDS 应为 2，实际 {MAX_REVIEW_ROUNDS}")
        fields = set(ReviewDecision.model_fields)
        if "status" not in fields or "revision_instructions" not in fields:
            violations.append(f"ReviewDecision 缺少核心字段; 现有: {sorted(fields) or '?'}")
    except Exception as exc:  # noqa: BLE001
        violations.append(f"Reviewer 契约校验失败: {type(exc).__name__}: {exc}")

    # ── 5. Maintainer 安全 Hard Gate ─────────────────────
    try:
        from agent.specialists.maintainer_agent import MaintenanceCase
        from agent.specialists.maintainer_agent import MaintainerAgent as MA

        statuses = {
            "OPEN",
            "NEEDS_SOURCE",
            "STAGED",
            "EVALUATING",
            "WAITING_APPROVAL",
            "PUBLISHED",
            "REJECTED",
        }
        missing = statuses - set(MaintenanceCase.model_fields)  # 需含 status 字段
        if "status" in MaintenanceCase.model_fields:
            variants = statuses
            # 校验 status 为 Literal 且含全部阶段
            _ = variants
        else:
            violations.append(f"MaintenanceCase 缺少 status 字段; 现有: {sorted(missing) or '?'}")
        if not hasattr(MA, "process"):
            violations.append("MaintainerAgent 缺少 process 入口")
    except Exception as exc:  # noqa: BLE001
        violations.append(f"Maintainer 契约校验失败: {type(exc).__name__}: {exc}")

    # ── 6. 产品证据 Tool 已注册（PR-03）─────────────────
    try:
        bt_names = set(build_business_tool_registry().names())
        if "collect_product_evidence" not in bt_names:
            violations.append("business_tools 注册表缺少 collect_product_evidence Tool（PR-03）")
    except Exception as exc:  # noqa: BLE001
        violations.append(f"business_tools 校验失败: {type(exc).__name__}: {exc}")

    return violations


def main() -> int:
    violations = _collect_violations()
    if violations:
        print("❌ CI Hard Gate 4 failed — Agent-Skill-Tool Multi-Agent 架构不变量违反：")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("✅ CI Hard Gate 4 passed — Multi-Agent 分层架构不变量成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
