"""Model Harness — 阶段4 §1.2（WBS 4.1）。

统一 provider 适配（usage / finish_reason / tool_calls 标准化）、错误映射
（对齐统一 ErrorTaxonomy）、模型 allowlist / 路由 / fallback / 熔断 / 限流，
并提供 deterministic fake 与 recorded response 用于 CI。

安全约束：
  - 只记录模型名、用量、原因码与退化标记，不落 prompt 正文；
  - allowlist 之外的模型一律拒绝（防误用未授权模型）。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class ModelErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    CONNECTION = "connection"
    INVALID_SCHEMA = "invalid_schema"
    AUTH = "auth"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UsageSnapshot:
    """标准化用量快照（对齐预算结算与成本计价的四元组）。"""

    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    estimated: bool = False
    finish_reason: FinishReason = FinishReason.STOP
    tool_calls: int = 0
    duration_ms: float = 0.0


@dataclass(frozen=True)
class ModelCallResult:
    """一次模型调用的统一结果。"""

    ok: bool
    content: str = ""
    usage: UsageSnapshot | None = None
    model_id: str = ""
    error_kind: str = ""
    reason_code: str = ""
    degraded: bool = False
    fallback_used: bool = False
    breaker_open: bool = False


class ModelProviderAdapter(Protocol):
    """provider 适配器：返回 (content, usage)，错误抛出由 Harness 映射。"""

    kind: str

    async def generate(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> tuple[str, UsageSnapshot]: ...


class FakeModelAdapter:
    """确定性脚本化后端：按脚本顺序返回内容，可注入故障（CI 可重复）。"""

    kind = "fake"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        usage: UsageSnapshot | None = None,
        faults: list[BaseException] | None = None,
    ):
        self.responses = list(responses or ["确定性回复"])
        self.faults = list(faults or [])
        self.usage = usage
        self.calls: list[str] = []

    async def generate(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> tuple[str, UsageSnapshot]:
        self.calls.append(model_id)
        if self.faults:
            raise self.faults.pop(0)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        content = self.responses[idx]
        usage = self.usage or UsageSnapshot(
            model_id=model_id,
            input_tokens=100,
            output_tokens=max(1, len(content) // 4),
        )
        return content, usage


def _messages_hash(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(messages, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RecordedModelAdapter:
    """录制响应重放：按输入消息 hash 确定性返回（离线评测/回归）。"""

    kind = "recorded"

    def __init__(self, recordings: dict[str, dict[str, Any]] | None = None):
        self.recordings: dict[str, dict[str, Any]] = dict(recordings or {})

    def add(
        self, *, messages: list[dict[str, Any]], content: str, usage: UsageSnapshot | None = None
    ):
        self.recordings[_messages_hash(messages)] = {
            "content": content,
            "usage": usage,
        }

    async def generate(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> tuple[str, UsageSnapshot]:
        key = _messages_hash(messages)
        rec = self.recordings.get(key)
        if rec is None:
            raise KeyError(f"recorded adapter: no recording for message hash {key[:16]}")
        usage = rec["usage"] or UsageSnapshot(model_id=model_id, input_tokens=100, output_tokens=50)
        return rec["content"], usage


# ═══════════════════════════════════════════════════════════════
# 错误映射
# ═══════════════════════════════════════════════════════════════

_RATE_LIMIT_MARKS = ("429", "rate.limit", "rate_limit", "too many requests", "quota")
_SERVER_MARKS = ("5", "server", "unavailable", "bad gateway", "500", "503")
_CONNECTION_MARKS = ("connection", "connect", "timeout error", "network", "socket", "dns")
_AUTH_MARKS = ("401", "403", "unauthorized", "forbidden", "authentication", "api key")
_SCHEMA_MARKS = ("json", "schema", "validation", "parse")


def map_model_error(exc: BaseException) -> tuple[ModelErrorKind, str]:
    """将任意 provider 异常映射为统一分类（对齐 ErrorTaxonomy）。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in name or "timeout" in text:
        return ModelErrorKind.TIMEOUT, "timeout"
    if any(m in name or m in text for m in _RATE_LIMIT_MARKS):
        return ModelErrorKind.RATE_LIMIT, "rate_limit"
    if any(m in name or m in text for m in _SERVER_MARKS):
        return ModelErrorKind.SERVER_ERROR, "server_error"
    if any(m in name or m in text for m in _CONNECTION_MARKS):
        return ModelErrorKind.CONNECTION, "connection"
    if any(m in name or m in text for m in _AUTH_MARKS):
        return ModelErrorKind.AUTH, "auth"
    if any(m in name or m in text for m in _SCHEMA_MARKS):
        return ModelErrorKind.INVALID_SCHEMA, "invalid_schema"
    return ModelErrorKind.UNKNOWN, "unknown"


# ═══════════════════════════════════════════════════════════════
# 限流（模型级滑动窗口 RPS/TPM）
# ═══════════════════════════════════════════════════════════════


class ModelRateLimiter:
    """模型级滑动窗口限流：每秒调用数 + 每分钟 Token 数。

    窗口内计数超限则拒绝（返回 False），调用方走 fallback 或拒绝，
    不阻塞等待 —— 避免排队放大故障。
    """

    def __init__(
        self,
        *,
        max_calls_per_second: int = 0,
        max_tokens_per_minute: int = 0,
        now_provider: Callable[[], float] | None = None,
    ):
        self.max_cps = max_calls_per_second
        self.max_tpm = max_tokens_per_minute
        self._now = now_provider or (lambda: time.monotonic())
        self._call_times: list[float] = []
        self._token_times: list[tuple[float, int]] = []

    def _prune(self, now: float) -> None:
        self._call_times = [t for t in self._call_times if now - t < 1.0]
        self._token_times = [(t, n) for t, n in self._token_times if now - t < 60.0]

    async def acquire(
        self,
        *,
        model_id: str,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
    ) -> bool:
        now = self._now()
        self._prune(now)
        if self.max_cps and len(self._call_times) >= self.max_cps:
            return False
        tokens = estimated_input_tokens + estimated_output_tokens
        if self.max_tpm and sum(n for _, n in self._token_times) + tokens > self.max_tpm:
            return False
        self._call_times.append(now)
        self._token_times.append((now, tokens))
        return True

    def stats(self) -> dict[str, Any]:
        now = self._now()
        self._prune(now)
        return {
            "calls_in_window": len(self._call_times),
            "tokens_in_window": sum(n for _, n in self._token_times),
        }


# ═══════════════════════════════════════════════════════════════
# ModelHarness（allowlist + 路由 + fallback + 熔断 + 限流 + 遥测）
# ═══════════════════════════════════════════════════════════════


class ModelHarness:
    """统一模型入口：路由选择 → 限流 → 熔断 → 调用 → 失败回退。

    回退仅在同一 allowlist 内按序尝试；不降级到允许列表之外的模型。
    安全约束：只记录模型名/用量/原因码/退化标记，不落正文。
    """

    def __init__(
        self,
        *,
        allowlist: list[str],
        adapter: ModelProviderAdapter,
        router: Any = None,
        breaker_registry: Any = None,
        limiter: ModelRateLimiter | None = None,
        telemetry: Any = None,
        default_model: str = "",
    ):
        if not allowlist:
            raise ValueError("ModelHarness require non-empty allowlist")
        self.allowlist = list(allowlist)
        self.adapter = adapter
        self.router = router
        self.breaker_registry = breaker_registry
        self.limiter = limiter
        self.telemetry = telemetry
        self.default_model = default_model or allowlist[0]

    def _breaker(self, model_id: str) -> Any | None:
        if self.breaker_registry is None:
            return None
        try:
            return self.breaker_registry.breaker(f"provider:{model_id}")
        except Exception:
            return None

    def _candidates(self, forced_model: str) -> list[str]:
        if forced_model:
            if forced_model not in self.allowlist:
                return []
            return [forced_model]
        if self.router is not None:
            try:
                decision = self.router.route(
                    self.router.RouteRequest(task_type="chat", sensitivity="L0")
                )
                return [decision.model]
            except Exception:
                pass
        return list(self.allowlist)

    async def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str = "",
        tool_schemas: list[dict[str, Any]] | None = None,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        allow_fallback: bool = True,
    ) -> ModelCallResult:
        candidates = self._candidates(model_id)
        if not candidates:
            return ModelCallResult(
                ok=False,
                model_id=model_id or self.default_model,
                error_kind=ModelErrorKind.AUTH.value,
                reason_code="not_in_allowlist",
            )
        last: ModelCallResult | None = None
        for idx, candidate in enumerate(candidates):
            if self.limiter is not None:
                allowed = await self.limiter.acquire(
                    model_id=candidate,
                    estimated_input_tokens=estimated_input_tokens,
                    estimated_output_tokens=estimated_output_tokens,
                )
                if not allowed:
                    last = ModelCallResult(
                        ok=False,
                        model_id=candidate,
                        error_kind=ModelErrorKind.RATE_LIMIT.value,
                        reason_code="rate_limited",
                    )
                    continue
            breaker = self._breaker(candidate)
            if breaker is not None and not breaker.allow_request():
                last = ModelCallResult(
                    ok=False,
                    model_id=candidate,
                    error_kind=ModelErrorKind.UNKNOWN.value,
                    reason_code="breaker_open",
                    breaker_open=True,
                )
                continue
            try:
                content, usage = await self.adapter.generate(
                    model_id=candidate, messages=messages, tool_schemas=tool_schemas
                )
                if breaker is not None:
                    breaker.record_success()
                self._observe(candidate, usage, ok=True)
                return ModelCallResult(
                    ok=True,
                    content=content,
                    usage=usage,
                    model_id=candidate,
                    degraded=idx > 0,
                    fallback_used=idx > 0,
                )
            except Exception as exc:
                kind, reason = map_model_error(exc)
                if breaker is not None:
                    breaker.record_failure(timed_out=kind == ModelErrorKind.TIMEOUT)
                self._observe(candidate, None, ok=False, reason=reason)
                last = ModelCallResult(
                    ok=False,
                    model_id=candidate,
                    error_kind=kind.value,
                    reason_code=reason,
                )
                if not allow_fallback:
                    return last
        return last or ModelCallResult(
            ok=False,
            model_id=self.default_model,
            error_kind=ModelErrorKind.UNKNOWN.value,
            reason_code="no_candidate",
        )

    def _observe(
        self, model_id: str, usage: UsageSnapshot | None, *, ok: bool, reason: str = ""
    ) -> None:
        if self.telemetry is None:
            return
        self.telemetry.inc("model_calls", model=model_id, status="ok" if ok else "error")
        if usage is not None:
            self.telemetry.inc(
                "model_tokens", amount=usage.input_tokens + usage.output_tokens, model=model_id
            )
        if not ok:
            self.telemetry.inc("model_errors", model=model_id, reason=reason)
