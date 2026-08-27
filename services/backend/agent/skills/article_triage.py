"""ArticleTriageSkill - 文章分诊技能（阶段二 §19）。

读取已入库文章 → 调用分类 Tool → 生成 TriageArtifact，
判断其类别、相关度、eligibility 以及是否需要拉取全文。

产物契约：输出 `TriageArtifact`，字段见 TriageArtifact payload 文档。
"""

from __future__ import annotations

from typing import Any

from agent.skills.context import SkillExecutionContext
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry

ARTICLE_REF_KEY = "article_ref"


def _build_manifest() -> SkillManifest:
    """构造 ArticleTriageSkill 的显式 SkillManifest。

    作为 register() / build_manifests() 的单一来源，避免清单漂移。
    """
    return SkillManifest(
        name=ArticleTriageSkill.name,
        version=ArticleTriageSkill.version,
        description=ArticleTriageSkill.description,
        purpose=ArticleTriageSkill.purpose,
        required_tools=ArticleTriageSkill.required_tools,
        risk_level=ArticleTriageSkill.risk_level,
        required_scopes=frozenset(ArticleTriageSkill.required_scopes),
        output_artifact_type=ArticleTriageSkill.output_artifact_type,
    )


def build_manifests() -> list[SkillManifest]:
    """返回本模块内所有 Skill 的清单（当前仅一个）。"""
    return [_build_manifest()]


def register(registry: ExecutableSkillRegistry) -> None:
    """把该 Skill 及其显式清单注册进 ExecutableSkillRegistry。"""
    registry.register(ArticleTriageSkill(), _build_manifest())


class ArticleTriageSkill:
    """分诊：判定文章类别、相关度、eligibility 与是否需全文（计划 §19）。"""

    name = "article-triage"
    version = "1.0.0"
    description = "对已入库文章执行分诊：判定类别、相关度、eligibility 与是否需要拉取全文。"
    purpose = "triage"
    risk_level = "low"
    required_scopes = frozenset({"articles:read", "articles:classify"})
    required_tools = ("get_article", "classify_article")
    output_artifact_type = "TriageArtifact"

    async def execute(
        self,
        request: SkillRequest,
        context: SkillExecutionContext,
    ) -> SkillResult:
        article_ref = request.input_refs.get(ARTICLE_REF_KEY) or str(
            request.params.get(ARTICLE_REF_KEY, "")
        )
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

        article = get_result.article
        category_override = str(request.params.get("category_override") or "")

        category = ""
        confidence: float = 0.0
        reasons: list[str] = []
        if category_override:
            category = category_override
            confidence = 1.0
            reasons.append(f"category_override: {category_override}")
        else:
            classify = await context.execute_tool(
                "classify_article",
                {
                    "article": {"article_id": article.article_id},
                    "user_category": str(request.params.get("user_category") or ""),
                },
            )
            category = str(classify.category or "")
            if not category:
                return SkillResult.partial(
                    self.name,
                    error_code="classify_failed",
                    message=f"分类失败，未得到合法 category: {article_ref}",
                )
            confidence = float(classify.confidence or 0.0)
            if getattr(classify, "reason", ""):
                reasons.append(f"classify: {classify.reason}")

        eligible = bool(getattr(classify, "eligible", False)) if not category_override else True
        needs_fulltext = eligible and not bool(getattr(article, "content_available", False))
        reasons.append(f"eligible: {eligible}")
        if needs_fulltext:
            reasons.append("needs_fulltext: 文章未含全文内容")

        payload: dict[str, Any] = {
            "article_ref": article_ref,
            "category": category,
            "confidence": confidence,
            "eligible": eligible,
            "needs_fulltext": needs_fulltext,
            "reasons": reasons,
            "producer": "article-triage",
        }
        record = await context.store_artifact(
            artifact_type="TriageArtifact",
            payload=payload,
            producer="article-triage",
            step_id="triage",
        )

        next_recommendations = ["score"] if eligible else ["reject"]
        return SkillResult.succeeded(
            self.name,
            artifact_refs=[str(record["ref"])],
            next_recommendations=next_recommendations,
        )
