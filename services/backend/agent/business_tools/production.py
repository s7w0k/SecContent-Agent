"""Production adapter that wraps existing article, scoring and draft services."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from typing import Any

from agent.business_tools.contracts import BusinessToolContract, ToolRequestContext
from agent.product_catalog import ProductCatalogService
from agent.product_matcher import ProductMatcher


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_published_at(value: Any) -> datetime | None:
    """DB 中部分文章 published_at 可能被写入损坏的拼接字符串，这里安全解析；
    无法解析则返回 None，避免候选构建被无效时间打断。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for loader, fmt in ((datetime.fromisoformat, None), (date.fromisoformat, "date")):
        try:
            parsed = loader(text)
        except (ValueError, TypeError):
            continue
        return datetime(parsed.year, parsed.month, parsed.day) if fmt else parsed
    return None


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if is_dataclass(value):
        return asdict(value)
    return dict(value) if isinstance(value, dict) else {"value": value}


# 用户类别词到安全域的归一（用户说“agent安全/AI安全/传统安全”等时用于域级比对）
_SECURITY_DOMAIN_KEYWORDS = {
    "agent安全": ("agent安全", "智能体安全", "agentic", "agent security", "智能体", "mcp", "a2a"),
    "AI安全": ("ai安全", "人工智能安全", "大模型安全", "llm安全", "生成式", "提示注入", "模型安全"),
    "传统安全": ("传统安全", "网络安全", "终端安全", "数据安全", "云安全", "等保"),
}


def _normalize_security_domain(text: str) -> str:
    lowered = (text or "").strip().lower()
    if not lowered:
        return ""
    for domain, keywords in _SECURITY_DOMAIN_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return domain
    return "未知"


_SIX_CATEGORIES = {
    "爆点事件",
    "法律法规/监管动态",
    "AI技术重大进展",
    "国内外竞品信息",
    "运营商/行业事件",
    "学术/会展/高校",
}


class ProductionBusinessToolService:
    """Keeps Mongo and concrete service signatures behind the Tool boundary."""

    ARTIFACTS = "agent_draft_artifacts"
    EXPORTS = "agent_draft_exports"
    PRIMARY = "agent_draft_primary"

    def __init__(
        self,
        *,
        db: Any,
        classifier: Any = None,
        scorer: Any = None,
        draft_generator: Any = None,
        draft_reviewer: Any = None,
        draft_chat: Any = None,
        crawl_client: Any = None,
        search_client: Any = None,
        product_catalog: ProductCatalogService | None = None,
        product_matcher: ProductMatcher | None = None,
    ):
        self.db = db
        self.classifier = classifier
        self.scorer = scorer
        self.draft_generator = draft_generator
        self.draft_reviewer = draft_reviewer
        self.draft_chat = draft_chat
        self.crawl_client = crawl_client
        self.search_client = search_client
        self.catalog = product_catalog or ProductCatalogService()
        self.matcher = product_matcher or ProductMatcher(self.catalog)

    async def __call__(
        self, contract: BusinessToolContract, args: dict[str, Any], context: ToolRequestContext
    ) -> Any:
        handler = getattr(self, f"_{contract.name}", None)
        if handler is None:
            raise NotImplementedError(f"production tool is not wired: {contract.name}")
        return await handler(args, context)

    @staticmethod
    def _scope(context: ToolRequestContext) -> dict[str, Any]:
        # Legacy shared articles have no tenant field; explicitly tenant-owned rows must match.
        return {
            "$or": [
                {"tenant_id": context.tenant_id},
                {"tenant_id": {"$exists": False}},
                {"tenant_id": ""},
            ]
        }

    async def _find_article(self, article_id: str, context: ToolRequestContext) -> dict[str, Any] | None:
        return await self.db["articles"].find_one(
            {"$and": [{"url_hash": article_id}, self._scope(context)]}
        )

    @staticmethod
    def _article_candidate(doc: dict[str, Any], *, include_content: bool = False) -> dict[str, Any]:
        article_id = str(doc.get("url_hash") or doc.get("article_id") or doc.get("_id") or "")
        content = str(doc.get("content_md") or doc.get("content") or "")
        source_ref = str(doc.get("url") or doc.get("source_url") or "")
        result = {
            "article_id": article_id,
            "source_ref": source_ref,
            "content_hash": _hash_text(content) if content else "",
            "title": str(doc.get("title") or "")[:500],
            "source": str(doc.get("source") or doc.get("source_name") or "")[:160],
            "published_at": _parse_published_at(doc.get("published_at") or doc.get("publish_time")),
            "summary": str(doc.get("summary_cn") or doc.get("summary") or "")[:2000],
            "content_available": bool(content),
            "untrusted_content": True,
            "score": doc.get("pr_total_score"),
        }
        if include_content:
            result["content"] = content[:100_000]
        return result

    async def _list_articles(self, args, context):
        query: dict[str, Any] = self._scope(context)
        filters: list[dict[str, Any]] = [query]
        if args.get("query"):
            safe = re.escape(args["query"][:200])
            filters.append({"$or": [{"title": {"$regex": safe, "$options": "i"}}, {"summary_cn": {"$regex": safe, "$options": "i"}}]})
        if args.get("source"):
            filters.append({"source": args["source"]})
        if args.get("category"):
            filters.append({"category_v2": args["category"]})
        date_filter: dict[str, Any] = {}
        if args.get("published_from"):
            date_filter["$gte"] = args["published_from"]
        if args.get("published_to"):
            date_filter["$lte"] = args["published_to"]
        if date_filter:
            filters.append({"published_at": date_filter})
        effective = {"$and": filters}
        cursor = self.db["articles"].find(effective).sort("published_at", -1)
        # 先取充足的原始行，过滤掉 published_at 损坏/缺失的行（损坏行常为拼接乱串，
        # 且会被 Mongo 排到最前，若先 limit 后过滤会把有效行整批截掉），再按 limit 切片
        prefetch = max(args["limit"] * 3, 100)
        docs = await cursor.to_list(length=prefetch)
        docs = [
            doc
            for doc in docs
            if _parse_published_at(doc.get("published_at") or doc.get("publish_time")) is not None
            and (str(doc.get("content_md") or doc.get("content") or "").strip())
        ]
        docs = docs[: args["limit"]]
        items = [self._article_candidate(doc) for doc in docs]
        replay_ref = _hash_text("|".join(item["article_id"] for item in items))
        return {"items": items, "total": len(items), "replay_ref": replay_ref}

    async def _get_article(self, args, context):
        doc = await self._find_article(args["article_id"], context)
        return {
            "found": doc is not None,
            "article": self._article_candidate(doc, include_content=args["include_content"])
            if doc
            else None,
        }

    async def _search_news(self, args, context):
        """候选新闻检索只查本地文章库；SearXNG 仅用于 LLM 生成时的知识补充。"""
        local = await self._list_articles({**args, "source": "", "category": ""}, context)
        candidates = list(local["items"])
        return {
            "query": args["query"],
            "items": candidates[: args["limit"]],
            "total": len(candidates),
            "replay_ref": _hash_text("|".join(item["article_id"] for item in candidates)),
        }

    async def _crawl_news(self, args, context):
        if self.crawl_client is None:
            raise RuntimeError("crawl client unavailable")
        days = 1
        if args.get("published_from"):
            days = max(1, min(30, (_now() - args["published_from"]).days + 1))
        response = await self.crawl_client.crawl_news(days)
        container = response.get("data") if isinstance(response.get("data"), dict) else response
        articles = container.get("articles") or container.get("items") or []
        if isinstance(articles, dict):
            articles = list(articles.values())
        saved = 0
        refs = []
        for item in articles[: args["max_results"]]:
            if not isinstance(item, dict):
                continue
            url_hash = str(item.get("url_hash") or "")
            if not url_hash:
                continue
            await self.db["articles"].update_one(
                {"url_hash": url_hash},
                {
                    "$set": {
                        "url_hash": url_hash,
                        "title": str(item.get("title") or "")[:500],
                        "url": str(item.get("url") or "")[:2000],
                        "source": str(item.get("source") or "")[:160],
                        "source_name": str(item.get("source") or "")[:160],
                        "source_type": str(item.get("source_type") or "overseas_news")[:40],
                        "summary_cn": str(item.get("summary") or "")[:500],
                        "content_md": str(item.get("content_md") or "")[:50_000],
                        "published_at": item.get("published_at") or None,
                        "added_at": _now(),
                        "updated_at": _now(),
                    }
                },
                upsert=True,
            )
            saved += 1
            refs.append({"article_id": url_hash, "source_ref": str(item.get("url") or "")})
        added_raw = container.get("saved", saved)
        updated_raw = container.get("updated", 0)
        skipped_raw = container.get("skipped", 0)
        failed_raw = container.get("failed", 0)
        return {
            "task_ref": "crawl-" + hashlib.sha256(args["idempotency_key"].encode()).hexdigest()[:20],
            "status": "completed",
            "added": int(added_raw) if added_raw is not None else len(refs),
            "updated": int(updated_raw) if updated_raw is not None else 0,
            "skipped": int(skipped_raw) if skipped_raw is not None else 0,
            "failed": int(failed_raw) if failed_raw is not None else 0,
            "articles": refs,
            "errors": list(response.get("errors") or []),
        }

    async def _classify_article(self, args, context):
        if self.classifier is None:
            raise RuntimeError("classifier unavailable")
        ref = args["article"]
        article = await self._find_article(ref["article_id"], context)
        if article is None:
            raise KeyError("article not found")
        result = _plain(await self.classifier.classify_single(article, user_id=context.user_id, task_id=context.run_id))
        confidence = float(result.get("confidence", 0))
        if confidence > 1:
            confidence /= 100
        category = str(result.get("category") or "unknown")
        security_domain = str(result.get("security_domain") or "未知")
        user_category = args.get("user_category") or ""
        # 用户指定的类别若能归一到安全域（agent安全/AI安全/传统安全），
        # 则在安全域层面比对；若是六分类词则按六分类比对；
        # 自由主题（如“APT 攻击”）不做类别比对，由检索过滤与 eligible 把关。
        user_domain = _normalize_security_domain(user_category)
        conflict = ""
        if user_domain and user_domain != "未知":
            if security_domain != "未知" and user_domain != security_domain:
                conflict = f"user={user_category}(域:{user_domain}); model域={security_domain}"
        elif user_category in _SIX_CATEGORIES and user_category != category:
            conflict = f"user={user_category}; model={category}"
        return {
            "article": ref,
            "category": category,
            "security_domain": security_domain,
            "confidence": confidence,
            "reason": str(result.get("reason") or "")[:1000],
            "eligible": bool(result.get("is_relevant", category != "不相关")),
            "conflict": conflict,
            "model_version": str(result.get("model_version") or "classifier-v2"),
            "prompt_version": str(result.get("prompt_version") or "classifier-v2"),
        }

    async def _match_products(self, args, context):
        ref = args["article"]
        article = await self._find_article(ref["article_id"], context)
        if article is None:
            raise KeyError("article not found")
        explicit = args.get("explicit_product_ids") or []
        candidates = []
        if explicit:
            products = self.catalog.validate_product_ids(explicit, purpose="draft")
            candidates = [{"product_id": p.product_id, "name": p.name, "confidence": 1.0, "evidence": ["explicit user selection"], "user_selected": True} for p in products]
        else:
            matches = self.matcher.match_by_rules(article, top_n=args["max_candidates"])
            candidates = [{"product_id": match.product_id, "name": match.product_name, "confidence": match.match_score / 100, "evidence": [match.match_reason]} for match in matches]
        outcome = "no_related_product" if not candidates else ("ambiguous" if self.matcher.is_ambiguous(self.matcher.match_by_rules(article, top_n=args["max_candidates"])) and not explicit else "matched")
        return {"article": ref, "candidates": candidates, "outcome": outcome, "catalog_hash": self.catalog.catalog_hash()}

    async def _score_article(self, args, context):
        if self.scorer is None:
            raise RuntimeError("scorer unavailable")
        ref = args["article"]
        article = await self._find_article(ref["article_id"], context)
        if article is None:
            raise KeyError("article not found")
        products = self.catalog.validate_product_ids(args["product_ids"], purpose="score")
        raw = await self.scorer.score_single(article, products=[{"product_id": p.product_id, "product_name": p.name} for p in products], user_id=context.user_id, task_id=context.run_id)
        product = float(raw.get("product_relevance", raw.get("relevance", 0)))
        impact = float(raw.get("event_impact", 0))
        total = float(raw.get("pr_total_score", raw.get("total_score", product + impact)))
        return {"article": ref, "product_relevance": {"score": product, "evidence": [str(raw.get("reason") or "")]}, "event_impact": {"score": impact, "evidence": [str(raw.get("impact_reason") or raw.get("reason") or "")]}, "total_score": total, "confidence": 0.0 if raw.get("_fallback") else 0.8, "anomalies": ["fallback"] if raw.get("_fallback") else [], "worth_writing": bool(raw.get("is_pr_candidate", total >= 80)), "user_requested_draft": args["user_requested_draft"], "model_version": str(raw.get("model_version") or "scorer-v2"), "prompt_version": str(raw.get("prompt_version") or args["skill_version"])}

    async def _artifact(self, artifact_id: str, context: ToolRequestContext) -> dict[str, Any]:
        doc = await self.db[self.ARTIFACTS].find_one({"artifact_id": artifact_id, "tenant_id": context.tenant_id, "user_id": context.user_id})
        if doc is None:
            raise KeyError("artifact not found")
        return doc

    async def _generate_draft(self, args, context):
        if self.draft_generator is None:
            raise RuntimeError("draft generator unavailable")
        existing = await self.db[self.ARTIFACTS].find_one({"tenant_id": context.tenant_id, "tool_idempotency_key": args["idempotency_key"]})
        if existing:
            return existing["tool_result"]
        ref = args["article"]
        article = await self._find_article(ref["article_id"], context)
        if article is None:
            raise KeyError("article not found")
        self.catalog.validate_product_ids(args["product_ids"], purpose="draft")
        generated = await self.draft_generator.generate(article, style_hints=args["tone"], user_business_prompt=args["angle"] or None, max_drafts=1)
        drafts = generated.get("drafts") or []
        content = str((drafts[0] if drafts else {}).get("content_md") or "").strip()
        if not content:
            raise ValueError("draft output is empty")
        artifact_id = "draft-" + uuid.uuid4().hex[:24]
        content_hash = _hash_text(content)
        artifact = {"artifact_id": artifact_id, "version": 1, "content_hash": content_hash, "status": "draft"}
        result = {"artifact": artifact, "summary": content[:300], "content": content, "evidence_refs": [ref["article_id"], *args["product_ids"]], "model_version": str(generated.get("model_version") or "draft-generator"), "prompt_version": str(generated.get("prompt_version") or args["template_key"]), "skill_version": "generate-draft.v1", "context_hash": _hash_text("|".join([ref["article_id"], *args["product_ids"], args["template_key"]]))}
        await self.db[self.ARTIFACTS].insert_one({**artifact, "root_artifact_id": artifact_id, "parent_artifact_id": "", "tenant_id": context.tenant_id, "user_id": context.user_id, "article_id": ref["article_id"], "product_ids": args["product_ids"], "content_md": content, "created_by": "agent", "instruction": "", "source_ids": [ref["article_id"], *args["product_ids"]], "tool_idempotency_key": args["idempotency_key"], "tool_result": result, "created_at": _now(), "updated_at": _now()})
        return result

    async def _review_document(self, doc, context):
        if self.draft_reviewer is None:
            raise RuntimeError("draft reviewer unavailable")
        article = await self._find_article(doc["article_id"], context)
        review = _plain(await self.draft_reviewer.review(article or {}, doc))
        issues = []
        for issue in review.get("issues", []):
            value = _plain(issue)
            severity = str(value.get("severity") or "warning").lower()
            severity = {
                "high": "error",
                "medium": "warning",
                "low": "info",
            }.get(severity, severity)
            if severity not in {"info", "warning", "error", "critical"}:
                severity = "warning"
            issues.append(
                {
                    "code": str(value.get("code") or value.get("type") or "review_issue"),
                    "severity": severity,
                    "message": str(
                        value.get("message") or value.get("description") or "review issue"
                    )[:1000],
                    "evidence_refs": list(value.get("evidence_refs") or []),
                }
            )
        artifact = {"artifact_id": doc["artifact_id"], "version": doc["version"], "content_hash": doc["content_hash"], "status": "reviewed" if not issues else "needs_review"}
        return {"artifact": artifact, "content_hash": doc["content_hash"], "passed": not issues, "issues": issues, "reviewer_version": str(review.get("reviewer_version") or "draft-reviewer-v1")}

    async def _review_draft(self, args, context):
        doc = await self._artifact(args["artifact"]["artifact_id"], context)
        if doc["version"] != args["artifact"]["version"] or doc["content_hash"] != args["artifact"]["content_hash"]:
            raise ValueError("artifact version or content hash conflict")
        review = await self._review_document(doc, context)
        await self.db[self.ARTIFACTS].update_one(
            {"artifact_id": doc["artifact_id"], "tenant_id": context.tenant_id},
            {"$set": {"review": review, "review_status": "review_passed" if review["passed"] else "needs_user_review", "status": review["artifact"]["status"], "updated_at": _now()}},
        )
        return review

    async def _revise_draft(self, args, context):
        if self.draft_chat is None:
            raise RuntimeError("draft revision service unavailable")
        source = await self._artifact(args["artifact"]["artifact_id"], context)
        if source["version"] != args["expected_version"]:
            raise ValueError("artifact version conflict")
        existing = await self.db[self.ARTIFACTS].find_one({"tenant_id": context.tenant_id, "tool_idempotency_key": args["idempotency_key"]})
        if existing:
            return existing["tool_result"]
        article = await self._find_article(source["article_id"], context)
        revised = await self.draft_chat.revise(args["instruction"], article or {}, source, selected_text=args.get("selection") or None)
        content = str(revised.get("revised_content_md") or "").strip()
        if not content:
            raise ValueError("revision output is empty")
        doc = {**source, "_id": None, "artifact_id": "draft-" + uuid.uuid4().hex[:24], "root_artifact_id": source.get("root_artifact_id") or source["artifact_id"], "version": source["version"] + 1, "content_md": content, "content_hash": _hash_text(content), "status": "draft", "parent_artifact_id": source["artifact_id"], "created_by": context.user_id, "instruction": args["instruction"], "tool_idempotency_key": args["idempotency_key"], "created_at": _now(), "updated_at": _now()}
        doc.pop("_id", None)
        review = await self._review_document(doc, context)
        doc["status"] = review["artifact"]["status"]
        doc["review"] = review
        doc["review_status"] = "review_passed" if review["passed"] else "needs_user_review"
        artifact = review["artifact"]
        result = {"source_artifact": args["artifact"], "artifact": artifact, "changed_sections": list(revised.get("change_summary") or []), "review": review}
        doc["tool_result"] = result
        await self.db[self.ARTIFACTS].insert_one(doc)
        return result

    async def _save_draft_version(self, args, context):
        doc = await self._artifact(args["artifact"]["artifact_id"], context)
        if doc["version"] != args["expected_version"]:
            raise ValueError("artifact version conflict")
        duplicate = doc.get("save_idempotency_key") == args["idempotency_key"]
        result = await self.db[self.ARTIFACTS].update_one({"artifact_id": doc["artifact_id"], "tenant_id": context.tenant_id, "version": args["expected_version"]}, {"$set": {"save_kind": args["kind"], "confirmed_by_user": args["confirmed_by_user"], "save_idempotency_key": args["idempotency_key"], "updated_at": _now()}})
        if args["kind"] == "business_version" and args["confirmed_by_user"]:
            root_id = doc.get("root_artifact_id") or doc["artifact_id"]
            await self.db[self.PRIMARY].update_one(
                {"tenant_id": context.tenant_id, "user_id": context.user_id, "root_artifact_id": root_id},
                {"$set": {"artifact_id": doc["artifact_id"], "artifact_version": doc["version"], "content_hash": doc["content_hash"], "idempotency_key": args["idempotency_key"], "updated_at": _now()}, "$setOnInsert": {"created_at": _now()}},
                upsert=True,
            )
        return {"artifact": args["artifact"], "saved": result.matched_count == 1, "kind": args["kind"], "duplicate": duplicate}

    async def _export_draft(self, args, context):
        doc = await self._artifact(args["artifact"]["artifact_id"], context)
        if doc["version"] != args["artifact"]["version"] or doc["content_hash"] != args["artifact"]["content_hash"]:
            raise ValueError("export requires an immutable artifact version")
        export_id = "export-" + hashlib.sha256(f"{context.tenant_id}:{args['idempotency_key']}".encode()).hexdigest()[:24]
        await self.db[self.EXPORTS].update_one({"export_id": export_id, "tenant_id": context.tenant_id}, {"$setOnInsert": {"export_id": export_id, "tenant_id": context.tenant_id, "user_id": context.user_id, "artifact_id": doc["artifact_id"], "artifact_version": doc["version"], "content_hash": doc["content_hash"], "format": args["format"], "filename": args["filename"], "created_at": _now()}}, upsert=True)
        return {"artifact": args["artifact"], "export_ref": f"agent-export://{export_id}/{args['filename']}.{args['format']}", "format": args["format"], "content_hash": doc["content_hash"], "immutable": True}
