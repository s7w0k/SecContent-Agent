from __future__ import annotations

import pytest
from agent.business_tools import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
    build_business_tool_registry,
)
from agent.candidate_selection import CandidateSelector
from agent.clarification import ClarificationPolicy
from agent.contracts.task import SlotSource, SlotState, TaskEnvelope, TaskIntent
from agent.conversational_service import AgentTurnInput, ConversationalAgentService
from agent.slot_merger import SlotMerger
from agent.task_state_store import TaskStateConflictError
from agent.task_understanding import TaskEnvelopePatch, TaskUnderstandingService


@pytest.mark.asyncio
async def test_deterministic_understanding_extracts_intent_product_length_and_date():
    result = await TaskUnderstandingService().understand(
        "请搜索最近7天 AI-BOM 新闻，控制在1200字"
    )
    assert result.patch.intent == TaskIntent.SEARCH_AND_RANK
    assert result.patch.product_ids == ["ai-bom"]
    assert result.patch.length == 1200
    assert result.patch.constraints[0].startswith("published_from=")
    assert "news_query" in result.patch.explicit_slots


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮我生成一篇PR稿", TaskIntent.GENERATE_DRAFT),
        ("帮我写一篇PR稿", TaskIntent.GENERATE_DRAFT),
        ("写一篇关于AI安全的报道", TaskIntent.GENERATE_DRAFT),
        ("帮我重写这篇稿", TaskIntent.REVISE),
        ("保存这篇稿", TaskIntent.SAVE),
        ("搜索AI新闻并写一篇PR", TaskIntent.SEARCH_AND_DRAFT),
        ("搜索智能体安全新闻", TaskIntent.SEARCH_AND_RANK),
    ],
)
async def test_deterministic_understanding_draft_intent_variants(text, expected):
    result = await TaskUnderstandingService().understand(text)
    assert result.patch.intent == expected, f"{text!r} -> {result.patch.intent}"


@pytest.mark.asyncio
async def test_model_cannot_emit_identity_or_authorization_fields():
    async def invalid_model(_text):
        return {"intent": "save", "user_id": "attacker", "approval": "approved"}

    result = await TaskUnderstandingService(model_parser=invalid_model).understand("保存这篇稿件")
    assert result.model_fallback
    assert result.patch.intent == TaskIntent.SAVE
    assert result.warnings


def _envelope() -> TaskEnvelope:
    envelope = TaskEnvelope.from_user_input(
        task_id="task-1",
        thread_id="thread-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="写一篇稿件",
        intent=TaskIntent.GENERATE_DRAFT,
        turn_id="turn-1",
    )
    return envelope.model_copy(
        update={
            "selected_article_ids": SlotState.from_value(
                ["article-1"], source=SlotSource.USER, turn_id="turn-1"
            ),
            "product_ids": SlotState.from_value(
                ["agent-security"], source=SlotSource.USER, turn_id="turn-1"
            ),
        }
    )


def test_user_product_correction_invalidates_only_downstream_completed_steps():
    result = SlotMerger().merge(
        _envelope(),
        TaskEnvelopePatch(
            product_ids=["ai-bom"], explicit_slots=frozenset({"product_ids"})
        ),
        turn_id="turn-2",
        completed_steps={"search_news", "score_article", "generate_draft"},
    )
    assert result.envelope.product_ids.value == ["ai-bom"]
    assert result.changed_slots == ["product_ids"]
    assert result.invalidated_steps == ["generate_draft", "score_article"]


def test_tool_evidence_cannot_override_confirmed_user_product():
    result = SlotMerger().merge(
        _envelope(),
        TaskEnvelopePatch(product_ids=["ai-bom"]),
        turn_id="tool-1",
        source=SlotSource.TOOL,
    )
    assert result.envelope.product_ids.value == ["agent-security"]
    assert result.envelope.product_ids.status.value == "conflicted"


def test_clarification_is_prioritized_bounded_and_not_repeated():
    envelope = TaskEnvelope.from_user_input(
        task_id="t",
        thread_id="th",
        user_id="u",
        tenant_id="tenant",
        goal="改稿",
        intent=TaskIntent.REVISE,
    )
    policy = ClarificationPolicy()
    first = policy.decide(envelope)
    assert 1 <= len(first.questions) <= 3
    assert first.questions[0].slot == "selected_article_ids"
    second = policy.decide(envelope, asked_slots={"selected_article_ids"})
    assert "selected_article_ids" not in [q.slot for q in second.questions]
    assert not second.can_proceed
    assert second.skipped_previously_asked == ["selected_article_ids"]


def test_candidate_selection_handles_zero_one_many_and_stale():
    selector = CandidateSelector()
    one = {
        "article_id": "a-1",
        "title": "AI 安全新规",
        "source": "source-a",
        "summary": "summary",
        "score": 0.9,
    }
    two = {**one, "article_id": "a-2", "title": "AI 安全产品更新"}
    assert selector.select([]).outcome == "no_results"
    assert selector.select([one]).outcome == "auto_selected"
    assert selector.select([one, two]).outcome == "needs_selection"
    assert selector.select([one, two], "第二个").selected.article_id == "a-2"
    assert selector.select([one], stale_article_ids={"a-1"}).outcome == "stale"


@pytest.mark.asyncio
async def test_turn_is_persisted_once_and_duplicate_turn_does_not_execute_again():
    registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        registry, {BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter()}
    )
    service = ConversationalAgentService(tool_executor=executor)
    body = AgentTurnInput(
        content="搜索 AI-BOM 新闻", turn_id="turn-fixed", thread_id="thread-fixed"
    )
    first = await service.submit_turn(body, user_id="user-a", tenant_id="tenant-a")
    second = await service.submit_turn(body, user_id="user-a", tenant_id="tenant-a")
    state = await service.task_store.get(
        first.task.task_id, user_id="user-a", tenant_id="tenant-a"
    )
    assert not first.duplicate
    assert second.duplicate
    assert second.run.run_id == first.run.run_id
    manifest = await service.get_manifest(
        first.run.run_id, user_id="user-a", tenant_id="tenant-a"
    )
    assert manifest.tool_registry_version.startswith("2.0:sha256:")
    assert manifest.task_snapshot_hash == first.task.fingerprint()
    assert len(state.turns) == 1
    assert await service.get_run(
        first.run.run_id, user_id="user-a", tenant_id="tenant-b"
    ) is None

    with pytest.raises(
        TaskStateConflictError,
        match="turn_id already exists with different content",
    ):
        await service.submit_turn(
            body.model_copy(update={"content": "搜索不同的新闻"}),
            user_id="user-a",
            tenant_id="tenant-a",
        )


@pytest.mark.asyncio
async def test_unanswered_clarification_stays_blocked_without_repeating_question():
    service = ConversationalAgentService()
    first = await service.submit_turn(
        AgentTurnInput(content="帮我修改这篇稿", turn_id="turn-1", thread_id="thread-blocked"),
        user_id="u",
        tenant_id="tenant",
    )
    assert first.run.status == "waiting_user"
    assert first.run.questions[0]["slot"] == "selected_article_ids"
    second = await service.submit_turn(
        AgentTurnInput(
            content="谢谢",
            turn_id="turn-2",
            task_id=first.task.task_id,
        ),
        user_id="u",
        tenant_id="tenant",
    )
    assert second.run.status == "waiting_user"
    assert second.run.questions == []
    events = await service.events(
        second.run.run_id, user_id="u", tenant_id="tenant"
    )
    assert events[-1].event_type == "clarification.waiting"


@pytest.mark.asyncio
async def test_candidate_reply_confirms_selected_article_slot():
    registry = build_business_tool_registry()
    candidates = [
        {"article_id": "a-1", "title": "新闻一", "score": 0.9},
        {"article_id": "a-2", "title": "新闻二", "score": 0.88},
    ]
    executor = BusinessToolExecutor(
        registry,
        {
            "fake": FakeBusinessToolAdapter(
                {
                    "search_news": {
                        "query": "新闻",
                        "items": candidates,
                        "total": 2,
                        "replay_ref": "recording-1",
                    }
                }
            )
        },
    )
    service = ConversationalAgentService(tool_executor=executor)
    first = await service.submit_turn(
        AgentTurnInput(content="搜索 AI 新闻", turn_id="turn-1", thread_id="thread-1"),
        user_id="u",
        tenant_id="tenant",
    )
    assert first.run.status == "waiting_user"
    second = await service.submit_turn(
        AgentTurnInput(
            content="第二个",
            turn_id="turn-2",
            task_id=first.task.task_id,
            thread_id="thread-1",
        ),
        user_id="u",
        tenant_id="tenant",
    )
    assert second.task.selected_article_ids.value == ["a-2"]
    assert second.task.selected_article_ids.status.value == "confirmed"


@pytest.mark.asyncio
async def test_generate_draft_clarifies_then_matches_local_candidates():
    registry = build_business_tool_registry()
    candidates = [
        {"article_id": "a-1", "title": "AI 安全新规", "source": "src", "score": 0.9},
        {"article_id": "a-2", "title": "AI 安全产品更新", "source": "src", "score": 0.88},
    ]
    executor = BusinessToolExecutor(
        registry,
        {
            "fake": FakeBusinessToolAdapter(
                {"list_articles": {"items": candidates, "total": 2, "replay_ref": "r"}}
            )
        },
    )
    service = ConversationalAgentService(tool_executor=executor)
    # turn1：信息不足 → 澄清类别与产品（带默认选项）
    first = await service.submit_turn(
        AgentTurnInput(content="帮我生成一篇PR稿", turn_id="turn-1", thread_id="thread-g"),
        user_id="u",
        tenant_id="tenant",
    )
    assert first.run.intent == "generate_draft"
    assert first.run.status == "waiting_user"
    slots = {question["slot"] for question in first.run.questions}
    assert {"category", "product_ids"}.issubset(slots)
    category_q = next(q for q in first.run.questions if q["slot"] == "category")
    assert "AI技术重大进展" in category_q["options"]
    assert category_q["default"] == "AI技术重大进展"
    # turn2：选择类别+产品 → 本地库匹配 top5 候选
    second = await service.submit_turn(
        AgentTurnInput(
            content="AI技术重大进展，智能体安全",
            turn_id="turn-2",
            task_id=first.task.task_id,
            thread_id="thread-g",
        ),
        user_id="u",
        tenant_id="tenant",
    )
    assert second.run.status == "waiting_user"
    assert len(second.run.result["items"]) == 2
    assert second.run.result["candidate_source"] == "local"
    # turn3：选择候选 → 进入写稿
    third = await service.submit_turn(
        AgentTurnInput(content="第一个", turn_id="turn-3", task_id=first.task.task_id, thread_id="thread-g"),
        user_id="u",
        tenant_id="tenant",
    )
    assert third.run.status == "completed"
    assert third.task.selected_article_ids.value == ["a-1"]


@pytest.mark.asyncio
async def test_generate_draft_defaults_when_user_delegates_and_dedupes_assumptions():
    registry = build_business_tool_registry()
    candidates = [{"article_id": "a-1", "title": "默认候选", "source": "src", "score": 0.9}]
    executor = BusinessToolExecutor(
        registry,
        {
            "fake": FakeBusinessToolAdapter(
                {"list_articles": {"items": candidates, "total": 1, "replay_ref": "r"}}
            )
        },
    )
    service = ConversationalAgentService(tool_executor=executor)
    first = await service.submit_turn(
        AgentTurnInput(content="帮我生成一篇PR稿，你决定", turn_id="turn-1", thread_id="thread-g2"),
        user_id="u",
        tenant_id="tenant",
    )
    assert first.run.status == "waiting_user"
    assert len(first.run.result["items"]) == 1
    assert first.run.result["category"] == "AI技术重大进展"
    # 同一 thread 再次带“你决定”提交，假设不重复累积。
    second = await service.submit_turn(
        AgentTurnInput(
            content="帮我写一篇PR稿，你决定",
            turn_id="turn-2",
            task_id=first.task.task_id,
            thread_id="thread-g2",
        ),
        user_id="u",
        tenant_id="tenant",
    )
    assert len(second.run.assumptions) == 1
    assert second.run.assumptions[0].startswith("用户授权系统")


class _LimitAwareFakeAdapter:
    """模拟 list_articles 按 limit 截断 + crawl_news 返回结果的测试适配器。"""

    kind = "fake"

    def __init__(self, items: list[dict]):
        self.items = items

    async def invoke(self, contract, args, context):
        if contract.name == "list_articles":
            limit = args.get("limit", 5)
            return {"items": self.items[:limit], "total": len(self.items), "replay_ref": "r"}
        if contract.name == "crawl_news":
            return {
                "task_ref": "crawl-1",
                "status": "completed",
                "added": 3,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "articles": [],
                "errors": [],
            }
        raise KeyError(contract.name)


@pytest.mark.asyncio
async def test_generate_draft_refinement_more_and_crawl():
    registry = build_business_tool_registry()
    items = [{"article_id": f"a-{i}", "title": f"新闻 {i}", "source": "src"} for i in range(20)]
    executor = BusinessToolExecutor(registry, {"fake": _LimitAwareFakeAdapter(items)})
    service = ConversationalAgentService(tool_executor=executor)
    # turn1：默认值 → 本地库 top5
    first = await service.submit_turn(
        AgentTurnInput(content="帮我生成一篇PR稿，你决定", turn_id="t1", thread_id="thread-ref"),
        user_id="u",
        tenant_id="tenant",
    )
    assert first.run.status == "waiting_user"
    assert len(first.run.result["items"]) == 5
    assert first.run.result["candidate_source"] == "local"
    # turn2：继续匹配库内其他文章 → 更多候选
    more = await service.submit_turn(
        AgentTurnInput(
            content="继续匹配更多新闻",
            turn_id="t2",
            task_id=first.task.task_id,
            thread_id="thread-ref",
        ),
        user_id="u",
        tenant_id="tenant",
    )
    assert more.run.status == "waiting_user"
    assert len(more.run.result["items"]) == 20
    assert more.run.result["candidate_source"] == "more"
    # turn3：触发爬虫爬取最新 → crawl 后重新匹配
    crawled = await service.submit_turn(
        AgentTurnInput(
            content="爬取最新新闻",
            turn_id="t3",
            task_id=first.task.task_id,
            thread_id="thread-ref",
        ),
        user_id="u",
        tenant_id="tenant",
    )
    assert crawled.run.status == "waiting_user"
    assert crawled.run.result["candidate_source"] == "crawl"
    events = await service.events(crawled.run.run_id, user_id="u", tenant_id="tenant")
    assert any(event.event_type == "tool_started" and event.payload.get("tool") == "crawl_news" for event in events)
