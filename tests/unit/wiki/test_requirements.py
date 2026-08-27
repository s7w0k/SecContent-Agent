"""Phase 7 / PR-13：Evidence Requirement + Navigator V2（GOAL A，§4/§10）。"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef, WikiRelation
from agent.wiki.evidence import EvidenceItem
from agent.wiki.navigation_policy import NavigationState
from agent.wiki.navigator import ALLOWED_ACTIONS, NavigationCandidate, WikiNavigator
from agent.wiki.requirement_evaluator import RequirementEvaluator
from agent.wiki.requirements import RequirementTracker, default_requirements
from helpers import make_page


async def _single_product_wiki(store):
    store.write_page(
        make_page(
            "product.agent_identity",
            product_id="agent_identity",
            relations=[
                WikiRelation(
                    relation_type="related_to",
                    target_page_id="product.agent_identity.capability.identity_auth",
                )
            ],
        )
    )
    store.write_page(
        make_page(
            "product.agent_identity.capability.identity_auth",
            product_id="agent_identity",
        )
    )


# ── Requirement Planner（§10.4）───────────────────────────


def test_default_requirements_score():
    reqs = default_requirements("score")
    assert [r.requirement_id for r in reqs] == ["R1", "R2", "R3"]
    assert all(0.0 <= r.weight <= 1.0 for r in reqs)


def _verified_evidence(requirement_ids: list[str], *, evidence_id: str = "ev-a") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        fact="支持智能体身份认证",
        page_id="product.agent_identity.capability.identity_auth",
        page_title="身份认证",
        requirement_ids=requirement_ids,
        source_refs=[
            SourceRef(
                source_id="s1",
                relative_path="1-产品/overview.md",
                content_hash="h1",
            )
        ],
        relevance=0.9,
        confidence=0.9,
        reason_code="VERIFIED",
        relation_to_task="potential_match",
    )


def test_requirement_tracker_alone_does_not_drive_coverage():
    """GOAL A：RequirementTracker 不再由 page_type 直接判定 MET，单独使用 coverage 恒为 0。"""
    tracker = RequirementTracker(default_requirements("score"))
    assert tracker.coverage() == 0.0
    assert tracker.missing == ["R1", "R2", "R3"]
    # 没有任何"打开页面即 MET"路径；MET 只能来自 RequirementEvaluator（Verified Evidence）
    assert tracker.met == []


def test_coverage_accumulates_from_verified_evidence():
    """GOAL A：只有经过验证的 Evidence 才能满足 Requirement 并累计 Coverage。"""
    reqs = default_requirements("score")
    evaluator = RequirementEvaluator(confidence_threshold=0.8, relevance_threshold=0.5)

    ev = _verified_evidence(["R1"])
    evaluation = evaluator.evaluate(reqs, [ev])
    assert "R1" in evaluation.met_requirements
    cov = evaluation.coverage
    assert 0.0 < cov < 1.0  # R1(weight 0.5) 已满足，累计部分权重
    assert "R2" in evaluation.missing_requirements
    assert evaluation.all_required_met is True  # R1 是唯一 required 且已 MET

    # 补充 R2 证据后 coverage 继续累计
    ev_r2 = _verified_evidence(["R2"], evidence_id="ev-b")
    evaluation2 = evaluator.evaluate(reqs, [ev, ev_r2])
    assert "R2" in evaluation2.met_requirements
    assert evaluation2.coverage > cov


def test_sufficient_requires_verified_state():
    """GOAL A：`sufficient` 由 Verified 快照硬校验，而非 page_type 声称。"""
    reqs = default_requirements("score")
    evaluator = RequirementEvaluator(confidence_threshold=0.8, relevance_threshold=0.5)
    # 只有 R1 满足：all_required_met True，但 coverage(0.5) < min_coverage(0.7) → 不 sufficient
    single = evaluator.evaluate(reqs, [_verified_evidence(["R1"])])
    assert single.is_sufficient(min_coverage=0.7, confidence_threshold=0.8) is False


def test_coverage_full_when_sufficient_evidence():
    """GOAL A：R1+R2+R3 全部有验证证据 → coverage 1.0。"""
    reqs = default_requirements("score")
    evaluator = RequirementEvaluator(confidence_threshold=0.8, relevance_threshold=0.5)
    evidence = [
        _verified_evidence(["R1"], evidence_id="ev-a"),
        _verified_evidence(["R2"], evidence_id="ev-b"),
        _verified_evidence(["R3"], evidence_id="ev-c"),
    ]
    evaluation = evaluator.evaluate(reqs, evidence)
    assert evaluation.coverage == 1.0
    assert evaluation.all_required_met is True
    assert evaluation.is_sufficient(min_coverage=0.7, confidence_threshold=0.8) is True


def test_tracker_unknown_task_falls_back_to_score():
    tracker = RequirementTracker(default_requirements("unknown-task"))
    assert tracker.coverage() == 0.0


# ── NavigationCandidate 真实深度（§10.1）─────────────────


def test_candidate_holds_real_depth():
    c = NavigationCandidate(page_id="p", depth=2, parent_page_id="parent", via_relation="related")
    assert c.depth == 2
    assert c.via_relation == "related"


async def test_navigate_stops_at_requirement_sufficient(store):
    await _single_product_wiki(store)
    nav = WikiNavigator(store)
    outcome = await nav.navigate(
        "身份认证", product_ids=["agent_identity"], task_type="score", max_depth=1
    )
    # product(0) + capability(1) 已满足 R1
    assert outcome.state.evidence_so_far >= 1


async def test_navigate_bounded_and_cycle_safe(store):
    # A→B→A 环 + 自环，不得死循环
    store.write_page(
        make_page(
            "product.agent_identity",
            product_id="agent_identity",
            relations=[
                WikiRelation(relation_type="related_to", target_page_id="product.agent_identity"),
                WikiRelation(relation_type="related_to", target_page_id="product.a.capability.aa"),
            ],
        )
    )
    store.write_page(
        make_page(
            "product.a.capability.aa",
            product_id="agent_identity",
            relations=[
                WikiRelation(relation_type="related_to", target_page_id="product.agent_identity")
            ],
        )
    )
    nav = WikiNavigator(store)
    outcome = await nav.navigate("x", product_ids=["agent_identity"], task_type="score")
    # 每个页最多访问一次
    assert len(outcome.visited) == len(set(outcome.visited))
    assert "product.a.capability.aa" in outcome.visited


# ── LLM Action 白名单 validate_action（§10.5）────────────


def _nav_state(task_type: str = "score", max_pages: int = 6, max_depth: int = 3):
    return NavigationState(
        task_type=task_type,
        query="身份认证",
        product_ids=["agent_identity"],
        max_pages=max_pages,
        max_depth=max_depth,
    )


def test_validate_action_rejects_unknown_action():
    nav = WikiNavigator.__new__(WikiNavigator)  # 不触发 __init__
    ok, reason = nav.validate_action(
        {"action": "DELETE_PAGE", "target": "x"},
        state=_nav_state(),
        frontier=[],
        visited=set(),
    )
    assert not ok
    assert reason.startswith("ACTION_NOT_ALLOWED")


def test_validate_action_rejects_invented_target():
    nav = WikiNavigator.__new__(WikiNavigator)
    frontier = [
        NavigationCandidate(page_id="product.agent_identity.capability.identity_auth", depth=1)
    ]
    ok, _ = nav.validate_action(
        {"action": "OPEN_PAGE", "target": "product.fake.page"},
        state=_nav_state(),
        frontier=frontier,
        visited=set(),
    )
    assert not ok


def test_validate_action_accepts_candidate_target():
    nav = WikiNavigator.__new__(WikiNavigator)
    frontier = [
        NavigationCandidate(page_id="product.agent_identity.capability.identity_auth", depth=1)
    ]
    ok, reason = nav.validate_action(
        {"action": "OPEN_PAGE", "target": "product.agent_identity.capability.identity_auth"},
        state=_nav_state(),
        frontier=frontier,
        visited=set(),
    )
    assert ok and reason == ""


def test_validate_action_rejects_cross_product_target():
    nav = WikiNavigator.__new__(WikiNavigator)
    ok, reason = nav.validate_action(
        {"action": "OPEN_PAGE", "target": "product.beta.capability.x"},
        state=_nav_state(),
        frontier=[],
        visited=set(),
    )
    assert not ok
    assert reason == "TARGET_NOT_IN_PRODUCTS"


def test_allowed_actions_whitelist_complete():
    for action in ALLOWED_ACTIONS:
        assert isinstance(action, str) and action.isupper()
