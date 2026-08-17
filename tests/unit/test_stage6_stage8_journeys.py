from __future__ import annotations

from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
)
from agent.contracts.task import SlotSource, SlotState, TaskEnvelope, TaskIntent
from agent.draft_versions import DraftReviewStatus, DraftVersionError, DraftVersionRepository
from agent.journey_policy import (
    BoundedReviewRepairPolicy,
    JourneyAction,
    StableCandidateRanker,
    decide_low_score,
)
from agent.journey_runtime import ProductionJourneyRunner
from agent.runtime_state import RuntimeStatus


def _task(intent: TaskIntent, **slots) -> TaskEnvelope:
    task = TaskEnvelope.from_user_input(
        task_id="run-journey",
        thread_id="thread-journey",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="complete the requested PR workflow",
        intent=intent,
        acceptance_criteria=["machine validated result"],
    )
    updates = {
        name: SlotState.from_value(value, source=SlotSource.USER, confirmed=True)
        for name, value in slots.items()
    }
    return task.model_copy(update=updates)


def _runner(results=None) -> ProductionJourneyRunner:
    registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        registry,
        adapters={BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter(results)},
    )
    return ProductionJourneyRunner(executor, adapter=BusinessToolAdapterKind.FAKE)


async def test_known_article_one_turn_produces_reviewed_draft():
    task = _task(
        TaskIntent.GENERATE_DRAFT,
        selected_article_ids=["article-123"],
        product_ids=["agent-security"],
    )
    state, result = await _runner().run(task)
    assert state.status == RuntimeStatus.COMPLETED
    assert result.artifact["artifact_id"]
    assert result.review["passed"] is True
    assert result.score["user_requested_draft"] is True
    assert set(result.completed_steps) == {"article", "classify", "products", "score", "draft", "review"}


async def test_search_zero_and_many_candidates_pause_with_machine_reason():
    task = _task(TaskIntent.SEARCH_AND_DRAFT, news_query="agent security")
    empty_state, empty = await _runner().run(task)
    assert empty_state.status == RuntimeStatus.WAITING_USER
    assert empty_state.reason_code == "crawl_suggested"
    assert empty.pending_questions

    search_result = {
        "query": "agent security",
        "items": [
            {"article_id": "a-1", "title": "First", "score": 0.9},
            {"article_id": "a-2", "title": "Second", "score": 0.89},
        ],
        "total": 2,
        "replay_ref": "recording-1",
    }
    many_state, _many = await _runner({"search_news": search_result}).run(task)
    assert many_state.status == RuntimeStatus.WAITING_USER
    assert many_state.reason_code == "article_selection_required"
    assert len(many_state.normalized_observations["discover"]["data"]["items"]) == 2


async def test_product_ambiguity_pauses_after_classification_without_writing_draft():
    ambiguous = {
        "article": {"article_id": "article-123"},
        "candidates": [
            {"product_id": "p1", "name": "P1", "confidence": 0.8},
            {"product_id": "p2", "name": "P2", "confidence": 0.79},
        ],
        "outcome": "ambiguous",
        "catalog_hash": "sha256:catalog",
    }
    task = _task(TaskIntent.GENERATE_DRAFT, selected_article_ids=["article-123"])
    state, result = await _runner({"match_products": ambiguous}).run(task)
    assert state.status == RuntimeStatus.WAITING_USER
    assert state.reason_code == "product_ambiguity"
    assert state.completed_steps == ["article", "classify", "products"]
    assert "draft" not in state.normalized_observations
    assert result.products["outcome"] == "ambiguous"


def test_low_score_branch_respects_explicit_user_goal():
    forced = decide_low_score(
        intent=TaskIntent.GENERATE_DRAFT.value,
        worth_writing=False,
        user_requested_draft=True,
    )
    curated = decide_low_score(
        intent=TaskIntent.CURATE_NEWS.value,
        worth_writing=False,
        user_requested_draft=False,
    )
    assert forced.action == JourneyAction.CONTINUE
    assert curated.action == JourneyAction.STOP_EXPLAINED


def test_top_n_ranking_is_bounded_stable_and_evidenced():
    candidates = [
        {"article_id": "b", "total_score": 90, "product_ids": ["p1"], "evidence": ["e2"]},
        {"article_id": "a", "total_score": 90, "product_ids": ["p2"], "evidence": ["e1"]},
        {"article_id": "c", "total_score": 70, "product_ids": ["p3"], "evidence": ["e3"]},
    ]
    ranked = StableCandidateRanker().rank(candidates, top_n=2)
    assert [item.article_id for item in ranked] == ["a", "b"]
    assert all(item.product_ids and item.evidence for item in ranked)


async def test_draft_version_dag_branches_compare_and_rolls_back_pointer_without_delete():
    repo = DraftVersionRepository()
    root = await repo.create(
        tenant_id="t1",
        user_id="u1",
        article_id="a1",
        product_ids=["p1"],
        content="# Title\nOriginal",
        created_by="agent",
        review_status=DraftReviewStatus.REVIEW_PASSED,
    )
    branch_a = await repo.create(
        tenant_id="t1", user_id="u1", article_id="a1",
        content="# Short\nOriginal", created_by="u1",
        parent_artifact_id=root.artifact_id, instruction="shorter title",
    )
    branch_b = await repo.create(
        tenant_id="t1", user_id="u1", article_id="a1",
        content="# Title\nExpanded body", created_by="u1",
        parent_artifact_id=root.artifact_id, instruction="expand body",
    )
    assert branch_a.parent_artifact_id == branch_b.parent_artifact_id == root.artifact_id
    assert len(await repo.lineage(branch_a.artifact_id, tenant_id="t1", user_id="u1")) == 2
    comparison = await repo.compare(branch_a.artifact_id, branch_b.artifact_id, tenant_id="t1", user_id="u1")
    assert comparison.added_lines and comparison.removed_lines
    pointer = await repo.set_primary(
        branch_b.artifact_id, tenant_id="t1", user_id="u1", confirmed=True,
        idempotency_key="primary-branch-b", expected_generation=0,
    )
    rolled_back = await repo.rollback_primary(
        root.artifact_id, tenant_id="t1", user_id="u1",
        idempotency_key="rollback-root", expected_generation=pointer.generation,
    )
    assert rolled_back.artifact_id == root.artifact_id
    assert len(await repo.list_versions(root.artifact_id, tenant_id="t1", user_id="u1")) == 3
    try:
        await repo.set_primary(
            branch_a.artifact_id, tenant_id="t1", user_id="u1", confirmed=False,
            idempotency_key="not-confirmed",
        )
    except DraftVersionError as exc:
        assert "confirmation" in str(exc)
    else:
        raise AssertionError("unconfirmed primary update must fail")


def test_review_auto_repair_is_bounded_and_high_risk_never_ignored():
    policy = BoundedReviewRepairPolicy(max_auto_repairs=1)
    warning = [{"severity": "warning"}]
    assert policy.decide(warning, repair_count=0).action == "auto_repair"
    assert policy.decide(warning, repair_count=1).status == "review_failed"
    severe = policy.decide([{"severity": "critical"}], repair_count=0)
    assert severe.action == "ask_user"
    assert severe.status == "needs_user_review"
