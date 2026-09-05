"""Normalize every business Tool result before Planner or Validator sees it."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Protocol

from agent.business_tools.contracts import BusinessToolContract
from agent.business_tools.execution import BusinessToolExecutionError
from pydantic import BaseModel, ConfigDict, Field

_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)\s*[:=]\s*[^\s,;]+"
)


class ObservationStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class NormalizedObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ObservationStatus
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    retryable: bool = False
    reason_code: str = ""
    artifact_ref: str = ""
    result_hash: str = ""


class ArtifactStore(Protocol):
    async def put(self, *, content: str, content_hash: str) -> str: ...


class InMemoryArtifactStore:
    def __init__(self):
        self.items: dict[str, str] = {}

    async def put(self, *, content: str, content_hash: str) -> str:
        ref = "artifact://tool-results/" + content_hash.removeprefix("sha256:")
        self.items[ref] = content
        return ref


def _safe_message(value: Any) -> str:
    text = _SECRET_PATTERN.sub(r"\1=***redacted***", str(value))
    return text[:500]


class ObservationNormalizer:
    def __init__(self, artifact_store: ArtifactStore | None = None, *, inline_limit: int = 8000):
        self.artifact_store = artifact_store or InMemoryArtifactStore()
        self.inline_limit = max(1000, inline_limit)

    async def success(
        self, contract: BusinessToolContract, result: BaseModel | dict[str, Any]
    ) -> NormalizedObservation:
        payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else dict(result)
        raw = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
        result_hash = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        evidence: list[str] = []
        for field in contract.evidence_fields:
            value = payload.get(field)
            if value not in (None, "", [], {}):
                encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
                evidence.append(
                    f"{contract.name}:{field}:sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
                )

        partial = (
            str(payload.get("status", "")).lower() == "partial"
            or bool(payload.get("failed", 0) and payload.get("added", 0))
            or bool(payload.get("errors") and evidence)
        )
        artifact_ref = ""
        warnings: list[str] = []
        data = payload
        if len(raw) > self.inline_limit:
            artifact_ref = await self.artifact_store.put(content=raw, content_hash=result_hash)
            data = {
                "summary": f"{contract.name} result stored as artifact",
                "field_count": len(payload),
            }
            warnings.append("result_truncated_to_artifact")
        return NormalizedObservation(
            status=ObservationStatus.PARTIAL if partial else ObservationStatus.OK,
            ok=True,
            data=data,
            evidence=evidence,
            warnings=warnings,
            retryable=False,
            reason_code="partial_success" if partial else "ok",
            artifact_ref=artifact_ref,
            result_hash=result_hash,
        )

    def failure(self, error: Exception) -> NormalizedObservation:
        if isinstance(error, BusinessToolExecutionError):
            code = error.code
        elif isinstance(error, TimeoutError):
            code = "timeout"
        elif isinstance(error, PermissionError):
            code = "policy_denied"
        elif isinstance(error, KeyError):
            code = "not_found"
        else:
            code = "tool_internal_error"
        retryable = code in {"timeout", "rate_limit", "upstream_5xx", "ConnectionError"}
        message_hash = hashlib.sha256(_safe_message(error).encode("utf-8")).hexdigest()
        return NormalizedObservation(
            status=ObservationStatus.FAILED,
            ok=False,
            warnings=["tool execution failed"],
            retryable=retryable,
            reason_code=code,
            result_hash="sha256:" + message_hash,
        )
