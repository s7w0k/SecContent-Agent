from __future__ import annotations

from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import ReadOnlyProductionBusinessToolAdapter
from agent.contracts.task import SlotSource, SlotState, TaskEnvelope, TaskIntent
from agent.harness.full_loop_harness import (
    CAPACITY_SCENARIOS,
    RECOVERY_SCENARIOS,
    SECURITY_SCENARIOS,
    ConversationSample,
    FullLoopAcceptanceReport,
    JourneyExecution,
    JourneyHarnessRunner,
    PlannerMutationHarness,
    build_core_60_journeys,
    compare_domain_metrics,
    conversation_metrics,
    validate_metric_tags,
)
from agent.learning_governance import (
    DraftCandidateRegistry,
    FeedbackAction,
    FeedbackLedger,
    GovernedMemory,
    GovernedMemoryStore,
    LearningFeedbackEvent,
    MemoryLayer,
    PairedEvalGate,
    SkillChangeCandidate,
)
from agent.migration_controller import (
    MigrationController,
    MigrationStage,
    PromotionGate,
    stable_user_bucket,
)
from agent.production_plan import ProductionPlanValidator
from agent.rule_planner import RulePlannerV1


def test_memory_provenance_conflict_delete_and_tenant_isolation():
    store = GovernedMemoryStore()
    user = store.put(
        GovernedMemory(
            tenant_id="t1",
            user_id="u1",
            layer=MemoryLayer.USER,
            key="tone",
            value="casual",
            provenance="explicit-profile",
        )
    )
    store.put(
        GovernedMemory(
            tenant_id="t1",
            layer=MemoryLayer.ORGANIZATION,
            key="tone",
            value="compliant",
            provenance="compliance-skill@2.0.0",
        )
    )
    resolved = store.resolve(tenant_id="t1", user_id="u1", key="tone")
    assert resolved and resolved.value == "compliant"
    assert resolved.provenance == "compliance-skill@2.0.0"
    assert store.delete(memory_id=user.memory_id, tenant_id="t1", user_id="u1")
    assert store.resolve(tenant_id="t2", user_id="u1", key="tone") is None


def test_feedback_is_idempotent_minimized_reversible_and_tenant_bound():
    ledger = FeedbackLedger()
    event = LearningFeedbackEvent(
        idempotency_key="turn-1:save",
        tenant_id="t1",
        user_id="u1",
        run_id="r1",
        action=FeedbackAction.SAVE,
        target_ref="artifact:a1",
        context={"draft_content": "sensitive draft", "version": 2},
        confidence=0.8,
    )
    first = ledger.append(event)
    second = ledger.append(event.model_copy(update={"event_id": "different"}))
    assert first.event_id == second.event_id
    assert "draft_content" not in first.context
    assert first.context["draft_content_hash"].startswith("sha256:")
    ledger.undo(event_id=first.event_id, tenant_id="t1", user_id="u1")
    assert ledger.active(tenant_id="t1", user_id="u1") == []


def test_automatic_skill_candidate_is_draft_only_and_paired_gate_is_fail_closed():
    registry = DraftCandidateRegistry()
    candidate = registry.create(
        SkillChangeCandidate(
            target_type="template_ranker",
            base_version="v1",
            source_dataset_id="dataset-1",
            baseline_snapshot_id="baseline-1",
            hypothesis="Increase correct template ranking on ambiguous cases",
            target_failures=("case-7",),
            expected_metrics={"precision_at_1": 0.03},
            content={"weights": {"security": 1.1}},
        )
    )
    assert candidate.status == "draft"
    assert not registry.executable(candidate.candidate_id)
    passed = PairedEvalGate.evaluate(
        baseline_quality=[0.50, 0.55, 0.60, 0.52],
        candidate_quality=[0.60, 0.65, 0.70, 0.62],
        baseline_security_failures=0,
        candidate_security_failures=0,
        baseline_factual_failures=0,
        candidate_factual_failures=0,
        token_cost_delta_ratio=0.05,
    )
    assert passed.passed and passed.confidence_interval_95[0] > 0
    failed = PairedEvalGate.evaluate(
        baseline_quality=[0.5, 0.5],
        candidate_quality=[0.7, 0.7],
        baseline_security_failures=0,
        candidate_security_failures=1,
        baseline_factual_failures=0,
        candidate_factual_failures=0,
    )
    assert not failed.passed and "security_non_regression" in failed.reason_codes


def test_core_60_conversation_domain_security_recovery_and_capacity_contracts():
    journeys = build_core_60_journeys()
    assert len(journeys) == 60 and len({item.case_id for item in journeys}) == 60
    metrics = conversation_metrics(
        [
            ConversationSample(
                expected_intent="draft",
                predicted_intent="draft",
                expected_slots=frozenset({"article"}),
                predicted_slots=frozenset({"article"}),
                clarification_required=False,
            ),
            ConversationSample(
                expected_intent="search",
                predicted_intent="search",
                clarification_required=True,
                clarification_asked=True,
            ),
        ]
    )
    assert metrics["intent_accuracy"] == 1.0
    assert metrics["slot_f1"] == 1.0
    domain = compare_domain_metrics(
        {"classification_macro_f1": 0.80, "product_precision_at_3": 0.75},
        {"classification_macro_f1": 0.84, "product_precision_at_3": 0.78},
    )
    assert domain["classification_macro_f1"]["delta"] == 0.04
    assert len(SECURITY_SCENARIOS) >= 12
    assert len(RECOVERY_SCENARIOS) >= 17
    assert "50_concurrent_users" in CAPACITY_SCENARIOS
    assert validate_metric_tags([{"name": "tasks", "tags": ("status", "cohort")}]) == []
    assert validate_metric_tags([{"name": "bad", "tags": ("user_id",)}]) == ["bad:user_id"]


async def test_planner_mutation_block_rate_is_one_hundred_percent():
    registry = build_business_tool_registry()
    task = TaskEnvelope.from_user_input(
        task_id="mutation-run",
        thread_id="thread",
        user_id="u1",
        tenant_id="t1",
        goal="generate reviewed draft",
        intent=TaskIntent.GENERATE_DRAFT,
        acceptance_criteria=["reviewed artifact"],
    ).model_copy(
        update={
            "selected_article_ids": SlotState.from_value(
                ["article-1"], source=SlotSource.USER, confirmed=True
            ),
            "product_ids": SlotState.from_value(
                ["product-1"], source=SlotSource.USER, confirmed=True
            ),
        }
    )
    outcome = await RulePlannerV1(registry).plan(task)
    report = PlannerMutationHarness(ProductionPlanValidator(registry)).run(
        outcome.plan,
        confirmed_slots={
            "intent",
            "goal",
            "acceptance_criteria",
            "selected_article_ids",
            "product_ids",
        },
    )
    assert report["blocked_rate"] == 1.0


async def test_all_core_60_journeys_are_executed_by_the_harness():
    async def execute(case):
        return JourneyExecution(
            case_id=case.case_id,
            terminal_status=case.expected_terminal,
            actual_clarification=case.requires_clarification,
        )

    report = await JourneyHarnessRunner(execute).run(build_core_60_journeys())
    assert report.total == 60
    assert report.success_rate == 1.0
    assert report.reproductions == ()


def test_migration_shadow_bucketing_freeze_gate_and_rollback():
    assert stable_user_bucket(tenant_id="t1", user_id="u1") == stable_user_bucket(
        tenant_id="t1", user_id="u1"
    )
    controller = MigrationController(stage=MigrationStage.SHADOW)
    shadow = controller.route(tenant_id="t1", user_id="u1")
    assert shadow.path == "agent" and not shadow.write_tools_allowed
    controller.enforce_shadow_write_policy(shadow, side_effect_level="L1")
    try:
        controller.enforce_shadow_write_policy(shadow, side_effect_level="L2")
    except PermissionError:
        pass
    else:
        raise AssertionError("shadow mode must reject write tools")

    assert controller.freeze_run("run-1", {"skill": "v1"}) == {"skill": "v1"}
    assert controller.freeze_run("run-1", {"skill": "v2"}) == {"skill": "v1"}
    gate = PromotionGate(
        g0_passed=True,
        g1_passed=True,
        e2e_success_rate=1.0,
        minimum_e2e_success_rate=0.95,
        security_failures=0,
        duplicate_writes=0,
        unauthorized_actions=0,
        zombie_runs=0,
        rollback_drill_passed=True,
    )
    controller.promote(MigrationStage.PERCENT_10, gate)
    assert controller.stage == MigrationStage.PERCENT_10
    controller.rollback()
    assert controller.route(tenant_id="t1", user_id="u1").path == "legacy"


def test_full_loop_acceptance_report_hard_gate():
    report = FullLoopAcceptanceReport(
        journey_count=60,
        journey_success_rate=1.0,
        planner_mutation_block_rate=1.0,
        security_failures=0,
        recovery_failures=0,
    )
    assert report.passed


async def test_production_shadow_adapter_allows_real_reads_and_denies_writes():
    calls = []

    async def production(contract, args, context):
        calls.append(contract.name)
        return {"unused": True}

    registry = build_business_tool_registry()
    adapter = ReadOnlyProductionBusinessToolAdapter(production)
    await adapter.invoke(registry.get("get_article"), {"article_id": "a1"}, object())
    assert calls == ["get_article"]
    try:
        await adapter.invoke(registry.get("save_draft_version"), {}, object())
    except PermissionError:
        pass
    else:
        raise AssertionError("production shadow must block every business write")
