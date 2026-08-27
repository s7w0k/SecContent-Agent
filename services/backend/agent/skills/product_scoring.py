"""ProductScoringSkill - 产品评分技能（阶段二 §21）。

对一篇文章匹配已授权产品 → 逐产品收集已验证证据（LLM Wiki）→
证据充分的产品进入 score_article 打分，产出 ScoringArtifact。

安全不变量：
  - 证据不充分 / 冲突 / 失败的候选产品直接记为 NO_SCORE，绝不进入打分；
  - 一切工具调用均经 context.execute_tool 白名单 + 预算边界。
"""

from __future__ import annotations

from typing import Any

from agent.skills.context import SkillExecutionContext
from agent.skills.contracts import SkillManifest, SkillRequest, SkillResult
from agent.skills.executable_registry import ExecutableSkillRegistry

ARTICLE_REF_KEY = "article_ref"
SCORE_VERSION = "score.v2"
SCORED = "SCORED"
NO_SCORE = "NO_SCORE"


def _build_manifest() -> SkillManifest:
    """构造 ProductScoringSkill 的显式 SkillManifest。"""
    return SkillManifest(
        name=ProductScoringSkill.name,
        version=ProductScoringSkill.version,
        description=ProductScoringSkill.description,
        purpose=ProductScoringSkill.purpose,
        required_tools=ProductScoringSkill.required_tools,
        risk_level=ProductScoringSkill.risk_level,
        required_scopes=frozenset(ProductScoringSkill.required_scopes),
        output_artifact_type=ProductScoringSkill.output_artifact_type,
    )


def build_manifests() -> list[SkillManifest]:
    """返回本模块内所有 Skill 的清单（当前仅一个）。"""
    return [_build_manifest()]


def register(registry: ExecutableSkillRegistry) -> None:
    """把该 Skill 及其显式清单注册进 ExecutableSkillRegistry。"""
    registry.register(ProductScoringSkill(), _build_manifest())


class ProductScoringSkill:
    """产品评分：匹配产品 → 收集证据 → 逐产品打分（计划 §21）。"""

    name = "product-scoring"
    version = "1.0.0"
    description = "对文章匹配已授权产品并逐个打分，产出 ScoringArtifact（证据不足则跳过打分）。"
    purpose = "score"
    risk_level = "low"
    required_scopes = frozenset(
        {"articles:read", "products:read", "evidence:read", "articles:score"}
    )
    required_tools = ("match_products", "collect_product_evidence", "score_article")
    output_artifact_type = "ScoringArtifact"

    async def execute(
        self,
        request: SkillRequest,
        context: SkillExecutionContext,
    ) -> SkillResult:
        article_ref = request.input_refs.get(ARTICLE_REF_KEY) or str(
            request.params.get(ARTICLE_REF_KEY, "")
        )
        query = str(request.params.get("query") or "")
        max_candidates = int(request.params.get("max_candidates", 2))

        match = await context.execute_tool(
            "match_products",
            {
                "article": {"article_id": article_ref},
                "explicit_product_ids": list(request.params.get("explicit_product_ids") or []),
                "max_candidates": max_candidates,
            },
        )

        products: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        evidence_bundle_refs: list[str] = []
        any_scored = False
        any_insufficient = False
        any_conflicted = False

        for candidate in match.candidates:
            collect = await context.execute_tool(
                "collect_product_evidence",
                {
                    "query": query,
                    "product_ids": [candidate.product_id],
                    "task_type": "score",
                },
            )
            evidence_ids.extend(list(getattr(collect, "evidence_ids", []) or []))
            bundle_ref = str(getattr(collect, "evidence_bundle_ref", "") or "")
            if bundle_ref:
                evidence_bundle_refs.append(bundle_ref)
            collect_status = str(collect.status)

            product_entry: dict[str, Any] = {
                "product_id": candidate.product_id,
                "product_name": candidate.name,
                "relevance": 0.0,
                "event_impact": 0.0,
                "evidence_ref": bundle_ref,
                "status": NO_SCORE,
            }

            if collect_status != "SUFFICIENT":
                if collect_status == "INSUFFICIENT_EVIDENCE":
                    any_insufficient = True
                elif collect_status in ("CONFLICTED", "FAILED"):
                    any_conflicted = True
                products.append(product_entry)
                continue

            score = await context.execute_tool(
                "score_article",
                {
                    "article": {"article_id": article_ref},
                    "product_ids": [candidate.product_id],
                    "skill_version": SCORE_VERSION,
                },
            )
            any_scored = True
            product_entry["relevance"] = float(score.product_relevance.score or 0.0)
            product_entry["event_impact"] = float(score.event_impact.score or 0.0)
            product_entry["status"] = SCORED
            products.append(product_entry)

        best_product_id = self._pick_best(products)
        (
            product_relevance,
            event_impact,
            pr_total_score,
        ) = self._best_scores(products, best_product_id)

        if any_scored:
            status = "SCORED"
        elif not any_insufficient and not any_conflicted:
            status = "INSUFFICIENT_EVIDENCE"
        else:
            status = "CONFLICTED_EVIDENCE" if any_conflicted else "INSUFFICIENT_EVIDENCE"

        payload: dict[str, Any] = {
            "article_ref": article_ref,
            "products": products,
            "best_product_id": best_product_id,
            "product_relevance": product_relevance,
            "event_impact": event_impact,
            "pr_total_score": pr_total_score,
            "evidence_bundle_refs": evidence_bundle_refs,
            "status": status,
        }
        record = await context.store_artifact(
            artifact_type="ScoringArtifact",
            payload=payload,
            producer="product-scoring",
            step_id="score",
        )

        next_recommendations = ["draft"] if best_product_id and pr_total_score >= 80 else ["end"]

        return SkillResult.succeeded(
            self.name,
            artifact_refs=[str(record["ref"])],
            evidence_refs=evidence_ids,
            next_recommendations=next_recommendations,
        )

    @staticmethod
    def _pick_best(products: list[dict[str, Any]]) -> str:
        """在已打分产品中选取 pr_total 最高的产品；无打分产品返回空串。"""
        scored = [p for p in products if p["status"] == SCORED]
        if not scored:
            return ""
        best = str(scored[0]["product_id"])
        best_total = float(scored[0]["relevance"]) + float(scored[0]["event_impact"])
        for product in scored[1:]:
            total = float(product["relevance"]) + float(product["event_impact"])
            if total > best_total:
                best_total = total
                best = str(product["product_id"])
        return best

    @staticmethod
    def _best_scores(
        products: list[dict[str, Any]],
        best_product_id: str,
    ) -> tuple[float, float, float]:
        """返回 best 产品的 product_relevance / event_impact / pr_total_score。"""
        for product in products:
            if product["status"] == SCORED and product["product_id"] == best_product_id:
                relevance = float(product["relevance"])
                impact = float(product["event_impact"])
                return relevance, impact, relevance + impact
        return 0.0, 0.0, 0.0
