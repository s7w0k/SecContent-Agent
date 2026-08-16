"""Business tool contracts, registry snapshots and compatibility checks."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from agent.business_tools import models
from agent.harness.tool_harness import SideEffectLevel, ToolContract, ToolRegistry
from pydantic import BaseModel, ConfigDict, Field, model_validator

BUSINESS_TOOL_REGISTRY_VERSION = "2.0"


class ToolRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TenantBoundary(StrEnum):
    TENANT = "tenant"
    USER = "user"
    PUBLIC_READ = "public_read"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0.0, ge=0, le=60)
    retry_on: tuple[str, ...] = ("timeout", "rate_limit", "upstream_5xx")


class IdempotencyPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    required: bool = False
    key_field: str = ""
    scope: str = "tenant_tool"

    @model_validator(mode="after")
    def validate_key_field(self) -> IdempotencyPolicy:
        if self.required and not self.key_field:
            raise ValueError("required idempotency policy needs key_field")
        return self


class CachePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = False
    ttl_seconds: int = Field(default=0, ge=0, le=86_400)
    vary_by: tuple[str, ...] = ("tenant_id",)


class CompensationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reversible: bool = False
    action: str = ""
    irreversible_reason: str = "read_only"

    @model_validator(mode="after")
    def validate_description(self) -> CompensationPolicy:
        if self.reversible and not self.action:
            raise ValueError("reversible tool needs a compensating action")
        if not self.reversible and not self.irreversible_reason:
            raise ValueError("non-reversible tool needs an explanation")
        return self


class ToolRequestContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(..., min_length=1, max_length=100)
    tenant_id: str = Field(..., min_length=1, max_length=100)
    scopes: frozenset[str] = Field(default_factory=frozenset)
    run_id: str = Field(default="", max_length=100)
    turn_id: str = Field(default="", max_length=100)


class BusinessToolContract(BaseModel):
    """Immutable, executable contract shared by Runtime and Harness adapters."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(..., pattern=r"^\d+\.\d+(?:\.\d+)?$")
    description: str = Field(..., min_length=1, max_length=500)
    args_schema: type[BaseModel]
    result_schema: type[BaseModel]
    risk_level: ToolRiskLevel
    side_effect_level: SideEffectLevel
    timeout_seconds: float = Field(..., gt=0, le=900)
    retry_policy: RetryPolicy
    idempotency_policy: IdempotencyPolicy
    required_scopes: frozenset[str] = Field(..., min_length=1)
    tenant_boundary: TenantBoundary
    cache_policy: CachePolicy
    evidence_fields: tuple[str, ...] = Field(..., min_length=1)
    compensation: CompensationPolicy
    adapters: frozenset[str] = Field(
        default_factory=lambda: frozenset({"fake", "recorded", "sandbox", "production"})
    )

    @model_validator(mode="after")
    def validate_contract(self) -> BusinessToolContract:
        expected = {"fake", "recorded", "sandbox", "production"}
        if self.adapters != expected:
            raise ValueError(f"all execution adapters are required: {sorted(expected)}")
        if self.retry_policy.max_attempts > 1:
            safe_retry = self.side_effect_level == SideEffectLevel.L1
            if not safe_retry and not self.idempotency_policy.required:
                raise ValueError("retryable write tools require idempotency")
        result_fields = set(self.result_schema.model_fields)
        missing = set(self.evidence_fields) - result_fields
        if missing:
            raise ValueError(f"evidence fields missing from result schema: {sorted(missing)}")
        return self

    @property
    def qualified_name(self) -> str:
        return f"{self.name}@{self.version}"

    def to_harness_contract(self) -> ToolContract:
        return ToolContract(
            name=self.name,
            description=self.description,
            args_schema=self.args_schema.model_json_schema(),
            args_model=self.args_schema,
            result_model=self.result_schema,
            side_effect_level=self.side_effect_level,
            risk_level=self.risk_level.value,
            idempotency_required=self.idempotency_policy.required,
            timeout_seconds=self.timeout_seconds,
            retryable=self.retry_policy.max_attempts > 1,
            required_scopes=tuple(sorted(self.required_scopes)),
            tenant_boundary=self.tenant_boundary.value,
            cache_policy=self.cache_policy.model_dump(mode="json"),
            evidence_fields=self.evidence_fields,
            compensating_action=(
                self.compensation.action or self.compensation.irreversible_reason
            ),
            registry_version=self.version,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "args_schema": self.args_schema.model_json_schema(),
            "result_schema": self.result_schema.model_json_schema(),
            "risk_level": self.risk_level.value,
            "side_effect_level": self.side_effect_level.value,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": self.retry_policy.model_dump(mode="json"),
            "idempotency_policy": self.idempotency_policy.model_dump(mode="json"),
            "required_scopes": sorted(self.required_scopes),
            "tenant_boundary": self.tenant_boundary.value,
            "cache_policy": self.cache_policy.model_dump(mode="json"),
            "evidence_fields": list(self.evidence_fields),
            "compensation": self.compensation.model_dump(mode="json"),
            "adapters": sorted(self.adapters),
        }


class BreakingContractChange(BaseModel):
    tool_name: str
    path: str
    reason: str


class BusinessToolRegistry:
    def __init__(self, *, version: str = BUSINESS_TOOL_REGISTRY_VERSION):
        self.version = version
        self._contracts: dict[str, BusinessToolContract] = {}
        self.harness_registry = ToolRegistry(version=version)

    def register(self, contract: BusinessToolContract) -> None:
        if contract.name in self._contracts:
            raise ValueError(f"duplicate business tool: {contract.name}")
        self._contracts[contract.name] = contract
        self.harness_registry.register(contract.to_harness_contract())

    def get(self, name: str) -> BusinessToolContract:
        try:
            return self._contracts[name]
        except KeyError:
            raise KeyError(f"unknown business tool: {name}") from None

    def names(self) -> list[str]:
        return sorted(self._contracts)

    def snapshot(self) -> dict[str, Any]:
        payload = {
            "registry_version": self.version,
            "tools": {name: value.snapshot() for name, value in sorted(self._contracts.items())},
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return {**payload, "fingerprint": "sha256:" + hashlib.sha256(raw.encode()).hexdigest()}

    @property
    def manifest_version(self) -> str:
        return f"{self.version}:{self.snapshot()['fingerprint']}"


def detect_breaking_changes(
    previous: dict[str, Any], current: dict[str, Any]
) -> list[BreakingContractChange]:
    """Conservative detector: removals and tightened input/changed output are breaking."""
    changes: list[BreakingContractChange] = []
    old_tools = previous.get("tools", {})
    new_tools = current.get("tools", {})
    for name, old in old_tools.items():
        new = new_tools.get(name)
        if new is None:
            changes.append(
                BreakingContractChange(tool_name=name, path="tool", reason="tool removed")
            )
            continue
        old_required = set(old.get("args_schema", {}).get("required", []))
        new_required = set(new.get("args_schema", {}).get("required", []))
        for field in sorted(new_required - old_required):
            changes.append(
                BreakingContractChange(
                    tool_name=name,
                    path=f"args.{field}",
                    reason="new required argument",
                )
            )
        if old.get("result_schema") != new.get("result_schema"):
            changes.append(
                BreakingContractChange(
                    tool_name=name,
                    path="result_schema",
                    reason="result schema changed",
                )
            )
        if not set(old.get("required_scopes", [])).issuperset(
            set(new.get("required_scopes", []))
        ):
            changes.append(
                BreakingContractChange(
                    tool_name=name,
                    path="required_scopes",
                    reason="required scopes were tightened",
                )
            )
    return changes


def _contract(
    name: str,
    description: str,
    args_schema: type[BaseModel],
    result_schema: type[BaseModel],
    *,
    scope: str,
    evidence_fields: tuple[str, ...],
    side_effect: SideEffectLevel = SideEffectLevel.L1,
    risk: ToolRiskLevel = ToolRiskLevel.LOW,
    timeout: float = 30,
    attempts: int = 2,
    idempotency_key: str = "",
    cache_ttl: int = 0,
    compensation: CompensationPolicy | None = None,
) -> BusinessToolContract:
    return BusinessToolContract(
        name=name,
        version="1.0.0",
        description=description,
        args_schema=args_schema,
        result_schema=result_schema,
        risk_level=risk,
        side_effect_level=side_effect,
        timeout_seconds=timeout,
        retry_policy=RetryPolicy(max_attempts=attempts),
        idempotency_policy=IdempotencyPolicy(
            required=bool(idempotency_key), key_field=idempotency_key
        ),
        required_scopes=frozenset({scope}),
        tenant_boundary=TenantBoundary.TENANT,
        cache_policy=CachePolicy(enabled=cache_ttl > 0, ttl_seconds=cache_ttl),
        evidence_fields=evidence_fields,
        compensation=compensation or CompensationPolicy(),
    )


def build_business_tool_registry() -> BusinessToolRegistry:
    registry = BusinessToolRegistry()
    definitions = [
        _contract("list_articles", "List tenant-visible article summaries", models.ListArticlesArgs, models.ListArticlesResult, scope="articles:read", evidence_fields=("items", "replay_ref"), cache_ttl=60),
        _contract("get_article", "Load one tenant-visible article by reference", models.GetArticleArgs, models.GetArticleResult, scope="articles:read", evidence_fields=("article",), cache_ttl=300),
        _contract("search_news", "Search local and web news as normalized candidates", models.SearchNewsArgs, models.SearchNewsResult, scope="news:search", evidence_fields=("items", "replay_ref"), timeout=45, cache_ttl=300),
        _contract("crawl_news", "Queue an idempotent bounded news crawl", models.CrawlNewsArgs, models.CrawlNewsResult, scope="news:crawl", evidence_fields=("task_ref", "articles"), side_effect=SideEffectLevel.L2, risk=ToolRiskLevel.MEDIUM, timeout=30, attempts=3, idempotency_key="idempotency_key", compensation=CompensationPolicy(irreversible_reason="crawl writes are deduplicated; individual articles can be archived")),
        _contract("classify_article", "Classify an immutable article reference", models.ClassifyArticleArgs, models.ClassifyArticleResult, scope="articles:classify", evidence_fields=("article", "model_version", "prompt_version"), timeout=60, cache_ttl=3600),
        _contract("match_products", "Match only published and authorized products", models.MatchProductsArgs, models.MatchProductsResult, scope="products:read", evidence_fields=("article", "candidates", "catalog_hash"), cache_ttl=900),
        _contract("score_article", "Score product relevance and event impact", models.ScoreArticleArgs, models.ScoreArticleResult, scope="articles:score", evidence_fields=("article", "product_relevance", "event_impact", "model_version", "prompt_version"), timeout=90, cache_ttl=3600),
        _contract("generate_draft", "Generate a new traceable draft artifact version", models.GenerateDraftArgs, models.GenerateDraftResult, scope="drafts:write", evidence_fields=("artifact", "evidence_refs", "context_hash"), side_effect=SideEffectLevel.L2, risk=ToolRiskLevel.MEDIUM, timeout=180, attempts=2, idempotency_key="idempotency_key", compensation=CompensationPolicy(reversible=True, action="archive_generated_artifact", irreversible_reason="")),
        _contract("review_draft", "Review an immutable draft content hash", models.ReviewDraftArgs, models.ReviewDraftResult, scope="drafts:review", evidence_fields=("artifact", "content_hash", "issues"), timeout=90, cache_ttl=3600),
        _contract("revise_draft", "Create and re-review a new draft version", models.ReviseDraftArgs, models.ReviseDraftResult, scope="drafts:write", evidence_fields=("source_artifact", "artifact", "review"), side_effect=SideEffectLevel.L2, risk=ToolRiskLevel.MEDIUM, timeout=180, attempts=2, idempotency_key="idempotency_key", compensation=CompensationPolicy(reversible=True, action="archive_revised_artifact", irreversible_reason="")),
        _contract("save_draft_version", "Optimistically save an autosave or confirmed business version", models.SaveDraftVersionArgs, models.SaveDraftVersionResult, scope="drafts:write", evidence_fields=("artifact",), side_effect=SideEffectLevel.L2, risk=ToolRiskLevel.MEDIUM, timeout=30, attempts=3, idempotency_key="idempotency_key", compensation=CompensationPolicy(reversible=True, action="archive_saved_version", irreversible_reason="")),
        _contract("export_draft", "Export one immutable draft version using a safe filename", models.ExportDraftArgs, models.ExportDraftResult, scope="drafts:export", evidence_fields=("artifact", "export_ref", "content_hash"), side_effect=SideEffectLevel.L2, risk=ToolRiskLevel.MEDIUM, timeout=120, attempts=2, idempotency_key="idempotency_key", compensation=CompensationPolicy(reversible=True, action="delete_export_copy", irreversible_reason="")),
    ]
    for definition in definitions:
        registry.register(definition)
    return registry
