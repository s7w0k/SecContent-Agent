"""Tool Harness — 阶段4 §1.1（WBS 4.1）。

统一工具契约注册（ToolRegistry：Schema/版本/权限/side-effect/费用/超时/重试标记）、
四种执行适配器（fake / recorded / sandbox / production）、
结果净化（大小/编码/注入/敏感信息）、录制-重放轨迹（只存 hash 不存正文）
以及统一 timeout / retry / circuit breaker / telemetry 包装（ProtectedToolCaller）。

安全约束：
  - 参数与结果不入库，只存 hash 与状态（对齐统一事件契约）；
  - L3 高风险工具默认禁止通过 Harness 直接执行（需审批链）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger("backend.agent.harness.tool_harness")

DEFAULT_MAX_RESULT_BYTES = 1_048_576  # 1 MiB

# 常见敏感信息模式（脱敏用，覆盖 AK/SK/密钥/邮箱/手机号/URL 内嵌凭据）
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ak", re.compile(r"AK[A-Z0-9]{16,}", re.IGNORECASE)),
    ("sk", re.compile(r"(?i)\b(sk|secret|token|password|api[-_]?key)\b['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}")),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("url_credential", re.compile(r"(https?://)[^/@\s]+@")),
]

_INJECTION_MARKERS = (
    "<script",
    "javascript:",
    "onerror=",
    "onload=",
    "onclick=",
    "<?php",
    "<?xml",
    "os.system(",
    "subprocess",
    "eval(",
    "exec(",
    "rm -rf",
)


class SideEffectLevel(StrEnum):
    """副作用等级：L1 只读 / L2 低风险写（幂等）/ L3 高风险写（审批）。"""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


@dataclass(frozen=True)
class ToolContract:
    """工具契约：Schema、版本、权限、side-effect、费用与超时/重试元数据。"""

    name: str
    description: str = ""
    args_schema: dict[str, Any] | None = None
    side_effect_level: SideEffectLevel = SideEffectLevel.L1
    idempotency_required: bool = False
    cost_usd_per_call: float = 0.0
    timeout_seconds: float = 10.0
    retryable: bool = False  # 仅当幂等或只读时可重试
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    registry_version: str = "1.0"
    # BusinessToolContract 兼容字段。保留旧字段默认值，旧工具无需迁移。
    args_model: Any = None
    result_model: Any = None
    risk_level: str = "low"
    required_scopes: tuple[str, ...] = ()
    tenant_boundary: str = "tenant"
    cache_policy: dict[str, Any] | None = None
    evidence_fields: tuple[str, ...] = ()
    compensating_action: str = ""

    def __post_init__(self) -> None:
        if self.retryable and not self.idempotency_required and self.side_effect_level != SideEffectLevel.L1:
            # 允许 L1 只读默认重试；L2/L3 必须显式声明幂等才可重试
            raise ValueError(
                f"tool {self.name}: retryable=true 要求 L1 只读或 idempotency_required=true"
            )


class ToolRegistryError(ValueError):
    """工具契约注册/查询错误。"""


class ToolRegistry:
    """统一工具契约注册表（schema 版本/权限/side-effect/费用元数据）。"""

    def __init__(self, *, version: str = "1.0"):
        self.version = version
        self._contracts: dict[str, ToolContract] = {}

    def register(self, contract: ToolContract) -> None:
        if contract.name in self._contracts:
            raise ToolRegistryError(f"duplicate tool contract: {contract.name}")
        self._contracts[contract.name] = contract

    def get(self, name: str) -> ToolContract:
        try:
            return self._contracts[name]
        except KeyError:
            raise ToolRegistryError(f"unknown tool: {name}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._contracts

    def names(self) -> list[str]:
        return sorted(self._contracts)

    def allowlist(self, names: list[str]) -> list[ToolContract]:
        return [self.get(n) for n in names]

    def snapshot(self) -> dict[str, Any]:
        """注册表版本指纹（供 RunManifest 冻结 tool_registry_version）。"""
        payload = {
            "version": self.version,
            "tools": {
                n: {
                    "description": c.description,
                    "registry_version": c.registry_version,
                    "args_schema": _schema_fingerprint(c.args_schema, c.args_model),
                    "result_schema": _schema_fingerprint(c.result_model),
                    "side_effect_level": c.side_effect_level.value,
                    "risk_level": c.risk_level,
                    "idempotency_required": c.idempotency_required,
                    "retryable": c.retryable,
                    "timeout_seconds": c.timeout_seconds,
                    "required_scopes": list(c.required_scopes),
                    "tenant_boundary": c.tenant_boundary,
                }
                for n, c in sorted(self._contracts.items())
            },
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return {"registry_version": self.version, "fingerprint": f"sha256:{digest}"}


def _schema_fingerprint(schema: Any, model: Any = None) -> str:
    """Return a stable schema representation without importing Pydantic eagerly."""
    candidate = model or schema
    if candidate is None:
        return ""
    if hasattr(candidate, "model_json_schema"):
        try:
            candidate = candidate.model_json_schema()
        except Exception:
            candidate = str(candidate)
    return hashlib.sha256(
        json.dumps(candidate, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


# ═══════════════════════════════════════════════════════════════
# 工具适配器（fake / recorded / sandbox / production）
# ═══════════════════════════════════════════════════════════════


class ToolAdapterKind(StrEnum):
    FAKE = "fake"  # 确定性脚本结果（离线/CI）
    RECORDED = "recorded"  # 录制轨迹重放（确定性）
    SANDBOX = "sandbox"  # 隔离沙箱：校验参数、不产生真实副作用
    PRODUCTION = "production"  # 生产执行


class ToolAdapter(Protocol):
    kind: ToolAdapterKind

    async def invoke(self, name: str, args: dict[str, Any]) -> str:
        """执行工具并返回文本结果（错误由 ProtectedToolCaller 统一处理）。"""
        ...


@dataclass
class FakeToolAdapter:
    """确定性脚本化适配器：按工具名返回固定结果（CI 可重复）。"""

    kind: ToolAdapterKind = ToolAdapterKind.FAKE
    results: dict[str, str] = field(default_factory=dict)

    async def invoke(self, name: str, args: dict[str, Any]) -> str:
        return self.results.get(name, f"[fake:{name}]")


class RecordedToolAdapter:
    """录制轨迹重放适配器：按 args_hash 确定性返回录制结果。"""

    kind: ToolAdapterKind = ToolAdapterKind.RECORDED

    def __init__(self, log: RecordedToolLog):
        self.log = log

    async def invoke(self, name: str, args: dict[str, Any]) -> str:
        outcome = self.log.lookup(args_hash=_hash_args(args))
        if outcome is None:
            raise KeyError(f"recorded adapter: no recording for {name} args_hash={_hash_args(args)[:8]}")
        return outcome["result"]


class SandboxToolAdapter:
    """沙箱适配器：只校验参数并返回确认，不产生真实副作用。

    L3 工具在沙箱中一律拒绝（需审批链）；L2 返回幂等确认占位。
    """

    kind: ToolAdapterKind = ToolAdapterKind.SANDBOX

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry

    async def invoke(self, name: str, args: dict[str, Any]) -> str:
        contract = self.registry.get(name) if self.registry else None
        level = contract.side_effect_level if contract else SideEffectLevel.L1
        if level == SideEffectLevel.L3:
            raise PermissionError(f"sandbox rejects L3 tool: {name}")
        return f"[sandbox:{name} ok]"


class ProductionToolAdapter:
    """生产适配器：包装真实执行器（须由外部注入受控 executor）。"""

    kind: ToolAdapterKind = ToolAdapterKind.PRODUCTION

    def __init__(self, executor: Callable[[str, dict[str, Any]], Awaitable[str]]):
        self.executor = executor

    async def invoke(self, name: str, args: dict[str, Any]) -> str:
        return await self.executor(name, args)


# ═══════════════════════════════════════════════════════════════
# 结果净化
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SanitizedResult:
    """净化后的工具结果。"""

    text: str
    truncated: bool
    encoding_fixed: bool
    injection_detected: bool
    redacted_fields: list[str]


class ToolResultSanitizer:
    """工具结果净化：大小截断、编码清洗、注入标记、敏感信息脱敏。"""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        redact_patterns: list[tuple[str, re.Pattern[str]]] | None = None,
    ):
        self.max_bytes = max_bytes
        self._patterns = redact_patterns or _SENSITIVE_PATTERNS

    def sanitize(self, *, tool_name: str, raw_text: str) -> SanitizedResult:
        encoding_fixed = False
        try:
            raw_text.encode("utf-8")
        except UnicodeEncodeError:
            raw_text = raw_text.encode("utf-8", errors="replace").decode("utf-8")
            encoding_fixed = True

        truncated = len(raw_text.encode("utf-8")) > self.max_bytes
        if truncated:
            raw_text = raw_text.encode("utf-8")[: self.max_bytes].decode("utf-8", errors="replace")

        lower = raw_text.lower()
        injection = any(marker in lower for marker in _INJECTION_MARKERS)

        redacted: list[str] = []
        for name, pattern in self._patterns:
            if pattern.search(raw_text):
                raw_text = pattern.sub(f"[redacted:{name}]", raw_text)
                redacted.append(name)

        return SanitizedResult(
            text=raw_text,
            truncated=truncated,
            encoding_fixed=encoding_fixed,
            injection_detected=injection,
            redacted_fields=redacted,
        )


# ═══════════════════════════════════════════════════════════════
# 录制-重放轨迹
# ═══════════════════════════════════════════════════════════════


def _hash_args(args: dict[str, Any]) -> str:
    payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RecordedToolLog:
    """录制工具调用轨迹：只存 hash/状态，供 recorded 适配器确定性重放。"""

    def __init__(self, entries: list[dict[str, Any]] | None = None):
        self._entries: list[dict[str, Any]] = list(entries) if entries else []

    def record(
        self,
        *,
        tool_name: str,
        args_hash: str,
        result_hash: str,
        ok: bool,
        error_code: str = "",
        duration_ms: float = 0.0,
        source_ids: list[str] | None = None,
    ) -> None:
        self._entries.append(
            {
                "tool_name": tool_name,
                "args_hash": args_hash,
                "result_hash": result_hash,
                "ok": ok,
                "error_code": error_code,
                "duration_ms": duration_ms,
                "source_ids": source_ids or [],
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {"schema_version": "1.0", "entries": list(self._entries)}

    @classmethod
    def load(cls, snapshot: dict[str, Any]) -> RecordedToolLog:
        return cls(snapshot.get("entries", []))

    def lookup(self, *, args_hash: str) -> dict[str, Any] | None:
        for entry in reversed(self._entries):
            if entry["args_hash"] == args_hash:
                return dict(entry)
        return None

    def entry_count(self) -> int:
        return len(self._entries)


# ═══════════════════════════════════════════════════════════════
# ProtectedToolCaller（统一 timeout / retry / breaker / telemetry）
# ═══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ToolCallOutcome:
    """一次受保护工具调用的结果。"""

    tool_name: str
    ok: bool
    result_hash: str = ""
    error_code: str = ""
    message: str = ""
    duration_ms: float = 0.0
    truncated: bool = False
    sanitized: bool = False
    retries: int = 0
    source_ids: list[str] = field(default_factory=list)


class ProtectedToolCaller:
    """统一包装：契约校验 → 熔断 → 超时/重试 → 净化 → 遥测 → 录制。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        adapter: ToolAdapter,
        breaker_registry: Any = None,
        sanitizer: ToolResultSanitizer | None = None,
        telemetry: Any = None,
        max_retries: int = 2,
        backoff_jitter: float = 0.0,
        default_timeout_seconds: float | None = None,
    ):
        self.registry = registry
        self.adapter = adapter
        self.breaker_registry = breaker_registry
        self.sanitizer = sanitizer or ToolResultSanitizer()
        self.telemetry = telemetry
        self.max_retries = max(0, max_retries)
        self.backoff_jitter = backoff_jitter
        self.default_timeout = default_timeout_seconds

    def _breaker(self, name: str) -> Any | None:
        if self.breaker_registry is None:
            return None
        try:
            return self.breaker_registry.breaker(f"tool:{name}")
        except Exception:
            return None

    async def call(
        self,
        name: str,
        args: dict[str, Any],
        *,
        recorded: RecordedToolLog | None = None,
        now: Any = None,
    ) -> ToolCallOutcome:
        contract = self.registry.get(name)
        started = time_ms()
        breaker = self._breaker(name)
        if breaker is not None and not breaker.allow_request():
            return self._fail(
                name, "breaker_open", "circuit open", recorded, args, contract, started
            )

        outcome = await self._invoke_with_retry(name, args, contract, breaker)
        if self.telemetry is not None:
            self.telemetry.observe("tool_latency_ms", value=outcome.duration_ms, tool=name)
            self.telemetry.inc(
                "tool_calls", tool=name, status="ok" if outcome.ok else "error"
            )
        if recorded is not None:
            recorded.record(
                tool_name=name,
                args_hash=_hash_args(args),
                result_hash=outcome.result_hash,
                ok=outcome.ok,
                error_code=outcome.error_code,
                duration_ms=outcome.duration_ms,
            )
        return outcome

    async def _invoke_with_retry(
        self, name: str, args: dict[str, Any], contract: ToolContract, breaker: Any | None
    ) -> ToolCallOutcome:
        last: ToolCallOutcome | None = None
        attempts = 1 if not contract.retryable else 1 + self.max_retries
        for attempt in range(attempts):
            started = time_ms()
            timeout = self.default_timeout or contract.timeout_seconds
            try:
                raw = await asyncio.wait_for(self.adapter.invoke(name, args), timeout=timeout)
                sanitized = self.sanitizer.sanitize(tool_name=name, raw_text=raw)
                if breaker is not None:
                    breaker.record_success()
                return ToolCallOutcome(
                    tool_name=name,
                    ok=True,
                    result_hash=_hash_text(sanitized.text),
                    duration_ms=time_ms() - started,
                    truncated=sanitized.truncated,
                    sanitized=True,
                    retries=attempt,
                )
            except TimeoutError:
                if breaker is not None:
                    breaker.record_failure(timed_out=True)
                last = self._fail(name, "timeout", "tool timeout", None, args, contract, started)
            except Exception as exc:
                if breaker is not None:
                    breaker.record_failure()
                last = self._fail(
                    name, type(exc).__name__, str(exc)[:200], None, args, contract, started
                )
            if attempt < attempts - 1 and last is not None:
                await asyncio.sleep(_backoff(attempt, self.backoff_jitter))
        return last or self._fail(name, "unknown", "no result", None, args, contract, started)

    @staticmethod
    def _fail(
        name: str,
        error_code: str,
        message: str,
        recorded: RecordedToolLog | None,
        args: dict[str, Any],
        contract: ToolContract,
        started: float,
    ) -> ToolCallOutcome:
        outcome = ToolCallOutcome(
            tool_name=name,
            ok=False,
            error_code=error_code,
            message=message,
            duration_ms=time_ms() - started,
        )
        if recorded is not None:
            recorded.record(
                tool_name=name,
                args_hash=_hash_args(args),
                result_hash="",
                ok=False,
                error_code=error_code,
                duration_ms=outcome.duration_ms,
            )
        return outcome


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _backoff(attempt: int, jitter: float) -> float:
    import random

    base = 0.05 * (2**attempt)
    return base + (random.random() * jitter if jitter else 0.0)


def time_ms() -> float:
    import time

    return time.perf_counter() * 1000
