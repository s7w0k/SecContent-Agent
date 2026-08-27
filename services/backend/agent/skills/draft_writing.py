"""DraftWritingSkill - 初稿撰写技能（阶段二 §25）。

读取已授权、评分达标的文章 → 调用 generate_draft 生成初稿，
产出 DraftArtifact 供后续 review / revise 交接。

安全不变量：
  - 评分门槛：未显式 `force` 时，pr_total_score < 80 直接 BLOCKED，绝不生成初稿；
  - 一切工具调用（get_article / generate_draft）均经 context.execute_tool 白名单 + 预算边界。
"""

from __future__ import annotations

from typing import Any

from agent.skills.context import SkillExecutionContext
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry

ARTICLE_REF_KEY = "article_ref"
SCORE_THRESHOLD = 80


def _build_manifest() -> SkillManifest:
    """构造 DraftWritingSkill 的显式 SkillManifest。

    作为 register() / build_manifests() 的单一来源，避免清单漂移。
    """
    return SkillManifest(
        name=DraftWritingSkill.name,
        version=DraftWritingSkill.version,
        description=DraftWritingSkill.description,
        purpose=DraftWritingSkill.purpose,
        required_tools=DraftWritingSkill.required_tools,
        risk_level=DraftWritingSkill.risk_level,
        required_scopes=frozenset(DraftWritingSkill.required_scopes),
        output_artifact_type=DraftWritingSkill.output_artifact_type,
    )


def build_manifests() -> list[SkillManifest]:
    """返回本模块内所有 Skill 的清单（当前仅一个）。"""
    return [_build_manifest()]


def register(registry: ExecutableSkillRegistry) -> None:
    """把该 Skill 及其显式清单注册进 ExecutableSkillRegistry。"""
    registry.register(DraftWritingSkill(), _build_manifest())


class DraftWritingSkill:
    """撰写初稿：评分达标后基于文章与产品情报生成 DraftArtifact（计划 §25）。"""

    name = "draft-writing"
    version = "1.0.0"
    description = "对评分达标的文章调用 generate_draft 生成初稿，产出 DraftArtifact。"
    purpose = "draft"
    risk_level = "medium"
    required_scopes = frozenset({"articles:read", "drafts:write"})
    required_tools = ("get_article", "generate_draft")
    output_artifact_type = "DraftArtifact"

    async def execute(
        self,
        request: SkillRequest,
        context: SkillExecutionContext,
    ) -> SkillResult:
        article_ref = request.input_refs.get(ARTICLE_REF_KEY) or ""
        if not article_ref:
            return SkillResult.failed(
                self.name,
                "missing_article_ref",
                f"input_refs['{ARTICLE_REF_KEY}'] 缺失",
            )

        force = bool(request.params.get("force", False))
        pr_total_score = int(request.params.get("pr_total_score") or 0)
        if not force and pr_total_score < SCORE_THRESHOLD:
            return SkillResult.blocked(
                self.name,
                "score_below_threshold",
                f"pr_total_score={pr_total_score} 低于阈值 {SCORE_THRESHOLD}",
            )

        get_result = await context.execute_tool("get_article", {"article_id": article_ref})
        if not get_result.found:
            return SkillResult.failed(
                self.name,
                "article_not_found",
                f"文章不存在: {article_ref}",
            )

        product_ids = list(request.params.get("product_ids") or [])
        if not product_ids:
            return SkillResult.failed(
                self.name,
                "missing_product_ids",
                "params['product_ids'] 缺失",
            )

        template_key = str(request.params.get("template_key", "default"))
        idempotency_key = str(request.params.get("idempotency_key") or f"draft-{request.run_id}")
        generated = await context.execute_tool(
            "generate_draft",
            {
                "article": {"article_id": article_ref},
                "product_ids": product_ids,
                "template_key": template_key,
                "angle": str(request.params.get("angle") or ""),
                "tone": str(request.params.get("tone") or "professional"),
                "target_length": int(request.params.get("target_length") or 1200),
                "idempotency_key": idempotency_key,
            },
        )

        evidence_refs = list(getattr(generated, "evidence_refs", []) or [])
        payload: dict[str, Any] = {
            "parent_article_ref": article_ref,
            "product_ids": product_ids,
            "content": getattr(generated, "content", ""),
            "evidence_refs": evidence_refs,
            "content_hash": generated.artifact.content_hash,
            "context_hash": getattr(generated, "context_hash", ""),
            "template_key": template_key,
            "version": int(request.params.get("version") or 1),
            "status": "draft",
        }
        record = await context.store_artifact(
            artifact_type="DraftArtifact",
            payload=payload,
            producer="draft-writing",
            step_id="draft",
        )

        return SkillResult.succeeded(
            self.name,
            artifact_refs=[str(record["ref"])],
            evidence_refs=evidence_refs,
            next_recommendations=["review"],
        )
