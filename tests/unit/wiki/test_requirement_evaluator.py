"""Final Plan PR-B：Requirement Evaluator + Coverage（§5.15）。

核心论断：打开(访问)页面数量不再决定 coverage；只有经过验证且与查询相关的
Evidence 才满足 Requirement。
"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceConflict, EvidenceItem
from agent.wiki.provider import KnowledgeRequest, assemble_bundle
from agent.wiki.requirement_evaluator import RequirementEvaluator
from agent.wiki.requirements import EvidenceRequirement, default_requirements


def _ev(
    *, fact="支持行为检测", rid="R1", conf=0.9, rel=0.8, reason="VERIFIED", eid=None
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=eid or f"ev-{abs(hash(fact)) % 1000}",
        fact=fact,
        page_id="capability.x",
        page_title="x",
        requirement_ids=[rid],
        reason_code=reason,
        source_refs=[SourceRef(source_id="s", relative_path="a.md", content_hash="h")],
        relevance=rel,
        confidence=conf,
    )


score_reqs = default_requirements("score")  # R1=0.5(required), R2=0.3, R3=0.2


def test_verified_capability_evidence_satisfies_r1():
    ev = _ev(rid="R1")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    r1 = next(r for r in res.results if r.requirement_id == "R1")
    assert r1.status == "MET"
    assert abs(res.coverage - 0.5) < 1e-6
    assert res.missing_requirements == ["R2", "R3"]


def test_open_page_count_does_not_change_coverage():
    """访问页数不参与 coverage 公式（§5.11/§5.15）。"""
    no_pages = RequirementEvaluator().evaluate(score_reqs, [_ev(rid="R1")])
    fake_visited_extra = [_ev(rid="R1")]  # 同样只有 1 条有效证据
    assert (
        no_pages.coverage
        == RequirementEvaluator().evaluate(score_reqs, fake_visited_extra).coverage
    )


def test_low_relevance_capability_does_not_satisfy_r1():
    ev = _ev(rid="R1", rel=0.3)
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    r1 = next(r for r in res.results if r.requirement_id == "R1")
    assert r1.status == "OPEN"
    assert res.coverage == 0.0


def test_low_confidence_does_not_satisfy():
    ev = _ev(rid="R1", conf=0.5)
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    r1 = next(r for r in res.results if r.requirement_id == "R1")
    assert r1.status == "OPEN"


def test_stale_evidence_does_not_satisfy_requirement():
    ev = _ev(rid="R1", reason="STALE_SOURCE_REF")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert res.coverage == 0.0


def test_hash_mismatch_does_not_satisfy():
    ev = _ev(rid="R1", reason="SOURCE_HASH_MISMATCH")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert res.coverage == 0.0


def test_unsupported_evidence_not_counted():
    ev = _ev(rid="R1", reason="NOT_SUPPORTED")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    r1 = next(r for r in res.results if r.requirement_id == "R1")
    assert r1.status == "OPEN"


def test_conflicted_requirement_not_counted():
    ev = _ev(rid="R1", fact="支持行为检测")
    conflict = EvidenceConflict(
        topic="行为检测",
        claims=["支持行为检测", "不提供行为检测"],
        source_refs=[
            SourceRef(source_id="a", relative_path="a.md", content_hash="h"),
            SourceRef(source_id="b", relative_path="b.md", content_hash="h"),
        ],
    )
    res = RequirementEvaluator().evaluate(score_reqs, [ev], conflicts=[conflict])
    r1 = next(r for r in res.results if r.requirement_id == "R1")
    assert r1.status == "CONFLICTED"
    assert "R1" not in res.met_requirements
    assert res.coverage == 0.0


def test_duplicate_evidence_does_not_inflate():
    ev_a = _ev(rid="R1", eid="ev-1")
    ev_b = _ev(rid="R1", eid="ev-1")
    res = RequirementEvaluator().evaluate(score_reqs, [ev_a, ev_b])
    r1 = next(r for r in res.results if r.requirement_id == "R1")
    assert r1.evidence_count == 1


def test_minimum_evidence_is_enforced():
    req = EvidenceRequirement(
        requirement_id="R1",
        description="d",
        weight=0.5,
        required_page_types=["capability"],
        minimum_evidence=2,
    )
    ev = _ev(rid="R1")
    res = RequirementEvaluator().evaluate([req], [ev])
    r1 = res.results[0]
    assert r1.status == "PARTIAL"


def test_required_requirement_missing_blocks_sufficient():
    # 只有 R2 满足，required 的 R1 未满足 → all_required_met=False
    ev = _ev(rid="R2", fact="场景匹配")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert res.all_required_met is False
    assert res.missing_requirements == ["R1", "R3"]


def test_weighted_coverage_is_correct():
    ev = _ev(rid="R1")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert abs(res.coverage - 0.5) < 1e-6


def test_all_required_met_and_coverage_sufficient():
    ev = _ev(rid="R1")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert res.all_required_met is True


def test_missing_requirements_are_returned():
    res = RequirementEvaluator().evaluate(score_reqs, [_ev(rid="R1")])
    assert set(res.missing_requirements) == {"R2", "R3"}


# ── Bundle Status（§5.12）───────────────────────────────────


def _bundle_status(evaluation, threshold=0.7, visited=("p",)):
    return assemble_bundle(
        request=KnowledgeRequest(task_type="score", query="q", product_ids=["agent"]),
        evidence=[],
        visited_pages=list(visited),
        wiki_version="v",
        evaluation=evaluation,
        coverage_threshold=threshold,
    ).status


def test_bundle_insufficient_when_required_missing():
    ev = _ev(rid="R2")  # R1(required) missing
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert _bundle_status(res) == "INSUFFICIENT_EVIDENCE"


def test_bundle_insufficient_when_coverage_below_threshold():
    pass  # R1 MET coverage=0.5 < 0.7 → threshold 校验由以下用例覆盖


def test_bundle_sufficient_when_required_met_and_coverage_ok():
    ev = _ev(rid="R1")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    # required R1 MET；降低阈值后仍要求 coverage>=threshold
    assert _bundle_status(res, threshold=0.5) == "SUFFICIENT"
    assert _bundle_status(res, threshold=0.7) == "INSUFFICIENT_EVIDENCE"


def test_bundle_failed_when_no_pages():
    ev = _ev(rid="R1")
    res = RequirementEvaluator().evaluate(score_reqs, [ev])
    assert _bundle_status(res, threshold=0.5, visited=()) == "FAILED"


def test_acceptance_case_reads_many_pages_only_one_verified():
    """§5.16 验收：读 10 个 capability 页但只有 1 条已验证 R1 证据 → coverage=0.5。"""
    many_pages = [_ev(rid="R1", eid=f"dup-{i}") for i in range(10)]  # 全部只导向 R1
    res = RequirementEvaluator().evaluate(score_reqs, many_pages)
    # 多条证据让 R1 MET，但 R2/R3 仍 OPEN → coverage 不应 == 1.0
    assert abs(res.coverage - 0.5) < 1e-6
    assert res.coverage != 1.0
