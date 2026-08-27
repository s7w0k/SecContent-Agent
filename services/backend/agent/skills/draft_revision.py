"""DraftRevisionSkill - 初稿修订技能（阶段二 §27）。

基于上一版 DraftArtifact（source_artifact）调用 revise_draft 生成新一版
初稿，产出 version + 1 的 DraftArtifact，并通过 parent_ref 串联版本链。

安全不变量：
  - 一切工具调用（revise_draft）均经 context.execute_tool 白名单 + 预算边界。
"""

from __future__ import annotations

import hashlib
from typing import Any

from agent.skills.context import SkillExecutionContext
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry

PARENT_REF_KEY = "parent_artifact_ref"


def _build_manifest() -> SkillManifest:
    """构造 DraftRevisionSkill 的显式 SkillManifest。

    作为 register() / build_manifests() 的单一来源，避免清单漂移。
    """
    return SkillManifest(
        name=DraftRevisionSkill.name,
        version=DraftRevisionSkill.version,
        description=DraftRevisionSkill.description,
        purpose=DraftRevisionSkill.purpose,
        required_tools=DraftRevisionSkill.required_tools,
        risk_level=DraftRevisionSkill.risk_level,
        required_scopes=frozenset(DraftRevisionSkill.required_scopes),
        output_artifact_type=DraftRevisionSkill.output_artifact_type,
    )


def build_manifests() -> list[SkillManifest]:
    """返回本模块内所有 Skill 的清单（当前仅一个）。"""
    return [_build_manifest()]


def register(registry: ExecutableSkillRegistry) -> None:
    """把该 Skill 及其显式清单注册进 ExecutableSkillRegistry。"""
    registry.register(DraftRevisionSkill(), _build_manifest())


class DraftRevisionSkill:
    """修订初稿：基于上一版 artifact 调用 revise_draft 生成下一版（计划 §27）。"""

    name = "draft-revision"
    version = "1.0.0"
    description = "基于上一版初稿调用 revise_draft 生成下一版，产出新 DraftArtifact。"
    purpose = "revise"
    risk_level = "medium"
    required_scopes = frozenset({"drafts:write", "drafts:review"})
    required_tools = ("revise_draft",)
    output_artifact_type = "DraftArtifact"

    async def execute(
        self,
        request: SkillRequest,
        context: SkillExecutionContext,
    ) -> SkillResult:
        parent_ref = request.input_refs.get(PARENT_REF_KEY) or ""
        if not parent_ref:
            return SkillResult.failed(
                self.name,
                "missing_parent_ref",
                f"input_refs['{PARENT_REF_KEY}'] 缺失",
            )

        instruction = str(
            request.input_refs.get("revision_instruction")
            or request.params.get("instruction")
            or ""
        )
        if not instruction:
            return SkillResult.failed(
                self.name,
                "missing_instruction",
                "revision_instruction / params['instruction'] 缺失",
            )

        source = request.params.get("source_artifact")
        if not isinstance(source, dict):
            return SkillResult.failed(
                self.name,
                "missing_source_artifact",
                "params['source_artifact'] 缺失",
            )
        parent_version = max(int(source.get("version", 1)), 1)
        next_version = parent_version + 1

        artifact = {
            "artifact_id": str(source.get("artifact_id") or ""),
            "version": parent_version,
            "content_hash": str(source.get("content_hash") or ""),
        }
        idempotency_key = str(
            request.params.get("idempotency_key") or f"revise-{request.run_id}-{next_version}"
        )
        revise = await context.execute_tool(
            "revise_draft",
            {
                "artifact": artifact,
                "instruction": instruction,
                "expected_version": int(request.params.get("expected_version") or next_version),
                "idempotency_key": idempotency_key,
            },
        )

        content_hash = str(getattr(revise.artifact, "content_hash", "") or "")
        if not content_hash:
            # 工具未回传 content_hash 时，以"未变更内容"的哈希作为兜底指纹
            unchanged = str(source.get("content_hash") or instruction)
            content_hash = self._sha256(unchanged)

        payload: dict[str, Any] = {
            "parent_artifact_ref": parent_ref,
            "parent_version": parent_version,
            "version": next_version,
            "content_hash": content_hash,
            "changed_sections": list(getattr(revise, "changed_sections", []) or []),
            "trace_id": request.trace_id,
            "producer": "draft-revision",
            "status": "draft",
        }
        record = await context.store_artifact(
            artifact_type="DraftArtifact",
            payload=payload,
            producer="draft-revision",
            step_id="revise",
            parent_ref=parent_ref,
        )

        return SkillResult.succeeded(
            self.name,
            artifact_refs=[str(record["ref"])],
            next_recommendations=["review"],
        )

    @staticmethod
    def _sha256(content: str) -> str:
        """对内容做 sha256 指纹，返回 "sha256:<hex>"。"""
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
