"""ChatAgent 上下文增强（token 预算 / 记忆 / Skill / 压缩 / 自进化）单元测试。

覆盖本次接入的四部分能力：
  T1 token 预算与上下文窗口推导（chat_context.build_chat_context）
  T2 长期记忆注入（build_chat_context 注入 memory + 溢出丢弃）
  T4 Skill 指令编排（skill_instructions 注入）
  T3 历史 token 压缩（AgentEngine._build_history / _trim）
  T5 自进化闭环落库（ChatAgentService._record_generation_feedback）

这些用例全部为纯逻辑/伪对象，不依赖真实 MongoDB 或 LLM 调用，可离线运行。
"""

from __future__ import annotations

from agent.agent_engine import AgentEngine
from agent.chat_context import build_chat_context, derive_input_budget
from agent.context_manager import estimate_tokens
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

BASE_PROMPT = "你是 PR 智能体，负责撰写公关稿件。\n请按流程使用提供的工具。"


# ═══════════════════════════════════════════════════════════════
# T1 token 预算 & 上下文窗口推导
# ═══════════════════════════════════════════════════════════════


def test_derive_budget_from_window():
    """未显式配置上限时，从模型窗口推导输入预算（去掉输出/历史预留与基准提示词）。"""
    budget = derive_input_budget(
        model_id="deepseek-chat",
        max_input_tokens=0,
        base_tokens=estimate_tokens(BASE_PROMPT),
    )
    # deepseek-chat 窗口 64000 - 4000 输出 - 4000 历史 - base>0
    assert 50000 <= budget <= 64000


def test_derive_budget_explicit_override():
    """显式配置上限时直接采用（不叠加窗口推导）。"""
    budget = derive_input_budget(
        model_id="deepseek-chat",
        max_input_tokens=12345,
        base_tokens=estimate_tokens(BASE_PROMPT),
    )
    assert budget == 12345


def test_build_context_base_always_present():
    """基准提示词一定注入，且整体 system_prompt 以 base 开头。"""
    ctx = build_chat_context(
        base_system_prompt=BASE_PROMPT,
        model_id="deepseek-chat",
        max_input_tokens=0,
    )
    assert ctx.system_prompt.startswith(BASE_PROMPT)
    assert ctx.base_tokens > 0
    assert ctx.used_tokens == ctx.base_tokens


# ═══════════════════════════════════════════════════════════════
# T2 长期记忆注入 & 预算溢出丢弃
# ═══════════════════════════════════════════════════════════════


def test_memory_injected_within_budget():
    """记忆在预算内时被注入，并体现在 system_prompt。"""
    memory = "用户偏好简洁、口语化标题，署名统一用'安全研究院'。"
    ctx = build_chat_context(
        base_system_prompt=BASE_PROMPT,
        model_id="deepseek-chat",
        max_input_tokens=200000,  # 足够大，保证记忆不丢
        memory_text=memory,
    )
    assert "用户长期偏好" in ctx.system_prompt
    assert "安全研究院" in ctx.system_prompt
    assert ctx.memory_tokens > 0
    assert ctx.memory_dropped == 0


def test_memory_dropped_on_overflow():
    """记忆跨出预算时整块丢弃，不影响 base 与 skill 指令。"""
    tiny = "你是智能体。"  # base 极小，预算调到几乎为零触发记忆丢弃
    # 足够大：36000 字符 ≈ 9000 token，远超 effective_budget(4096 兜底)
    big_memory = "偏好：" + ("很长的用户记忆内容。" * 4000)
    ctx = build_chat_context(
        base_system_prompt=tiny,
        model_id="deepseek-chat",
        max_input_tokens=1,  # 极小预算 -> effective_budget 被 MIN 兜底但 memory 仍远大于其可装配余量
        memory_text=big_memory,
    )
    # 预算经 MIN_INPUT_BUDGET 兜底后，仍应丢弃这块超大的记忆
    assert ctx.memory_tokens == 0
    assert ctx.memory_dropped > 0
    assert "用户长期偏好" not in ctx.system_prompt


def test_memory_empty_is_noop():
    """无记忆数据时正常返回，不注入任何空白块。"""
    ctx = build_chat_context(
        base_system_prompt=BASE_PROMPT,
        max_input_tokens=200000,
        memory_text="",
    )
    assert ctx.memory_tokens == 0
    assert ctx.memory_dropped == 0
    assert "用户长期偏好" not in ctx.system_prompt


# ═══════════════════════════════════════════════════════════════
# T4 Skill 指令编排与 token 度量
# ═══════════════════════════════════════════════════════════════


def test_skill_instructions_injected_and_counted():
    """命中 Skill 指令时注入其正文并记录 skill_names / skill_tokens。"""
    skills = [
        ("scoring-knowledge", "评分准则：PR 价值从原创性、时效性、影响面三维度打分。"),
        ("draft-writing", "写作要求：结论先行，段首句可扫读。"),
    ]
    ctx = build_chat_context(
        base_system_prompt=BASE_PROMPT,
        max_input_tokens=200000,
        skill_instructions=skills,
    )
    assert ctx.skill_names == ["scoring-knowledge", "draft-writing"]
    assert ctx.skill_tokens > 0
    assert "# Skill 指令" in ctx.system_prompt
    assert "三维度打分" in ctx.system_prompt


def test_skill_empty_ok():
    """无 Skill 命中时不注入 skill 段。"""
    ctx = build_chat_context(base_system_prompt=BASE_PROMPT)
    assert ctx.skill_names == []
    assert ctx.skill_tokens == 0
    assert "# Skill 指令" not in ctx.system_prompt


# ═══════════════════════════════════════════════════════════════
# T3 历史 token 压缩（AgentEngine._build_history / _trim）
# ═══════════════════════════════════════════════════════════════


def _minimal_engine(history_tokens: int = 6000) -> AgentEngine:
    """用最少的字段构造 AgentEngine，仅用于测试 history 压缩（不触 LLM/工具）。"""
    engine = object.__new__(AgentEngine)
    engine.history_tokens = max(1024, int(history_tokens))
    return engine


def test_build_history_bounded_by_tokens():
    """_build_history 把历史压缩进 token 预算，且优先保留最近消息。"""
    engine = _minimal_engine(history_tokens=2000)
    # 造 20 条很长的历史（远超预算）
    history = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"第 {i} 轮内容" + ("填充" * 100),
        }
        for i in range(20)
    ]
    msgs = engine._build_history(history)
    # 第一条被纳入历史的应该是"最新"（即原始第 19 条）
    assert msgs[-1].content.startswith("第 19 轮内容")
    total = sum(len(getattr(m, "content", "")) // 4 for m in msgs)
    assert total <= 2000


def test_build_history_keeps_all_when_small():
    """历史总量小于预算时全量保留（条数上限仍约束）。"""
    engine = _minimal_engine(history_tokens=60000)
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我能帮你什么？"},
    ]
    msgs = engine._build_history(history)
    assert len(msgs) == 2


def test_trim_preserves_tool_pairing():
    """_trim 压缩旧历史时，保留最近用户消息及其后的工具调用/观察，保证配对完整。"""
    engine = _minimal_engine(history_tokens=1500)
    sys = SystemMessage(content="你是PR智能体")
    # 大量旧对话（应被丢弃）
    old = [
        HumanMessage(content="旧对话" * 400),
        AIMessage(content="旧答复" * 400),
    ] * 8
    recent_user = HumanMessage(content="请对这篇文章重新评分")
    tool_msg = AIMessage(content="", tool_calls=[{"id": "t1", "name": "score_article", "args": {}}])
    obs = ToolMessage(content="得分 88", tool_call_id="t1")
    messages = [sys, *old, recent_user, tool_msg, obs]

    trimmed = engine._trim(messages)
    # system 保留
    assert isinstance(trimmed[0], SystemMessage)
    # 工具-观察配对必须完整保留（都在最近用户消息之后）
    text = " | ".join(getattr(m, "content", "") or "" for m in trimmed)
    assert "请对这篇文章重新评分" in text
    assert "得分 88" in text


# ═══════════════════════════════════════════════════════════════
# T5 自进化闭环落库（_record_generation_feedback）
# ═══════════════════════════════════════════════════════════════


def test_generation_feedback_writes_run_and_memory_event():
    """进化开关开启时，把完成的 chat run 写入 generation_runs 并触发记忆事件。"""
    from agent.chat_agent_service import ChatAgentService

    captured = {}

    class FakeCol:
        def __init__(self, name):
            self.name = name

        async def insert_one(self, doc):
            captured[self.name] = doc

    class FakeDB:
        def __getitem__(self, name):
            return FakeCol(name)

    import agent.memory_event_service as mes

    async def fake_create_memory_event(db, user_id, source_type, **kw):
        captured["memory_event"] = {
            "user_id": user_id,
            "source_type": str(source_type),
            "idempotency_key": kw.get("idempotency_key"),
            "payload": kw.get("payload"),
        }
        return "mevt-x"

    service = object.__new__(ChatAgentService)
    service.evolution_enabled = True
    service.db = FakeDB()
    mes.create_memory_event = fake_create_memory_event

    import asyncio

    asyncio.run(
        service._record_generation_feedback(
            user_id="u1",
            run_id="r1",
            thread_id="t1",
            tools_used=["score_article", "generate_draft"],
            final_ok=True,
            context_telemetry={"input_budget": 50000, "skill_names": ["scoring-knowledge"]},
        )
    )

    assert "generation_runs" in captured
    run = captured["generation_runs"]
    assert run["generation_id"] == "chat-r1"
    assert run["tool_names"] == ["score_article", "generate_draft"]
    assert run["status"] == "completed"
    assert run["context"]["skill_names"] == ["scoring-knowledge"]

    assert "memory_event" in captured
    assert captured["memory_event"]["user_id"] == "u1"
    assert captured["memory_event"]["payload"]["final_ok"] is True


def test_generation_feedback_skipped_when_disabled():
    """进化开关关闭（或 db 为 None）时静默跳过，不抛错。"""
    from agent.chat_agent_service import ChatAgentService

    service = object.__new__(ChatAgentService)
    service.evolution_enabled = False
    service.db = None

    import asyncio

    asyncio.run(
        service._record_generation_feedback(
            user_id="u1",
            run_id="r9",
            thread_id="t9",
            tools_used=[],
            final_ok=True,
            context_telemetry={},
        )
    )
    # 不抛异常即为通过
    assert True
