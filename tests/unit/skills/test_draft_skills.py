"""DraftWritingSkill / DraftRevisionSkill / FullDraftWorkflowSkill 集成测试（阶段二 §25 / §26 / §27）。

通过真实 SkillRuntime（真实白名单 + 预算 + ArtifactStore + Fake 业务工具适配器）
端到端验证三个初稿技能的状态码语义、产物契约与版本递增。

运行（仓库根目录）:
    python -m pytest tests/unit/skills/test_draft_skills.py --basetemp ./.pytest-tmp-x -q --no-header
"""

from __future__ import annotations

from typing import Any

from agent.artifacts.store import ArtifactStore
from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import BusinessToolExecutor, FakeBusinessToolAdapter
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.draft_revision import DraftRevisionSkill
from agent.skills.draft_revision import build_manifests as build_revision
from agent.skills.draft_writing import DraftWritingSkill
from agent.skills.draft_writing import build_manifests as build_draft
from agent.skills.executable_registry import ExecutableSkillRegistry
from agent.skills.full_draft_workflow import FullDraftWorkflowSkill
from agent.skills.full_draft_workflow import build_manifests as build_workflow
from agent.skills.runtime import SkillRuntime

_DRAFT_SCOPES = frozenset({"articles:read", "drafts:write"})
_REVISION_SCOPES = frozenset({"drafts:write", "drafts:review"})
_WORKFLOW_SCOPES = frozenset(
    {
        "articles:read",
        "articles:classify",
        "products:read",
        "evidence:read",
        "articles:score",
        "drafts:write",
    }
)

_MANIFESTS: dict[str, SkillManifest] = {
    DraftWritingSkill.name: build_draft()[0],
    DraftRevisionSkill.name: build_revision()[0],
    FullDraftWorkflowSkill.name: build_workflow()[0],
}


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
        adapters={"fake": FakeBusinessToolAdapter()},
    )
    skill_registry = ExecutableSkillRegistry(business_tool_names=business_registry.names())
    skill_registry.register(DraftWritingSkill(), _MANIFESTS[DraftWritingSkill.name])
    skill_registry.register(DraftRevisionSkill(), _MANIFESTS[DraftRevisionSkill.name])
    skill_registry.register(FullDraftWorkflowSkill(), _MANIFESTS[FullDraftWorkflowSkill.name])

    store = ArtifactStore()
    runtime = SkillRuntime(
        skill_registry,
        tool_executor=executor,
        artifact_store=store,
        default_adapter="fake",
    )
    return runtime, store


def _parse_ref(ref: str) -> tuple[str, str, int]:
    """把 "ArtifactType:artifact_id@version" 拆成 (type, id, version)。"""
    artifact_type, rest = ref.split(":", 1)
    artifact_id, version = rest.rsplit("@", 1)
    return artifact_type, artifact_id, int(version)


# ═══════════════════════════════════════════════════════════════
# DraftWritingSkill
# ═══════════════════════════════════════════════════════════════


async def test_draft_writing_succeeds_with_score_above_threshold() -> None:
    runtime, store = _build_runtime()
    result: SkillResult = await runtime.execute(
        _request(
            "draft-writing",
            "run-draft-ok",
            input_refs={"article_ref": "article-123"},
            params={"product_ids": ["agent-security"], "pr_total_score": 85},
        ),
        scopes=_DRAFT_SCOPES,
    )
    assert result.status == "SUCCEEDED"
    assert result.artifact_refs

    artifact_type, artifact_id, version = _parse_ref(result.artifact_refs[0])
    assert artifact_type == "DraftArtifact"
    payload = await store.get(
        artifact_id=artifact_id, artifact_type="DraftArtifact", version=version
    )
    assert payload["status"] == "draft"
    assert payload["product_ids"] == ["agent-security"]


async def test_draft_writing_blocked_below_threshold() -> None:
    runtime, _store = _build_runtime()
    result: SkillResult = await runtime.execute(
        _request(
            "draft-writing",
            "run-draft-low",
            input_refs={"article_ref": "article-123"},
            params={"product_ids": ["agent-security"], "pr_total_score": 50},
        ),
        scopes=_DRAFT_SCOPES,
    )
    assert result.status == "BLOCKED"
    assert result.error_code == "score_below_threshold"
    assert result.artifact_refs == []


# ═══════════════════════════════════════════════════════════════
# DraftRevisionSkill
# ═══════════════════════════════════════════════════════════════


async def test_draft_revision_produces_next_version() -> None:
    runtime, store = _build_runtime()
    result: SkillResult = await runtime.execute(
        _request(
            "draft-revision",
            "run-revise",
            input_refs={"parent_artifact_ref": "DraftArtifact:art-1@1"},
            params={
                "source_artifact": {
                    "artifact_id": "art-1",
                    "version": 1,
                    "content_hash": "sha256:old",
                },
                "instruction": "补充分段与措辞对齐",
            },
        ),
        scopes=_REVISION_SCOPES,
    )
    assert result.status == "SUCCEEDED"
    assert result.artifact_refs

    artifact_type, artifact_id, version = _parse_ref(result.artifact_refs[0])
    assert artifact_type == "DraftArtifact"
    payload = await store.get(
        artifact_id=artifact_id, artifact_type="DraftArtifact", version=version
    )
    assert payload["version"] == 2
    assert payload["parent_version"] == 1
    assert payload["producer"] == "draft-revision"


# ═══════════════════════════════════════════════════════════════
# FullDraftWorkflowSkill
# ═══════════════════════════════════════════════════════════════


async def test_full_draft_workflow_succeeds() -> None:
    runtime, store = _build_runtime()
    result: SkillResult = await runtime.execute(
        _request(
            "full-draft-workflow",
            "run-workflow",
            input_refs={"article_ref": "article-123"},
            params={"query": "agent security"},
        ),
        scopes=_WORKFLOW_SCOPES,
    )
    assert result.status == "SUCCEEDED"
    assert result.artifact_refs
    assert result.evidence_refs == ["ev-fake"]

    artifact_type, artifact_id, version = _parse_ref(result.artifact_refs[0])
    assert artifact_type == "DraftArtifact"
    payload = await store.get(
        artifact_id=artifact_id, artifact_type="DraftArtifact", version=version
    )
    assert payload["product_ids"] == ["agent-security"]
