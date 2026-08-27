"""FullDraftWorkflowSkill - 固定高层工作流技能（阶段二 §26）。

作为协调器，不把子技能当作 Agent 分发，而是在同一 Skill 内按固定管线
顺序调用业务工具：get_article → match_products → collect_product_evidence
→ score_article →（达标则）generate_draft，最终产出 DraftArtifact。

安全不变量：
  - 管线中任意工具调用均经 context.execute_tool 白名单 + 预算边界；
  - 只对评分达标的文章触发生成，否则产出的 DraftArtifact 不含正文。
"""

from __future__ import annotations

from typing import Any

from agent.skills.context import SkillExecutionContext
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry

ARTICLE_REF_KEY = "article_ref"
_DEFAULT_THRESHOLD = 80


def _build_manifest() -> SkillManifest:
    """构造 FullDraftWorkflowSkill 的显式 SkillManifest。

    作为 register() / build_manifests() 的单一来源，避免清单漂移。
    required_scopes 为各子技能 Scope（triage / score / draft）的并集。
    """
    return SkillManifest(
        name=FullDraftWorkflowSkill.name,
        version=FullDraftWorkflowSkill.version,
        description=FullDraftWorkflowSkill.description,
        purpose=FullDraftWorkflowSkill.purpose,
        required_tools=FullDraftWorkflowSkill.required_tools,
        risk_level=FullDraftWorkflowSkill.risk_level,
        required_scopes=frozenset(FullDraftWorkflowSkill.required_scopes),
        output_artifact_type=FullDraftWorkflowSkill.output_artifact_type,
    )


def build_manifests() -> list[SkillManifest]:
    """返回本模块内所有 Skill 的清单（当前仅一个）。"""
    return [_build_manifest()]


def register(registry: ExecutableSkillRegistry) -> None:
    """把该 Skill 及其显式清单注册进 ExecutableSkillRegistry。"""
    registry.register(FullDraftWorkflowSkill(), _build_manifest())


class FullDraftWorkflowSkill:
    """固定流程协调器：按固定顺序执行读文章→匹配产品→收集证据→打分→生成（§26）。"""

    name = "full-draft-workflow"
    version = "1.0.0"
    description = "固定高层工作流：读文章→匹配产品→收集证据→打分→(达标)生成初稿。"
    purpose = "workflow"
    risk_level = "medium"
    required_scopes = frozenset(
        {
            "articles:read",
            "articles:classify",
            "products:read",
            "evidence:read",
            "articles:score",
            "drafts:write",
        }
    )
    required_tools = (
        "get_article",
        "classify_article",
        "match_products",
        "collect_product_evidence",
        "score_article",
        "generate_draft",
    )
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

        get_result = await context.execute_tool("get_article", {"article_id": article_ref})
        if not get_result.found:
            return SkillResult.failed(
                self.name,
                "article_not_found",
                f"文章不存在: {article_ref}",
            )

        match = await context.execute_tool(
            "match_products",
            {
                "article": {"article_id": article_ref},
                "explicit_product_ids": list(request.params.get("explicit_product_ids") or []),
                "max_candidates": int(request.params.get("max_candidates") or 2),
            },
        )
        product_ids = [str(candidate.product_id) for candidate in match.candidates]
        target_products: list[str] = product_ids or ["agent-security"]

        query = str(request.params.get("query") or "")
        evidence_refs: list[str] = []
        for product_id in target_products:
            collect = await context.execute_tool(
                "collect_product_evidence",
                {"query": query, "product_ids": [product_id], "task_type": "draft"},
            )
            evidence_refs.extend(str(item) for item in (getattr(collect, "evidence_ids", []) or []))

        score = await context.execute_tool(
            "score_article",
            {
                "article": {"article_id": article_ref},
                "product_ids": target_products,
                "skill_version": str(request.params.get("skill_version") or "score.v1"),
            },
        )
        relevance = self._dimension_score(score, "product_relevance")
        impact = self._dimension_score(score, "event_impact")
        total_score = relevance + impact
        threshold = int(request.params.get("score_threshold") or _DEFAULT_THRESHOLD)

        content = ""
        content_hash = ""
        if total_score >= threshold:
            generated = await context.execute_tool(
                "generate_draft",
                {
                    "article": {"article_id": article_ref},
                    "product_ids": target_products,
                    "template_key": str(request.params.get("template_key") or "default"),
                    "angle": str(request.params.get("angle") or ""),
                    "tone": str(request.params.get("tone") or "professional"),
                    "target_length": int(request.params.get("target_length") or 1200),
                    "idempotency_key": str(
                        request.params.get("idempotency_key") or f"workflow-{request.run_id}"
                    ),
                },
            )
            content = getattr(generated, "content", "")
            content_hash = generated.artifact.content_hash
            evidence_refs.extend(
                str(item) for item in (getattr(generated, "evidence_refs", []) or [])
            )

        payload: dict[str, Any] = {
            "parent_article_ref": article_ref,
            "product_ids": target_products,
            "content": content,
            "evidence_refs": evidence_refs,
            "content_hash": content_hash,
            "template_key": str(request.params.get("template_key") or "default"),
            "scored_total": total_score,
            "status": "draft",
        }
        record = await context.store_artifact(
            artifact_type="DraftArtifact",
            payload=payload,
            producer="full-draft-workflow",
            step_id="workflow",
        )

        return SkillResult.succeeded(
            self.name,
            artifact_refs=[str(record["ref"])],
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def _dimension_score(score: Any, attr: str) -> float:
        """返回 score_article 结果中某打分维度（product_relevance / event_impact）的分数。"""
        dimension = getattr(score, attr, None)
        return float(getattr(dimension, "score", 0.0) or 0.0)
