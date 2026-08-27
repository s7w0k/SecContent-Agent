"""Phase 20 Golden Dataset / Shadow Evaluation（§23）单元测试。"""

from __future__ import annotations

from agent.wiki.contracts import SourceRef
from agent.wiki.evidence import EvidenceBundle, EvidenceItem
from agent.wiki.golden import (
    GOLDEN_DATASET,
    GoldenTask,
    evaluate_task,
    run_golden,
    summarize_eval,
)


def _bundle(
    *,
    status: str = "SUFFICIENT",
    facts: list[tuple[str, float]] | None = None,
    product_ids: list[str] | None = None,
) -> EvidenceBundle:
    items = [
        EvidenceItem(
            evidence_id=f"ev{i}",
            claim_id=f"cl{i}",
            fact=fact,
            page_id="capability.a",
            page_title="能力",
            source_refs=[
                SourceRef(source_id="s1", relative_path="1-产品/overview.md", content_hash="h")
            ],
            relevance=0.9,
            confidence=conf,
            relation_to_task="potential_match",
        )
        for i, (fact, conf) in enumerate(facts or [("支持 OIDC 联合身份", 0.9)])
    ]
    return EvidenceBundle(
        status=status,
        query="q",
        product_ids=product_ids or ["aiscm"],
        evidence=items,
        coverage=0.8,
        confidence=0.9,
    )


def test_golden_dataset_is_well_formed():
    assert GOLDEN_DATASET
    ids = [t.task_id for t in GOLDEN_DATASET]
    assert len(ids) == len(set(ids))
    for t in GOLDEN_DATASET:
        assert GoldenTask.model_validate(t)
        assert t.query


def test_evaluate_sufficient_task_passes():
    task = GoldenTask(
        task_id="t1",
        query="支持哪些认证协议？",
        product_ids=["aiscm"],
        expected_entity=["aiscm"],
        required_claims=["OIDC"],
        expected_status="SUFFICIENT",
    )
    res = evaluate_task(_bundle(), task, latency_ms=5.0)
    assert res.passed is True
    assert res.status_match is True
    assert res.covered_claims == ["OIDC"]
    assert res.unsupported_claim_rate == 0.0
    assert res.grounding_precision == 1.0
    assert res.latency_ms == 5.0


def test_evaluate_insufficient_status_mismatch():
    task = GoldenTask(
        task_id="t2",
        query="未知产品",
        expected_status="INSUFFICIENT_EVIDENCE",
    )
    res = evaluate_task(_bundle(status="FAILED", facts=[]), task)
    assert res.passed is False
    assert res.status_match is False


def test_evaluate_forbidden_claim_violated():
    task = GoldenTask(
        task_id="t3",
        query="认证因子",
        forbidden_claims=["短信验证码是唯一认证因子"],
        expected_status="SUFFICIENT",
    )
    res = evaluate_task(_bundle(facts=[("支持短信验证码是唯一认证因子，兼具 OIDC", 0.9)]), task)
    assert res.violated_forbidden_claims
    assert res.passed is False


def test_summarize_eval_aggregates_metrics():
    t1 = GoldenTask(task_id="a", query="q1", expected_status="SUFFICIENT")
    t2 = GoldenTask(task_id="b", query="q2", expected_status="SUFFICIENT")
    ok = evaluate_task(_bundle(), t1)
    bad = evaluate_task(_bundle(status="FAILED", facts=[]), t2)
    summary = summarize_eval([ok, bad])
    assert summary["count"] == 2
    assert summary["pass_rate"] == 0.5
    assert summary["status_accuracy"] == 0.5
    assert summary["grounding_rate"] == 1.0
    assert summary["unsupported_claim_rate"] == 0.0


def test_summarize_eval_empty():
    assert summarize_eval([])["count"] == 0
    assert summarize_eval([])["pass_rate"] == 0.0


async def test_run_golden_invokes_provider():
    calls = []

    class _StubProvider:
        async def collect_evidence(self, request):
            calls.append(request.query)
            return _bundle()

    results = await run_golden(_StubProvider(), [GOLDEN_DATASET[0]])
    assert len(results) == 1
    assert calls == [GOLDEN_DATASET[0].query]
