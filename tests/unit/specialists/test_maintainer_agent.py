"""MaintainerAgent 硬门禁测试（计划 §69 / §44 / §41 / §42）。

覆盖：
  - untrusted（用户输入）不可发布 → NEEDS_SOURCE
  - Maintainer 只写 Staging，Publish 需过门禁
  - Regression 失败阻断 Publish → REJECTED
  - 全门禁通过才 Publish（Production 唯一出口计数为 1）

运行（仓库根目录）:
    python -m pytest tests/unit/specialists/test_maintainer_agent.py --basetemp ./.pytest-tmp-x -q --no-header
"""

from __future__ import annotations

from agent.specialists.maintainer_agent import MaintainerAgent
from agent.wiki.knowledge_improvement import ImprovementJournal, KnowledgeImprovementEvent


def _event(
    *, trusted: bool, subject: str = "agent-security", source_hint: str = "src-1"
) -> KnowledgeImprovementEvent:
    return KnowledgeImprovementEvent(
        event_id="ev-1",
        type="NEW_FACT",
        subject=subject,
        detail="agent-security 支持实时威胁检测",
        trusted=trusted,
        source_hint=source_hint,
    )


# ═══════════════════════════════════════════════════════════════
# 1. untrusted 用户事实不可发布
# ═══════════════════════════════════════════════════════════════


async def test_untrusted_event_cannot_publish():
    published_refs: list[str] = []
    agent = MaintainerAgent(
        publisher=lambda case: published_refs.append(str(case.case_id)) or {"published": True},
    )
    case = await agent.process(_event(trusted=False))
    assert case.status == "NEEDS_SOURCE"
    assert "untrusted" in case.reason
    # Production 出口绝不被触发
    assert agent.production_write_attempts == 0
    assert published_refs == []
    # 即便恶意发布器被调用也不应成功（此处不被调用）
    assert agent.published_cases == []


async def test_untrusted_only_proposes_source_discovery():
    """untrusted 事件只触发 Source Discovery / Proposal，不进入 Staging。"""
    agent = MaintainerAgent(source_verifier=lambda e: [e.source_hint])
    case = await agent.process(_event(trusted=False))
    assert case.status == "NEEDS_SOURCE"
    assert not case.source_refs
    assert "source_discovery" in case.proposed_actions
    assert "compile_staging" not in case.proposed_actions


# ═══════════════════════════════════════════════════════════════
# 2. Maintainer 只写 Staging，Publish 需过门禁
# ═══════════════════════════════════════════════════════════════


async def test_maintainer_only_writes_staging():
    published: list[str] = []
    agent = MaintainerAgent(
        source_verifier=lambda e: [e.source_hint],
        approval=lambda case: False,  # 卡在 Approval Gate
        publisher=lambda case: published.append(case.case_id) or {"published": True},
    )
    case = await agent.process(_event(trusted=True))
    # 事件已核实来源、进入 STAGED/EVALUATING，但未获审批 → WAITING_APPROVAL
    assert case.status == "WAITING_APPROVAL"
    assert case.source_refs == ["src-1"]
    # Production 没有任何写入
    assert agent.production_write_attempts == 0
    assert published == []


async def test_publish_requires_gate_passed():
    published: list[str] = []
    agent = MaintainerAgent(
        source_verifier=lambda e: [e.source_hint],
        approval=lambda case: True,
        publisher=lambda case: (
            published.append(case.case_id) or {"published": True, "ref": f"wiki:{case.case_id}"}
        ),
    )
    case = await agent.process(_event(trusted=True))
    assert case.status == "PUBLISHED"
    assert agent.production_write_attempts == 1  # 唯一 Production 出口仅一次
    assert case.case_id in published


# ═══════════════════════════════════════════════════════════════
# 3. Regression 阻断 Publish
# ═══════════════════════════════════════════════════════════════


async def test_regression_blocks_publish():
    published: list[str] = []
    agent = MaintainerAgent(
        source_verifier=lambda e: [e.source_hint],
        evaluator=lambda case: {"ok": False, "regressions": ["golden-001"]},
        approval=lambda case: True,
        publisher=lambda case: published.append(case.case_id) or {"published": True},
    )
    case = await agent.process(_event(trusted=True))
    assert case.status == "REJECTED"
    assert "regression" in case.reason
    assert agent.production_write_attempts == 0
    assert published == []


# ═══════════════════════════════════════════════════════════════
# 4. 门禁组合：全通过才发布；缺来源则 NEEDS_SOURCE
# ═══════════════════════════════════════════════════════════════


async def test_trusted_but_missing_source_not_published():
    agent = MaintainerAgent(source_verifier=lambda e: [])  # 无可核实来源
    case = await agent.process(_event(trusted=True))
    assert case.status == "NEEDS_SOURCE"
    assert agent.production_write_attempts == 0


async def test_dedup_via_journal_blocks_reprocessing():
    journal = ImprovementJournal()
    agent = MaintainerAgent(
        journal=journal,
        source_verifier=lambda e: [e.source_hint],
        publisher=lambda case: {"published": True, "ref": "wiki:new-published"},
    )
    first = await agent.process(_event(trusted=True))
    second = await agent.process(_event(trusted=True))
    # 第一条走完正常流程（可信且默认门禁全过 → PUBLISHED）
    assert first.status == "PUBLISHED"
    # 去重后不再重复处理
    assert second.status == "REJECTED"
    assert "duplicate" in second.reason
