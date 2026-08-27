"""KnowledgeProvider 抽象 - 业务 Agent 与知识系统的统一入口。

PR-06 产物（文档 7）：
  - KnowledgeRequest：一次业务任务的知识请求
  - KnowledgeProvider Protocol：`collect_evidence(request) -> EvidenceBundle`
  - LegacyKnowledgeProvider：包装旧链路（迁移期对比）
  - WikiKnowledgeProvider：Wiki 为主（Navigator → Collector → Verifier）
  - ShadowKnowledgeProvider：legacy + wiki 双跑，返回 wiki 结果并记录差异

边界约定：
  - ScoringAgent / DraftAgent / ChatAgent 只消费 EvidenceBundle
  - Runtime Plane 只读 Wiki；Provider 负责组装带状态判断的 EvidenceBundle
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from agent.wiki.conflict_detector import detect_conflicts
from agent.wiki.evidence import EvidenceBundle, EvidenceConflict, EvidenceItem
from agent.wiki.evidence_collector import EvidenceCollector
from agent.wiki.evidence_verifier import EvidenceVerifier
from agent.wiki.index import WikiIndex
from agent.wiki.navigator import NavigationOutcome, WikiNavigator
from agent.wiki.observability import KnowledgeMetrics, record_trace
from agent.wiki.resolver import EntityResolver
from agent.wiki.store import WikiStore
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.provider")


class KnowledgeRequest(BaseModel):
    """一次业务任务的知识请求。"""

    task_type: str = Field(default="score", description="score / draft / chat")
    query: str = Field(default="")
    product_ids: list[str] = Field(default_factory=list)
    user_id: str | None = Field(default=None)
    max_pages: int | None = Field(default=None)
    max_depth: int | None = Field(default=None)
    # 观测/多租户预留字段（§19.3 / §21）：可选的请求上下文
    trace_id: str = Field(default="")
    task_id: str = Field(default="")
    tenant_id: str = Field(default="")


class KnowledgeProvider(Protocol):
    """KnowledgeProvider 协议。"""

    mode: str

    async def collect_evidence(self, request: KnowledgeRequest) -> EvidenceBundle: ...


# ═══════════════════════════════════════════════════════════════
# Bundle 组装辅助
# ═══════════════════════════════════════════════════════════════


def assemble_bundle(
    *,
    request: KnowledgeRequest,
    evidence: list[EvidenceItem],
    visited_pages: list[str],
    wiki_version: str,
    conflicts: list[EvidenceConflict] | None = None,
    coverage: float | None = None,
    confidence: float | None = None,
    evaluation: Any | None = None,
    coverage_threshold: float = 0.7,
) -> EvidenceBundle:
    """把已验证证据组装成 EvidenceBundle，并给出状态判断（§5.12）。

    evaluation（RequirementEvaluation）非空时：
      - coverage/confidence/missing_requirements/requirements 全部由"已验证 Evidence"
        驱动的 Requirement 评估决定；visited_pages 仅作观测，不参与 coverage 公式。
      - status 判定：
          FAILED                ← 无任何访问页面
          CONFLICTED            ← 存在冲突
          INSUFFICIENT_EVIDENCE ← 必选需求未满足 或 coverage < threshold
          SUFFICIENT            ← 必选需求满足且 coverage/confidence 达标

    evaluation 为空（legacy / shadow 直接构造）时保留旧的启发式逻辑。
    """
    conflicts = conflicts or []
    if evaluation is not None:
        coverage = coverage if coverage is not None else evaluation.coverage
        confidence = confidence if confidence is not None else evaluation.confidence

        if not visited_pages:
            status = "FAILED"
        elif conflicts:
            status = "CONFLICTED"
        elif not evaluation.all_required_met or coverage < coverage_threshold:
            status = "INSUFFICIENT_EVIDENCE"
        else:
            status = "SUFFICIENT"

        return EvidenceBundle(
            task_type=request.task_type,
            query=request.query,
            product_ids=request.product_ids,
            evidence=evidence,
            coverage=round(coverage, 4),
            confidence=round(confidence, 4),
            conflicts=conflicts,
            visited_pages=list(dict.fromkeys(visited_pages)),
            wiki_version=wiki_version,
            requirements=[r.model_dump() for r in evaluation.results],
            missing_requirements=list(evaluation.missing_requirements),
            status=status,  # type: ignore[arg-type]
        )

    # ── 旧启发式（legacy / 无 evaluation 的兼容路径）─────────
    if coverage is None:
        coverage = _coverage_of(visited_pages)
    if confidence is None:
        confidence = _confidence_of(evidence)

    if not visited_pages:
        status = "FAILED"
    elif conflicts:
        status = "CONFLICTED"
    elif not evidence or coverage < 0.3 or confidence < 0.6:
        status = "INSUFFICIENT_EVIDENCE"
    else:
        status = "SUFFICIENT"

    return EvidenceBundle(
        task_type=request.task_type,
        query=request.query,
        product_ids=request.product_ids,
        evidence=evidence,
        coverage=round(coverage, 4),
        confidence=round(confidence, 4),
        conflicts=conflicts,
        visited_pages=list(dict.fromkeys(visited_pages)),
        wiki_version=wiki_version,
        status=status,  # type: ignore[arg-type]
    )


def _coverage_of(visited_pages: list[str]) -> float:
    if not visited_pages:
        return 0.0
    return min(1.0, 0.25 + 0.15 * len(set(visited_pages)))


def _confidence_of(evidence: list[EvidenceItem]) -> float:
    if not evidence:
        return 0.0
    return sum(e.confidence for e in evidence) / len(evidence)


# detect_conflicts 已在 conflict_detector.py 实现，这里是向前兼容的再导出。
__all__ = [
    "KnowledgeProvider",
    "KnowledgeRequest",
    "LegacyKnowledgeProvider",
    "ShadowKnowledgeProvider",
    "WikiKnowledgeProvider",
    "assemble_bundle",
    "build_knowledge_provider",
    "detect_conflicts",
]


# ═══════════════════════════════════════════════════════════════
# LegacyKnowledgeProvider
# ═══════════════════════════════════════════════════════════════


class LegacyKnowledgeProvider:
    """旧链路包装：把现有知识切片合成一段文本证据。仅供迁移期对比。

    通过注入的 legacy_backend（返回原始知识文本）保持确定性、可测试。
    """

    mode = "legacy"

    def __init__(self, legacy_backend: Any | None = None):
        self._backend = legacy_backend

    async def collect_evidence(self, request: KnowledgeRequest) -> EvidenceBundle:
        text = ""
        if self._backend is not None:
            result = self._backend(request)
            if callable(result):
                result = await result()
            text = str(result or "")

        facts = [t.strip() for t in text.split("\n") if t.strip()]
        evidence = [
            EvidenceItem(
                evidence_id=f"ev-{i + 1}",
                fact=f,
                page_id="legacy",
                page_title="legacy",
                source_refs=[],
                relevance=0.5,
                confidence=0.9,
                relation_to_task="legacy",
            )
            for i, f in enumerate(facts)
        ]
        return assemble_bundle(
            request=request,
            evidence=evidence,
            visited_pages=["legacy"],  # 旧链路视为可信来源，不计入 FAILED
            wiki_version="legacy",
        )


# ═══════════════════════════════════════════════════════════════
# WikiKnowledgeProvider
# ═══════════════════════════════════════════════════════════════


class WikiKnowledgeProvider:
    """Wiki 为主：Navigator → EvidenceCollector → EvidenceVerifier → Bundle。"""

    mode = "wiki"

    def __init__(
        self,
        store: WikiStore,
        index: WikiIndex | None = None,
        source_registry: Any | None = None,
        source_root: str | Any | None = None,
        navigator: WikiNavigator | None = None,
        collector: EvidenceCollector | None = None,
        verifier: EvidenceVerifier | None = None,
        resolver: EntityResolver | None = None,
        *,
        llm: Any | None = None,
        navigator_llm_enabled: bool = False,
        confidence_threshold: float = 0.8,
        relevance_threshold: float = 0.5,
        min_coverage: dict[str, float] | None = None,
    ):
        self.store = store
        self.index = index
        self.resolver = EntityResolver(store=store, index=index) if resolver is None else resolver
        nav_llm = llm if navigator_llm_enabled else None
        self.navigator = navigator or WikiNavigator(
            store, index=index, resolver=self.resolver, llm=nav_llm
        )
        self.collector = collector or EvidenceCollector(store)
        self.verifier = verifier or EvidenceVerifier(store, source_registry, source_root)
        from agent.wiki.requirement_evaluator import RequirementEvaluator

        self.evaluator = RequirementEvaluator(
            confidence_threshold=confidence_threshold,
            relevance_threshold=relevance_threshold,
        )
        self.min_coverage = min_coverage or {"score": 0.7, "draft": 0.8, "chat": 0.6}
        self.metrics: KnowledgeMetrics | None = None

    async def collect_evidence(self, request: KnowledgeRequest) -> EvidenceBundle:
        import time

        start = time.perf_counter()
        outcome = await self.navigator.navigate(
            query=request.query,
            product_ids=request.product_ids or None,
            task_type=request.task_type,
            max_pages=request.max_pages,
            max_depth=request.max_depth,
        )
        bundle = self._bundle_from_outcome(request, outcome)
        # 结构化 Trace（§21）：每次请求输出完整观测，并累计运行指标
        record_trace(
            metrics=self.metrics,
            success=bundle.status != "FAILED",
            latency_ms=(time.perf_counter() - start) * 1000,
            trace_id=request.trace_id,
            task_id=request.task_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            product_ids=request.product_ids,
            wiki_version=bundle.wiki_version,
            status=bundle.status,
            reason=outcome.stop_reason,
            coverage=bundle.coverage,
            confidence=bundle.confidence,
            evidence_count=len(bundle.evidence),
            pages_opened=len(outcome.visited),
        )
        return bundle

    def _bundle_from_outcome(
        self, request: KnowledgeRequest, outcome: NavigationOutcome
    ) -> EvidenceBundle:
        from agent.wiki.requirements import default_requirements

        candidates = self.collector.collect(
            request.query, outcome.opened_pages, task_type=request.task_type
        )
        verified = self.verifier.verify(candidates)
        conflicts = detect_conflicts(verified)
        wiki_version = self.index.wiki_version if self.index is not None else ""
        evaluation = self.evaluator.evaluate(
            default_requirements(request.task_type), verified, conflicts=conflicts
        )
        return assemble_bundle(
            request=request,
            evidence=verified,
            visited_pages=outcome.visited,
            wiki_version=wiki_version,
            conflicts=conflicts,
            evaluation=evaluation,
            coverage_threshold=self.min_coverage.get(request.task_type, 0.7),
        )


# ═══════════════════════════════════════════════════════════════
# ShadowKnowledgeProvider
# ═══════════════════════════════════════════════════════════════


class ShadowKnowledgeProvider:
    """shadow 模式：legacy + wiki 双跑，用户仍看旧结果，后台记录差异。

    return 对象是 wiki EvidenceBundle；`last_comparison` 保存对比数据，
    供 sync telemetry 消费。scoring 集成阶段决定是否对业务暴露 legacy。
    """

    mode = "shadow"

    def __init__(
        self,
        legacy: LegacyKnowledgeProvider,
        wiki: WikiKnowledgeProvider,
    ):
        self.legacy = legacy
        self.wiki = wiki
        self.last_comparison: dict[str, Any] = {}

    async def collect_evidence(self, request: KnowledgeRequest) -> EvidenceBundle:
        legacy_bundle = await self.legacy.collect_evidence(request)
        wiki_bundle = await self.wiki.collect_evidence(request)
        self.last_comparison = {
            "legacy_evidence": len(legacy_bundle.evidence),
            "wiki_evidence": len(wiki_bundle.evidence),
            "wiki_coverage": wiki_bundle.coverage,
            "wiki_confidence": wiki_bundle.confidence,
            "wiki_status": wiki_bundle.status,
            "wiki_version": wiki_bundle.wiki_version,
            "visited_pages": wiki_bundle.visited_pages,
        }
        return wiki_bundle


# ═══════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════


def build_knowledge_provider(
    *,
    mode: str,
    store: WikiStore | None = None,
    index: WikiIndex | None = None,
    source_registry: Any | None = None,
    source_root: str | None = None,
    legacy_backend: Any | None = None,
    llm: Any | None = None,
    navigator_llm_enabled: bool = False,
    confidence_threshold: float = 0.8,
    relevance_threshold: float = 0.5,
    min_coverage: dict[str, float] | None = None,
) -> KnowledgeProvider:
    """按 KNOWLEDGE_BACKEND 构建对应的 Provider。"""
    if mode == "legacy":
        return LegacyKnowledgeProvider(legacy_backend=legacy_backend)
    if store is None:
        raise ValueError(f"mode {mode!r} 需要提供 store")
    wiki = WikiKnowledgeProvider(
        store=store,
        index=index,
        source_registry=source_registry,
        source_root=source_root,
        llm=llm,
        navigator_llm_enabled=navigator_llm_enabled,
        confidence_threshold=confidence_threshold,
        relevance_threshold=relevance_threshold,
        min_coverage=min_coverage,
    )
    if mode == "wiki":
        return wiki
    if mode == "shadow":
        legacy = LegacyKnowledgeProvider(legacy_backend=legacy_backend)
        return ShadowKnowledgeProvider(legacy=legacy, wiki=wiki)
    raise ValueError(f"未知 KNOWLEDGE_BACKEND: {mode!r}")
