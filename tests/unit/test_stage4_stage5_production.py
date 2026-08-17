from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
)
from agent.contracts.task import SlotSource, SlotState, TaskEnvelope, TaskIntent
from agent.observation import InMemoryArtifactStore, ObservationNormalizer
from agent.production_plan import ProductionPlanValidator
from agent.rule_planner import RulePlannerV1
from agent.skill_governance import ReleaseStage, SkillPublicationService, SkillReleaseError
from agent.skill_registry import SkillRegistry, SkillResolutionError
from agent.workflow_validator import WorkflowDecision, WorkflowGoalValidator

ROOT = Path(__file__).resolve().parents[2]


def _task(intent: TaskIntent = TaskIntent.GENERATE_DRAFT) -> TaskEnvelope:
    task = TaskEnvelope.from_user_input(
        task_id="run-stage5",
        thread_id="thread-stage5",
        user_id="u1",
        tenant_id="tenant-1",
        goal="generate a reviewed draft",
        intent=intent,
        acceptance_criteria=["reviewed draft exists"],
    )
    return task.model_copy(
        update={
            "selected_article_ids": SlotState.from_value(
                ["article-123"], source=SlotSource.USER, confirmed=True
            ),
            "product_ids": SlotState.from_value(
                ["agent-security"], source=SlotSource.USER, confirmed=True
            ),
        }
    )


def test_all_nine_skill_v2_packages_are_published_and_linted():
    tools = build_business_tool_registry()
    registry = SkillRegistry(
        ROOT / "agent-security-briefs" / "skills", known_tools=set(tools.names())
    )
    snapshot = registry.load()
    assert len(snapshot.skills) == 9
    for manifest in snapshot.skills.values():
        assert manifest.schema_version == "2.0"
        assert manifest.status == "published"
        assert manifest.required_tools
        assert manifest.preconditions and manifest.postconditions
        dataset = ROOT / "agent-security-briefs" / "skills" / manifest.name / manifest.eval_datasets[0]
        assert len(dataset.read_text(encoding="utf-8").splitlines()) >= 10


def test_skill_selection_is_minimal_deterministic_and_fail_closed():
    registry = SkillRegistry(ROOT / "agent-security-briefs" / "skills")
    registry.load()
    one = registry.select(intent="review_draft", plan_tools=["review_draft"])
    two = registry.select(intent="review_draft", plan_tools=["review_draft"])
    assert one.version_refs == ("compliance-review@2.0.0",)
    assert one.selection_hash == two.selection_hash
    assert "news-discovery" not in one.version_refs
    with pytest.raises(SkillResolutionError, match="token"):
        registry.select(intent="search_and_draft", token_budget=10)


async def test_skill_release_pipeline_requires_eval_shadow_and_approval():
    service = SkillPublicationService()
    manifest = {"eval_datasets": ["evals/cases.jsonl"], "purpose": "test"}
    release = await service.create_draft(name="draft-writing", version="3.0.0", manifest=manifest)
    assert release.stage == ReleaseStage.DRAFT
    with pytest.raises(SkillReleaseError):
        await service.publish(release.release_id, publisher="ops")
    release = await service.validate(release.release_id)
    release = await service.record_offline_eval(release.release_id, report_ref="report://1", passed=True)
    release = await service.record_shadow(release.release_id, report_ref="shadow://1")
    release = await service.approve(release.release_id, approver="reviewer")
    release = await service.publish(release.release_id, publisher="ops")
    assert release.stage == ReleaseStage.PUBLISHED
    assert await service.freeze_published(["draft-writing"]) == {"draft-writing": "3.0.0"}
    with pytest.raises(SkillReleaseError, match="immutable"):
        await service.create_draft(
            name="draft-writing", version="3.0.0", manifest={**manifest, "purpose": "changed"}
        )


async def test_rule_planner_builds_valid_minimal_known_article_plan():
    registry = build_business_tool_registry()
    task = _task()
    outcome = await RulePlannerV1(registry).plan(task)
    assert outcome.validation.accepted
    assert [step.tool for step in outcome.plan.steps] == [
        "get_article",
        "classify_article",
        "match_products",
        "score_article",
        "generate_draft",
        "review_draft",
    ]
    assert outcome.plan.direct_pipeline == "pipeline_v2.known_article_draft"
    assert ProductionPlanValidator(registry).validate(
        outcome.plan,
        confirmed_slots={"intent", "goal", "acceptance_criteria", "selected_article_ids", "product_ids"},
        expected_run_id="run-stage5",
        expected_task_snapshot_hash=task.fingerprint(),
    ).accepted


async def test_observation_normalizer_externalizes_large_payload_and_sanitizes_failure():
    registry = build_business_tool_registry()
    contract = registry.get("list_articles")
    store = InMemoryArtifactStore()
    normalizer = ObservationNormalizer(store, inline_limit=1000)
    item = {
        "article_id": "a1",
        "title": "x" * 500,
        "summary": "y" * 1500,
    }
    result = contract.result_schema.model_validate({"items": [item], "total": 1, "replay_ref": "r1"})
    observation = await normalizer.success(contract, result)
    assert observation.ok and observation.artifact_ref in store.items
    failure = normalizer.failure(RuntimeError("api_key=super-secret raw failure"))
    assert not failure.ok and failure.reason_code == "tool_internal_error"
    assert "super-secret" not in json.dumps(failure.model_dump(mode="json"))


async def test_fake_business_tools_complete_validated_draft_workflow():
    registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        registry,
        adapters={BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter()},
    )
    from agent.production_runtime import ProductionActionPlanner, ProductionBusinessExecutor
    from agent.runtime_state import RuntimeState

    task = _task()
    state = RuntimeState(
        run_id=task.task_id,
        user_id=task.user_id,
        tenant_id=task.tenant_id,
        task_envelope=task,
        slot_states=task.slot_states(),
        goal=str(task.goal.value),
        acceptance_criteria=["reviewed draft exists"],
    )
    planner = ProductionActionPlanner(RulePlannerV1(registry))
    runner = ProductionBusinessExecutor(executor, planner, adapter=BusinessToolAdapterKind.FAKE)
    while True:
        action = await planner(state)
        if action is None:
            break
        result = await runner(state, action, {})
        assert result["ok"], result
        state = state.model_copy(update={"completed_steps": [*state.completed_steps, action.step_id]})
    decision = WorkflowGoalValidator().validate(planner.plan, planner.observations)
    assert decision.decision == WorkflowDecision.COMPLETE
    assert planner.observations["draft"].data["artifact"]["artifact_id"]


def test_autonomous_production_assembly_fails_closed_when_incomplete():
    from agent.autonomous_service import AutonomousRunService
    from agent.business_tools.execution import FakeBusinessToolAdapter

    with pytest.raises(RuntimeError, match="requires business executor and registry"):
        AutonomousRunService(
            store=object(),
            event_store=object(),
            business_executor=FakeBusinessToolAdapter(),
        )
