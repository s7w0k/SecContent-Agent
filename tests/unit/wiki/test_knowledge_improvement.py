"""Phase 25 安全 Self-Evolution（§28）单元测试：运行时只产生事件，用户输入永不直接入 Wiki。"""

from __future__ import annotations

import pytest
from agent.wiki.knowledge_improvement import (
    IMPROVEMENT_TYPES,
    ImprovementJournal,
    KnowledgeImprovementEvent,
    emit_event,
    to_maintainer_proposal,
)


def test_improvement_types_coverage():
    for t in (
        "MISSING_PAGE",
        "MISSING_ALIAS",
        "BROKEN_LINK_OBSERVED",
        "REPEATED_NAVIGATION_PATH",
        "LOW_COVERAGE_TOPIC",
        "POTENTIAL_SYNTHESIS",
        "STALE_KNOWLEDGE",
    ):
        assert t in IMPROVEMENT_TYPES


def test_emit_record_and_dedupe():
    j = ImprovementJournal()
    first = emit_event(
        event_type="MISSING_PAGE",
        subject="aiscm",
        detail="缺少 会话管理 页面",
        trusted=True,
        journal=j,
    )
    assert first is not None
    assert j.count() == 1
    # 相同主体+详情（空白差异归一化）→ 去重
    dup = emit_event(
        event_type="MISSING_PAGE",
        subject="aiscm",
        detail="缺少  会话管理 页面",
        trusted=True,
        journal=j,
    )
    assert dup is None
    assert j.count() == 1


def test_emit_unknown_type_rejected():
    with pytest.raises(ValueError):
        emit_event(
            event_type="USER_INJECTED_FACT",
            subject="aiscm",
            trusted=False,
        )


def test_trusted_event_becomes_maintainer_proposal():
    j = ImprovementJournal()
    event = emit_event(
        event_type="POTENTIAL_SYNTHESIS",
        subject="aiscm",
        detail="支持 OIDC 与 JIT 联合",
        trusted=True,
        source_hint="1-产品/overview.md",
        journal=j,
    )
    assert event is not None
    proposal = to_maintainer_proposal(event)
    assert proposal is not None
    assert proposal["trusted"] is True
    assert proposal["stage"] == "proposal_for_maintainer"


def test_untrusted_user_input_never_promoted():
    # 用户语句 cannot 直接进入 Production Wiki Fact（防知识投毒）
    j = ImprovementJournal()
    event = emit_event(
        event_type="POTENTIAL_SYNTHESIS",
        subject="aiscm",
        detail="（用户说）产品支持外星协议",
        trusted=False,
        source_hint="chat:user-message",
        journal=j,
    )
    assert event is not None
    assert to_maintainer_proposal(event) is None


def test_untrusted_can_be_gated_proposal_with_explicit_flag():
    event = KnowledgeImprovementEvent(
        event_id="e1",
        type="POTENTIAL_SYNTHESIS",
        subject="aiscm",
        detail="人工核验候选",
        trusted=False,
        source_hint="chat:user-message",
    )
    # 显式 permit_untrusted（人工维护）才放行，属显式 Feature Flag 而不是隐式分支
    assert to_maintainer_proposal(event, permit_untrusted=True) is not None
    assert to_maintainer_proposal(event) is None


def test_journal_lists_in_order():
    j = ImprovementJournal()
    emit_event(event_type="MISSING_ALIAS", subject="a", trusted=True, journal=j)
    emit_event(event_type="STALE_KNOWLEDGE", subject="b", trusted=True, journal=j)
    assert len(j.all()) == 2
    assert j.all()[0].type == "MISSING_ALIAS"
