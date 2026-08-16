"""Schema-validating adapters and runner for business tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol

from agent.business_tools.contracts import (
    BusinessToolContract,
    BusinessToolRegistry,
    ToolRequestContext,
)
from agent.business_tools.models import (
    ArticleReference,
    CrawlNewsResult,
    DraftArtifact,
    ExportDraftResult,
    GenerateDraftResult,
    GetArticleResult,
    ListArticlesResult,
    ReviewDraftResult,
    SearchNewsResult,
    model_payload,
)
from pydantic import BaseModel


class BusinessToolAdapterKind(StrEnum):
    FAKE = "fake"
    RECORDED = "recorded"
    SANDBOX = "sandbox"
    PRODUCTION = "production"
    PRODUCTION_READONLY = "production_readonly"


class BusinessToolAdapter(Protocol):
    kind: BusinessToolAdapterKind

    async def invoke(
        self, contract: BusinessToolContract, args: dict[str, Any], context: ToolRequestContext
    ) -> Any: ...


def _key(name: str, args: dict[str, Any]) -> str:
    raw = json.dumps({"name": name, "args": args}, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ref(args: dict[str, Any], field: str = "article") -> ArticleReference:
    raw = args.get(field, args.get("article_id", "unknown"))
    if isinstance(raw, dict):
        return ArticleReference.model_validate(raw)
    return ArticleReference(article_id=str(raw))


def _fake_result(contract: BusinessToolContract, args: dict[str, Any]) -> dict[str, Any]:
    """Small deterministic fixtures; they intentionally contain no real article text."""
    name = contract.name
    ref = _ref(args)
    if name == "list_articles":
        return ListArticlesResult(items=[], total=0, replay_ref=_key(name, args)).model_dump()
    if name == "get_article":
        article_id = str(args.get("article_id") or "article-fake")
        return GetArticleResult(
            found=True,
            article={
                "article_id": article_id,
                "source_ref": f"fake://articles/{article_id}",
                "content_hash": "sha256:fake-article",
                "title": "Deterministic fake article",
                "content_available": True,
                "content": "Fixture content for a bounded production-runtime test.",
            },
        ).model_dump()
    if name == "search_news":
        return SearchNewsResult(items=[], total=0, replay_ref=_key(name, args), query=args.get("query", "")).model_dump()
    if name == "crawl_news":
        return CrawlNewsResult(task_ref="crawl-" + _key(name, args)[7:19], status="queued").model_dump()
    if name == "classify_article":
        return {"article": ref.model_dump(), "category": "unknown", "confidence": 0.0, "reason": "fake", "eligible": False, "model_version": "fake-v1", "prompt_version": "fake-v1"}
    if name == "match_products":
        return {
            "article": ref.model_dump(),
            "candidates": [
                {
                    "product_id": "agent-security",
                    "name": "Agent Security",
                    "confidence": 0.9,
                    "evidence": ["fake-catalog"],
                }
            ],
            "outcome": "matched",
            "catalog_hash": "sha256:fake-catalog",
        }
    if name == "score_article":
        dim = {"score": 0.0, "evidence": []}
        return {"article": ref.model_dump(), "product_relevance": dim, "event_impact": dim, "total_score": 0.0, "confidence": 0.0, "anomalies": [], "worth_writing": False, "user_requested_draft": bool(args.get("user_requested_draft")), "model_version": "fake-v1", "prompt_version": "fake-v1"}
    artifact = DraftArtifact(artifact_id="artifact-fake", version=1, content_hash="sha256:fake")
    if name == "generate_draft":
        return GenerateDraftResult(artifact=artifact, model_version="fake-v1", prompt_version="fake-v1", skill_version="fake-v1", context_hash="sha256:fake").model_dump()
    if name == "review_draft":
        return ReviewDraftResult(artifact=artifact, content_hash=artifact.content_hash, passed=True, reviewer_version="fake-v1").model_dump()
    if name == "revise_draft":
        review = ReviewDraftResult(artifact=artifact, content_hash=artifact.content_hash, passed=True, reviewer_version="fake-v1")
        return {"source_artifact": artifact.model_dump(), "artifact": artifact.model_dump(), "changed_sections": [], "review": review.model_dump()}
    if name == "save_draft_version":
        return {"artifact": artifact.model_dump(), "saved": True, "kind": args.get("kind", "autosave"), "duplicate": False}
    if name == "export_draft":
        return ExportDraftResult(artifact=artifact, export_ref="fake://export/" + artifact.artifact_id, format=args.get("format", "markdown"), content_hash=artifact.content_hash).model_dump()
    raise KeyError(name)


class FakeBusinessToolAdapter:
    kind = BusinessToolAdapterKind.FAKE

    def __init__(self, results: dict[str, Any] | None = None):
        self.results = dict(results or {})

    async def invoke(self, contract, args, context):
        value = self.results.get(contract.name)
        return value if value is not None else _fake_result(contract, args)


class RecordedBusinessToolAdapter:
    kind = BusinessToolAdapterKind.RECORDED

    def __init__(self, recordings: dict[str, Any] | None = None):
        self.recordings = dict(recordings or {})

    async def invoke(self, contract, args, context):
        key = _key(contract.name, args)
        if key not in self.recordings:
            raise KeyError(f"no recorded result for {contract.name} ({key[:16]})")
        return self.recordings[key]


class SandboxBusinessToolAdapter:
    kind = BusinessToolAdapterKind.SANDBOX

    def __init__(self, registry: BusinessToolRegistry | None = None):
        self.registry = registry

    async def invoke(self, contract, args, context):
        if contract.side_effect_level.value == "L3":
            raise PermissionError(f"sandbox rejects high-risk tool: {contract.name}")
        return _fake_result(contract, args)


Executor = Callable[[BusinessToolContract, dict[str, Any], ToolRequestContext], Awaitable[Any]]


class ProductionBusinessToolAdapter:
    kind = BusinessToolAdapterKind.PRODUCTION

    def __init__(self, executor: Executor | dict[str, Executor]):
        self.executor = executor

    async def invoke(self, contract, args, context):
        fn = self.executor.get(contract.name) if isinstance(self.executor, dict) else self.executor
        if fn is None:
            raise KeyError(f"no production executor for {contract.name}")
        return await fn(contract, args, context)


class ReadOnlyProductionBusinessToolAdapter(ProductionBusinessToolAdapter):
    """Use real production reads in shadow mode while rejecting every write."""

    kind = BusinessToolAdapterKind.PRODUCTION_READONLY

    async def invoke(self, contract, args, context):
        if contract.side_effect_level.value != "L1":
            raise PermissionError(f"shadow adapter rejects write tool: {contract.name}")
        return await super().invoke(contract, args, context)


class BusinessToolExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BusinessToolExecutor:
    """Validate all boundaries and make adapter execution interchangeable."""

    def __init__(
        self,
        registry: BusinessToolRegistry,
        adapters: dict[BusinessToolAdapterKind | str, BusinessToolAdapter] | None = None,
    ):
        self.registry = registry
        self.adapters = {str(k): v for k, v in (adapters or {}).items()}
        self._idempotency: dict[tuple[str, str, str], Any] = {}

    def adapter(self, kind: BusinessToolAdapterKind | str) -> BusinessToolAdapter:
        key = kind.value if isinstance(kind, BusinessToolAdapterKind) else str(kind)
        try:
            return self.adapters[key]
        except KeyError:
            raise BusinessToolExecutionError("adapter_unavailable", f"adapter unavailable: {key}") from None

    async def invoke(
        self,
        name: str,
        args: dict[str, Any] | Any,
        *,
        context: ToolRequestContext,
        adapter: BusinessToolAdapterKind | str = BusinessToolAdapterKind.PRODUCTION,
    ) -> BaseModel:
        contract = self.registry.get(name)
        if not contract.required_scopes.issubset(context.scopes):
            missing = sorted(contract.required_scopes - context.scopes)
            raise BusinessToolExecutionError("missing_scope", f"missing scopes: {', '.join(missing)}")
        if not context.tenant_id:
            raise BusinessToolExecutionError("tenant_required", "tenant boundary requires tenant_id")
        try:
            parsed_args = contract.args_schema.model_validate(model_payload(args))
        except Exception as exc:
            raise BusinessToolExecutionError("invalid_arguments", str(exc)) from exc
        payload = parsed_args.model_dump(mode="python")
        idem_key = ""
        if contract.idempotency_policy.required:
            idem_key = str(payload.get(contract.idempotency_policy.key_field, ""))
            if not idem_key:
                raise BusinessToolExecutionError("idempotency_required", "idempotency key is required")
            cache_key = (context.tenant_id, name, idem_key)
            if cache_key in self._idempotency:
                return contract.result_schema.model_validate(self._idempotency[cache_key])
        selected = self.adapter(adapter)
        attempts = contract.retry_policy.max_attempts
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                raw = await asyncio.wait_for(
                    selected.invoke(contract, payload, context), timeout=contract.timeout_seconds
                )
                result = contract.result_schema.model_validate(raw)
                if idem_key:
                    self._idempotency[(context.tenant_id, name, idem_key)] = result.model_dump(mode="python")
                return result
            except TimeoutError as exc:
                last = exc
                code = "timeout"
            except Exception as exc:
                last = exc
                code = type(exc).__name__
            if attempt + 1 < attempts and contract.retry_policy.backoff_seconds:
                await asyncio.sleep(contract.retry_policy.backoff_seconds * (attempt + 1))
        raise BusinessToolExecutionError(code, str(last)[:500]) from last
