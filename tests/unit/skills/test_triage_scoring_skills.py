"""ArticleTriageSkill / ProductScoringSkill 集成测试（阶段二 §19 / §21）。

通过真实 SkillRuntime（真实白名单 + 预算 + ArtifactStore + Fake 业务工具适配器）
端到端验证两个新 Skill 的产物契约、工具白名单与状态码语义。

运行（仓库根目录）:
    python -m pytest tests/unit/skills/test_triage_scoring_skills.py --basetemp ./.pytest-tmp-x -q --no-header
"""

from __future__ import annotations

from typing import Any

from agent.artifacts.store import ArtifactStore
from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import (
    BusinessToolAdapterKind,
    BusinessToolExecutor,
    FakeBusinessToolAdapter,
)
from agent.skills.article_triage import ArticleTriageSkill
from agent.skills.article_triage import build_manifests as build_triage
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry
from agent.skills.product_scoring import ProductScoringSkill
from agent.skills.product_scoring import build_manifests as build_score
from agent.skills.runtime import SkillRuntime

_TRIAGE_SCOPES = frozenset({"articles:read", "articles:classify"})
_SCORE_SCOPES = frozenset({"articles:read", "products:read", "evidence:read", "articles:score"})

_TRIAGE_MANIFEST = build_triage()[0]
_SCORE_MANIFEST = build_score()[0]


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _request(skill_name: str, run_id: str, **extra: Any) -> SkillRequest:
    """构造最小 SkillRequest，便于用例覆写 input_refs / params。"""
    payload: dict[str, Any] = {
        "skill_name": skill_name,
        "run_id": run_id,
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "trace_id": "trace-1",
    }
    payload.update(extra)
    return SkillRequest(**payload)


def _build_runtime() -> tuple[SkillRuntime, ArtifactStore]:
    """用真实注册表 + Fake 业务工具适配器 + 内存 ArtifactStore 组装运行时。"""
    business_registry = build_business_tool_registry()
    executor = BusinessToolExecutor(
        business_registry,
        adapters={BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter()},
    )
    skill_registry = ExecutableSkillRegistry(business_tool_names=business_registry.names())
    skill_registry.register(ArticleTriageSkill(), _TRIAGE_MANIFEST)
    skill_registry.register(ProductScoringSkill(), _SCORE_MANIFEST)

    store = ArtifactStore()
    runtime = SkillRuntime(
        skill_registry,
        tool_executor=executor,
        artifact_store=store,
        default_adapter=BusinessToolAdapterKind.FAKE,
    )
    return runtime, store


def _parse_ref(ref: str) -> tuple[str, str, int]:
    """把 "ArtifactType:artifact_id@version" 拆成 (type, id, version)。"""
    artifact_type, rest = ref.split(":", 1)
    artifact_id, version = rest.rsplit("@", 1)
    return artifact_type, artifact_id, int(version)


# ═══════════════════════════════════════════════════════════════
# ArticleTriageSkill
# ═══════════════════════════════════════════════════════════════


async def test_article_triage_runs_and_produces_artifact():
    runtime, _store = _build_runtime()
    result = await runtime.execute(
        _request(
            "article-triage",
            "run-triage",
            input_refs={"article_ref": "article-123"},
        ),
        scopes=_TRIAGE_SCOPES,
    )
    assert result.status == "SUCCEEDED"
    assert len(result.artifact_refs) == 1
    artifact_type, _aid, _version = _parse_ref(result.artifact_refs[0])
    assert artifact_type == "TriageArtifact"


# ═══════════════════════════════════════════════════════════════
# ProductScoringSkill
# ═══════════════════════════════════════════════════════════════


async def test_product_scoring_sufficient_returns_score_artifact():
    runtime, store = _build_runtime()
    result = await runtime.execute(
        _request(
            "product-scoring",
            "run-score",
            input_refs={"article_ref": "article-123"},
            params={"query": "agent security"},
        ),
        scopes=_SCORE_SCOPES,
    )
    assert result.status == "SUCCEEDED"
    assert result.artifact_refs

    artifact_type, artifact_id, version = _parse_ref(result.artifact_refs[0])
    assert artifact_type == "ScoringArtifact"
    payload = await store.get(
        artifact_id=artifact_id, artifact_type="ScoringArtifact", version=version
    )
    assert payload["status"] == "SCORED"
    assert payload["best_product_id"] == "agent-security"
    assert payload["products"][0]["status"] == "SCORED"
    assert payload["pr_total_score"] == 0.0
    assert payload["evidence_bundle_refs"] == ["EvidenceBundleArtifact:eb-fake@1"]
    assert result.evidence_refs == ["ev-fake"]


async def test_product_scoring_insufficient_evidence_skips_scoring():
    runtime, _store = _build_runtime()
    # Fake adapter 将 collect_product_evidence 覆盖为 INSUFFICIENT_EVIDENCE
    business = build_business_tool_registry()
    executor = BusinessToolExecutor(
        business,
        adapters={
            BusinessToolAdapterKind.FAKE: FakeBusinessToolAdapter(
                {
                    "collect_product_evidence": {
                        "status": "INSUFFICIENT_EVIDENCE",
                        "evidence_bundle_ref": "EvidenceBundleArtifact:eb-empty@1",
                        "product_ids": ["agent-security"],
                        "coverage": 0.1,
                        "confidence": 0.2,
                        "evidence_ids": [],
                        "missing_requirements": ["product-overview"],
                        "wiki_version": "v1",
                    }
                }
            )
        },
    )
    skill_registry = ExecutableSkillRegistry(business_tool_names=business.names())
    skill_registry.register(ProductScoringSkill(), _SCORE_MANIFEST)
    score_store = ArtifactStore()
    runtime = SkillRuntime(
        skill_registry,
        tool_executor=executor,
        artifact_store=score_store,
        default_adapter=BusinessToolAdapterKind.FAKE,
    )

    result = await runtime.execute(
        _request(
            "product-scoring",
            "run-score-insufficient",
            input_refs={"article_ref": "article-123"},
            params={"query": "agent security"},
        ),
        scopes=_SCORE_SCOPES,
    )
    assert result.status == "SUCCEEDED"
    _artifact_type, artifact_id, version = _parse_ref(result.artifact_refs[0])
    payload = await score_store.get(
        artifact_id=artifact_id, artifact_type="ScoringArtifact", version=version
    )
    assert payload["status"] == "INSUFFICIENT_EVIDENCE"
    assert payload["products"][0]["status"] == "NO_SCORE"
    assert payload["best_product_id"] == ""
    assert result.next_recommendations == ["end"]


# ═══════════════════════════════════════════════════════════════
# 工具白名单：未声明 Tool 拒绝
# ═══════════════════════════════════════════════════════════════


class UnderclaredToolSkill:
    """清单不声明任何 Tool，但执行时却尝试调用 get_article。"""

    name = "undeclared-tool-skill"
    version = "1.0.0"
    description = "skill 未声明任何 tool，却尝试调用 get_article"
    purpose = "test"
    risk_level = "low"
    required_scopes = frozenset()
    required_tools = ()
    output_artifact_type = "TestArtifact"

    async def execute(
        self,
        request: SkillRequest,
        context: Any,
    ) -> SkillResult:
        await context.execute_tool("get_article", {"article_id": "article-1"})
        return SkillResult.succeeded(self.name, artifact_refs=["ref-x"])


async def test_skill_tool_undeclared_is_rejected():
    runtime, _store = _build_runtime()
    runtime.registry.register(
        UnderclaredToolSkill(),
        SkillManifest(
            name=UnderclaredToolSkill.name,
            version=UnderclaredToolSkill.version,
            description=UnderclaredToolSkill.description,
            purpose=UnderclaredToolSkill.purpose,
            required_tools=UnderclaredToolSkill.required_tools,
            risk_level=UnderclaredToolSkill.risk_level,
            output_artifact_type=UnderclaredToolSkill.output_artifact_type,
        ),
    )
    result = await runtime.execute(
        _request("undeclared-tool-skill", "run-undeclared"),
        scopes=frozenset(),
    )
    assert result.status == "BLOCKED"
    assert result.error_code == "undeclared_tool"
