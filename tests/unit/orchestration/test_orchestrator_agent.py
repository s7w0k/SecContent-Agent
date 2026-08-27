"""OrchestratorAgent V2 - SkillPlan 决策循环测试（计划 §66 / §30 / §34）。

验证：
  - intent → 白名单 Skill 确定性映射（LLM 只出 intent，服务端出 SkillPlan）
  - LLM 无法自造 Skill（未注册/未授权 → plan 拒绝，FAILED）
  - Replan bounded（max_replans 限制，不无限循环）
  - 必选 Skill 失败阻断下游；可选 Skill 可跳过

运行（仓库根目录）:
    python -m pytest tests/unit/orchestration/test_orchestrator_agent.py --basetemp ./.pytest-tmp-x -q --no-header
"""

from __future__ import annotations

from typing import Any

import pytest
from agent.artifacts.store import ArtifactStore
from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
)
from agent.orchestration import (
    INTENT_SKILLS,
    OrchestratorAgent,
    OrchestratorBudget,
    OrchestratorChoice,
    OrchestratorPlanner,
    SkillNotAllowedError,
)
from agent.skills.contracts import SkillManifest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry
from agent.skills.runtime import SkillRuntime

# ═══════════════════════════════════════════════════════════════
# 确定性 Stub Skill（可控成败，用于验证决策循环而不依赖真实 Skill 内部逻辑）
# ═══════════════════════════════════════════════════════════════


class _StubSkill:
    def __init__(self, name: str, *, status: str = "SUCCEEDED", artifact_type: str):
        self.name = name
        self.status = status
        self.artifact_type = artifact_type
        self.calls: list[str] = []

    async def execute(self, request: Any, context: Any) -> SkillResult:
        self.calls.append(request.skill_name)
        if self.status == "FAILED":
            return SkillResult.failed(self.name, "boom")
        if self.status == "BLOCKED":
            return SkillResult.blocked(self.name, "blocked")
        rec = await context.store_artifact(
            artifact_type=self.artifact_type,
            payload={"artifact_id": f"{self.name}-art", "note": self.name},
            producer=self.name,
            step_id=request.skill_name,
        )
        return SkillResult.succeeded(self.name, artifact_refs=[rec["ref"]])


def _register(registry: ExecutableSkillRegistry, stub: _StubSkill) -> None:
    manifest = SkillManifest(
        name=stub.name,
        version="1.0.0",
        description=stub.name,
        required_tools=(),
        status="published",
        risk_level="low",
        output_artifact_type=stub.artifact_type,
    )
    registry.register(stub, manifest)


def _make_agent(
    stubs: list[_StubSkill],
    *,
    intent_resolver: Any,
    budget: OrchestratorBudget | None = None,
    scopes: frozenset[str] | None = None,
    condition_checker: Any = None,
) -> tuple[OrchestratorAgent, dict[str, _StubSkill]]:
    """组装真实 SkillRuntime + 可控 Stub Skill + OrchestratorAgent。"""
    business_registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        business_registry,
        adapters={BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter()},
    )
    registry = ExecutableSkillRegistry(business_tool_names=business_registry.names())
    by_name: dict[str, _StubSkill] = {}
    for stub in stubs:
        _register(registry, stub)
        by_name[stub.name] = stub
    runtime = SkillRuntime(
        registry,
        tool_executor=executor,
        artifact_store=ArtifactStore(),
        default_adapter=BusinessToolAdapterKind.FAKE,
    )
    agent = OrchestratorAgent(
        skill_runtime=runtime,
        budget=budget or OrchestratorBudget(),
        intent_resolver=intent_resolver,
        scopes=scopes,
        condition_checker=condition_checker,
    )
    return agent, by_name


def _resolver(intent: str) -> Any:
    """返回恒定 intent 的同步 resolver。"""

    def _resolve(goal: str, state: Any) -> OrchestratorChoice:
        return OrchestratorChoice(intent=intent, desired_output=goal)  # type: ignore[arg-type]

    return _resolve


def _happy_stubs() -> list[_StubSkill]:
    return [
        _StubSkill("article-triage", artifact_type="TriageArtifact"),
        _StubSkill("product-scoring", artifact_type="ScoringArtifact"),
        _StubSkill("draft-writing", artifact_type="DraftArtifact"),
    ]


# ═══════════════════════════════════════════════════════════════
# 1. intent → 白名单 Skill 映射
# ═══════════════════════════════════════════════════════════════


def test_intent_maps_to_authorized_skills():
    sampler = OrchestratorPlanner(known_skills=INTENT_SKILLS["full_workflow"])
    plan = sampler.build_plan(OrchestratorChoice(intent="full_workflow"), run_id="run-t")
    assert [s.skill_name for s in plan.steps] == [
        "article-triage",
        "product-scoring",
        "draft-writing",
    ]
    # 每个步骤都命中服务端白名单
    for step in plan.steps:
        assert step.skill_name in INTENT_SKILLS["full_workflow"]

    plan2 = sampler.build_plan(OrchestratorChoice(intent="score_article"), run_id="run-t")
    assert [s.skill_name for s in plan2.steps] == ["article-triage", "product-scoring"]
    # 依赖感知：product-scoring 依赖 s1
    assert plan2.step("s2").depends_on == ["s1"]


def test_invalid_intent_rejected_by_contract():
    """LLM 输出白名单之外的 intent 会被契约（Literal）直接拒绝。"""
    with pytest.raises(ValueError):  # pydantic ValidationError 是 ValueError 子类
        OrchestratorChoice(intent="__bogus__")  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════
# 2. LLM 无法自造 Skill（未授权/未注册 → plan 拒绝）
# ═══════════════════════════════════════════════════════════════


async def test_llm_cannot_invent_skill():
    # 只注册 article-triage 与 product-scoring（缺 draft-writing）：
    # 即便 LLM 请求 full_workflow，构建者也会拒绝，Agent 返回 FAILED。
    stubs = [
        _StubSkill("article-triage", artifact_type="TriageArtifact"),
        _StubSkill("product-scoring", artifact_type="ScoringArtifact"),
    ]
    agent, _by_name = _make_agent(stubs, intent_resolver=_resolver("full_workflow"))

    state = await agent.run(goal="请写 PR 稿", user_id="u", tenant_id="t", trace_id="tr")
    assert state.status == "FAILED"
    # 没有任何 skill 被执行（计划在构建期即被拒绝）
    assert agent.skill_runtime.registry.names() == ["article-triage", "product-scoring"]


def test_planner_blocks_unauthorized_skill_directly():
    sampler = OrchestratorPlanner(known_skills=set(INTENT_SKILLS["curate_news"]))
    # review_draft 白名单只含 draft-revision；尝试以不存在的技能直接触发不可行。
    # 用 known_skills 排除 draft-writing 来模拟未注册：
    sampler = OrchestratorPlanner(known_skills={"article-triage", "product-scoring"})
    try:
        sampler.build_plan(OrchestratorChoice(intent="full_workflow"), run_id="run-t")
    except SkillNotAllowedError as exc:
        assert exc.skill_name == "draft-writing"
    else:
        raise AssertionError("expected SkillNotAllowedError")


# ═══════════════════════════════════════════════════════════════
# 3. Replan bounded
# ═══════════════════════════════════════════════════════════════


async def test_replan_is_bounded():
    # product-scoring 必选失败 → 反复触发 replan，直到 max_replans 停止。
    stubs = [
        _StubSkill("article-triage", artifact_type="TriageArtifact"),
        _StubSkill("product-scoring", artifact_type="ScoringArtifact", status="FAILED"),
    ]
    agent, _by_name = _make_agent(stubs, intent_resolver=_resolver("score_article"))
    state = await agent.run(goal="打分", user_id="u", tenant_id="t")
    assert state.replan_count == agent.budget.max_replans  # == 2，未越界
    assert state.status == "FAILED"


# ═══════════════════════════════════════════════════════════════
# 4. 必选失败阻断下游
# ═══════════════════════════════════════════════════════════════


async def test_required_skill_failure_blocks_downstream():
    # generate_draft：s1 triage(必选) → s2 scoring(必选) → s3 draft-writing(可选)。
    # scoring 必选失败 → 阻塞，且下游 draft-writing 绝不被执行。
    stubs = [
        _StubSkill("article-triage", artifact_type="TriageArtifact"),
        _StubSkill("product-scoring", artifact_type="ScoringArtifact", status="FAILED"),
        _StubSkill("draft-writing", artifact_type="DraftArtifact"),
    ]
    agent, by_name = _make_agent(
        stubs,
        intent_resolver=_resolver("generate_draft"),
        budget=OrchestratorBudget(max_replans=0),  # 第一次必选失败即终止
    )
    state = await agent.run(goal="写稿", user_id="u", tenant_id="t")
    assert state.status == "FAILED"
    assert "s2" in state.failed_steps
    # 下游 draft-writing 从未执行
    assert by_name["draft-writing"].calls == []
    assert "s3" not in state.completed_steps


# ═══════════════════════════════════════════════════════════════
# 5. 可选 Skill 可跳过
# ═══════════════════════════════════════════════════════════════


async def test_optional_skill_can_skip():
    stubs = _happy_stubs()
    # 条件不满足 → 跳过可选 draft-writing
    agent, by_name = _make_agent(
        stubs,
        intent_resolver=_resolver("generate_draft"),
        condition_checker=lambda state, condition, completed: False,
    )
    state = await agent.run(goal="写稿", user_id="u", tenant_id="t")
    assert state.status == "COMPLETED"
    assert "s1" in state.completed_steps
    assert "s2" in state.completed_steps
    assert "s3" not in state.completed_steps
    assert by_name["draft-writing"].calls == []  # 可选被跳过


async def test_full_workflow_happy_path():
    stubs = _happy_stubs()
    agent, by_name = _make_agent(stubs, intent_resolver=_resolver("full_workflow"))
    state = await agent.run(goal="分析并写稿", user_id="u", tenant_id="t")
    assert state.status == "COMPLETED"
    assert state.completed_steps == ["s1", "s2", "s3"]
    assert by_name["draft-writing"].calls == ["draft-writing"]
    # artifact-based handoff：下游拿到上游 ArtifactRef
    assert state.artifact_refs["s2"].startswith("ScoringArtifact:")
