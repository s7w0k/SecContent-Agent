"""A2A Client 适配与安全 — 阶段四 4B Step 4B-3 / 4B-4。

A2AClient 让本系统以「Agent Client」角色调用远端 Agent（仅允许列表）：

  - 发现层：只从管理员允许列表发现 Agent Card；缓存带 TTL 与失效策略；
  - SSRF 防线：HTTPS/主机名/端口校验；DNS 解析前后双重校验；跨域重定向
    重新执行完整安全检查；默认禁止环回、链路本地、私网与云元数据地址；
  - 能力选择：根据远端 Skill 的 input/output_modes 选择 task / stream 模式；
  - 有限重试：仅对超时、限流(429/408)与断流(5xx/传输错误)重试（指数退避+抖动）；
  - 本地 Step Ledger：每次远端调用写入脱敏记录；重试前按 (peer, idempotency_key)
    查账防重复副作用；
  - 策略与预算：所有调用继续经过 PolicyEngine（a2a_send 规则，强制幂等键）与
    每远端独立预算/限流/并发/熔断；
  - 认证与凭证隔离：OAuth2/OIDC Bearer（TokenProvider 注入短有效期、audience、
    scope 最小化）或 mTLS（http_client 自带证书）；Authorization 只绑定允许列表
    初始源，跨域重定向一律剥掉，绝不把内部凭证转发给远端；
  - 不可信响应净化：远端 Task/Artifact 视为不可信输入，按大小/类型/恶意内容校验。

安全约束：
  - 远端内容不能覆盖 system policy 或提升工具权限（能力声明只作参考，
    调用是否放行由本地 allowlist + PolicyEngine 决定）；
  - 日志只记录协议元数据与脱敏摘要，不记录提示词、密钥与私有推理链。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator, Awaitable, Callable, Literal
from urllib.parse import urljoin, urlparse

import httpx

from agent.a2a.mapper import validate_external_input, validate_external_task
from agent.a2a.models import (
    A2AError,
    AgentCard,
    InvalidInputError,
    Message,
    Part,
    PROTOCOL_VERSION,
    Skill,
    Task,
    TaskSendResult,
    TaskStatus,
    TaskStatusUpdateEvent,
    VERSION_HEADER,
)
from agent.policy_engine import PolicyEngine
from agent.runtime_state import BudgetUsage, RunBudget

logger = logging.getLogger("backend.agent.a2a_client")

# ── 常量 ────────────────────────────────────────────────────
MAX_AGENT_CARD_BYTES = 64 * 1024  # Agent Card 响应上限 64 KiB
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 一般响应上限 5 MiB
MAX_REDIRECTS = 3
DEFAULT_CARD_TTL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_MAX = 2
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_RPS = 5.0
DEFAULT_BREAKER_FAIL_THRESHOLD = 3
DEFAULT_BREAKER_COOLDOWN_SECONDS = 30.0

_BLOCKED_HOSTNAMES = frozenset({"localhost", "0.0.0.0", "::1"})
_BLOCKED_HOST_SUFFIXES = (".local", ".internal")
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_TASK_ID_CHARS = 100
_MAX_RESPONSE_HEADER_BYTES = 64 * 1024

# ═══════════════════════════════════════════════════════════════
# 错误
# ═══════════════════════════════════════════════════════════════


class SSRFBlockedError(A2AError):
    """URL/DNS 校验未通过（环回/私网/链路本地/云元数据/非 HTTPS 等）。"""


class DiscoveryError(A2AError):
    """远端 Agent 不在允许列表 / Agent Card 无法获取。"""


class RemoteUnavailableError(A2AError):
    """远端不可用：重试耗尽 / 熔断开启 / 断流。进入可解释暂停或失败状态。"""


class RateLimitedError(A2AError):
    """远端限流（429）且重试耗尽。"""


class BudgetExceededError(A2AError):
    """远端调用超出每 Agent 预算 / 配额。"""


class CapabilityError(A2AError):
    """远端 Agent Card 未声明能力 / 声明能力与允许列表不一致。"""


class AuthError(A2AError):
    """远端认证失败（401/403）或凭据缺失。"""


class ProtocolClientError(A2AError):
    """远端响应畸形 / 协议版本不符 / 响应体超限。"""


class PeerNotFoundError(A2AError):
    """远端任务/资源不存在（404）。"""


class PolicyDeniedError(A2AError):
    """本地 PolicyEngine 拒绝（能力门禁 / 策略门禁）。"""


class _RetryableRequest(A2AError):
    """内部标记：可重试的传输错误或限流/5xx。"""

    def __init__(self, reason: str, *, status: int = 0):
        super().__init__(reason)
        self.status = status


# ═══════════════════════════════════════════════════════════════
# SSRF 防护
# ═══════════════════════════════════════════════════════════════


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """环回 / 私网 / 链路本地 / 组播 / 保留 / 未指定 / 云元数据地址一律禁止。"""
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    # AWS/Azure/GCP 云元数据地址
    if isinstance(ip, ipaddress.IPv4Address) and str(ip) == "169.254.169.254":
        return True
    # IPv6 链路本地 fe80::/10 已被 is_link_local 覆盖
    return False


def _default_resolver(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return []
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def validate_peer_url(
    url: str,
    *,
    require_https: bool = True,
    resolver: Callable[[str], list[str]] | None = None,
) -> str:
    """URL 安全校验（DNS 解析前后双重）：返回原始 URL 或抛 SSRFBlockedError。

    规则：
      - 仅 http(s)；require_https=True 时强制 HTTPS；
      - 不允许 URL 内嵌用户名/密码（防凭证泄露）；
      - 字面 IP 直接校验；域名先查黑名单再解析，解析出的每个 IP 都必须安全；
      - 默认禁止环回、链路本地、私网、组播、保留地址与云元数据。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFBlockedError(f"scheme not allowed: {parsed.scheme!r}")
    if require_https and parsed.scheme != "https":
        raise SSRFBlockedError("https required")
    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlockedError("url has no host")
    if parsed.username or parsed.password:
        raise SSRFBlockedError("url must not embed credentials")
    port = parsed.port
    if port is not None and port not in (80, 443, 8080, 8443):
        raise SSRFBlockedError(f"non-default port not allowed: {port}")

    host = hostname.lower().rstrip(".")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise SSRFBlockedError(f"blocked hostname: {host}")

    # 字面 IP：直接校验
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise SSRFBlockedError(f"blocked ip: {host}")
        return url
    except ValueError:
        pass

    # 域名：DNS 解析后逐一校验（防 DNS 重绑定到内网/元数据）
    resolve = resolver or _default_resolver
    ips = resolve(host)
    if not ips:
        raise SSRFBlockedError(f"dns resolution failed: {host}")
    for ip_str in ips:
        try:
            resolved = ipaddress.ip_address(ip_str.split("%")[0])
        except ValueError:
            raise SSRFBlockedError(f"invalid resolved ip: {ip_str!r}") from None
        if _is_blocked_ip(resolved):
            raise SSRFBlockedError(f"resolved to blocked ip: {ip_str}")
    return url


# ═══════════════════════════════════════════════════════════════
# 允许列表配置 / 账本 / 认证 / 限流 / 熔断
# ═══════════════════════════════════════════════════════════════


class RemoteAgentConfig:
    """管理员允许列表中的单个远端 Agent（配置即门禁）。

    - base_url 必须是允许列表登记过的源；require_https 默认 True；
    - enabled_skills：本系统允许该远端调用的 Skill 子集（∩ Agent Card 声明）；
    - budget：每远端独立预算（含 remote_agent_quota 配额）；
    - auth_mode：none / bearer（OIDC）/ mtls（客户端证书由 http_client 承载）。
    """

    __slots__ = ("_data",)

    def __init__(
        self,
        *,
        key: str,
        base_url: str,
        enabled_skills: list[str] | None = None,
        require_https: bool = True,
        card_ttl_seconds: int = DEFAULT_CARD_TTL_SECONDS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        rps: float = DEFAULT_RPS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retry_max: int = DEFAULT_RETRY_MAX,
        circuit_breaker: bool = True,
        breaker_fail_threshold: int = DEFAULT_BREAKER_FAIL_THRESHOLD,
        breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS,
        budget: RunBudget | None = None,
        auth_mode: Literal["none", "bearer", "mtls"] = "none",
        audience: str = "",
        scopes: list[str] | None = None,
    ):
        self._data = {
            "key": key,
            "base_url": base_url.rstrip("/"),
            "enabled_skills": list(enabled_skills or []),
            "require_https": require_https,
            "card_ttl_seconds": max(30, int(card_ttl_seconds)),
            "max_concurrency": max(1, int(max_concurrency)),
            "rps": max(0.1, float(rps)),
            "timeout_seconds": max(1.0, float(timeout_seconds)),
            "retry_max": max(0, int(retry_max)),
            "circuit_breaker": bool(circuit_breaker),
            "breaker_fail_threshold": max(1, int(breaker_fail_threshold)),
            "breaker_cooldown_seconds": max(1.0, float(breaker_cooldown_seconds)),
            "budget": budget,
            "auth_mode": auth_mode,
            "audience": audience,
            "scopes": list(scopes or []),
        }

    def __getattr__(self, name: str):
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __repr__(self) -> str:  # 脱敏：不打印 budget/凭据细节
        return f"RemoteAgentConfig(key={self.key!r}, base_url={self.base_url!r})"


class A2ACallRecord:
    """本地 Step Ledger 条目（脱敏）：一次远端 Agent 调用的审计记录。"""

    __slots__ = (
        "peer", "task_id", "message_id", "skill_id", "idempotency_key",
        "status", "started_at", "finished_at", "artifact_count", "error",
    )

    def __init__(
        self,
        *,
        peer: str,
        task_id: str,
        message_id: str,
        skill_id: str,
        idempotency_key: str,
        status: str,
        started_at: datetime,
        finished_at: datetime,
        artifact_count: int = 0,
        error: str = "",
    ):
        self.peer = peer
        self.task_id = task_id
        self.message_id = message_id
        self.skill_id = skill_id
        self.idempotency_key = idempotency_key
        self.status = status
        self.started_at = started_at
        self.finished_at = finished_at
        self.artifact_count = artifact_count
        self.error = error

    def to_task(self) -> Task | None:
        """从已记账的成功调用还原 Task（重试去重：避免重复副作用）。"""
        if not self.task_id or self.status != "COMPLETED":
            return None
        return Task(
            id=self.task_id,
            status=TaskStatus.COMPLETED,
            metadata={
                "peer": self.peer,
                "message_id": self.message_id,
                "skill_id": self.skill_id,
                "from_ledger": True,
            },
            created_timestamp=self.started_at,
            last_updated_timestamp=self.finished_at,
        )


class A2ACallLedger:
    """本地 Step Ledger 接口：远端调用审计与幂等去重。"""

    async def record(self, record: A2ACallRecord) -> None:
        raise NotImplementedError

    async def find(self, *, peer: str, idempotency_key: str) -> A2ACallRecord | None:
        """按 (peer, idempotency_key) 查已成功记账的调用（重试去重）。"""
        raise NotImplementedError


class MemoryCallLedger(A2ACallLedger):
    """进程内实现（MVP；生产可替换为 ExecutionStepLedger/Mongo）。"""

    def __init__(self):
        self._records: list[A2ACallRecord] = []

    async def record(self, record: A2ACallRecord) -> None:
        self._records.append(record)

    async def find(self, *, peer: str, idempotency_key: str) -> A2ACallRecord | None:
        for rec in reversed(self._records):
            if rec.peer == peer and rec.idempotency_key == idempotency_key:
                return rec
        return None


class TokenProvider:
    """认证凭据注入接口（OAuth2/OIDC：短有效期、audience、最小 scope）。"""

    async def token(self, *, audience: str, scopes: list[str]) -> str | None:
        raise NotImplementedError


class _RateLimiter:
    """每远端独立限流：最小调用间隔 = 1 / rps。"""

    def __init__(
        self,
        rps: float,
        *,
        now: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ):
        self._min_interval = 1.0 / max(0.1, rps)
        self._now = now
        self._sleep = sleep
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._now()
            if self._last is not None:
                delta = now - self._last
                if delta < self._min_interval:
                    await self._sleep(self._min_interval - delta)
            self._last = self._now()


class _CircuitBreaker:
    """每远端独立熔断：连续失败达到阈值开启，冷却后半开探测。"""

    def __init__(
        self,
        *,
        fail_threshold: int,
        cooldown_seconds: float,
        now: Callable[[], float],
    ):
        self.fail_threshold = fail_threshold
        self.cooldown_seconds = cooldown_seconds
        self._now = now
        self.failures = 0
        self.state = "closed"
        self.opened_at: float | None = None

    def allow(self) -> bool:
        now = self._now()
        if self.state == "open":
            if self.opened_at is not None and now - self.opened_at >= self.cooldown_seconds:
                self.state = "half-open"
                return True
            return False
        return True

    def on_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = None

    def on_failure(self) -> None:
        self.failures += 1
        if self.state == "half-open" or self.failures >= self.fail_threshold:
            self.state = "open"
            self.opened_at = self._now()


@dataclass
class _CachedCard:
    card: AgentCard
    fetched_at: datetime


# ═══════════════════════════════════════════════════════════════
# A2AClient
# ═══════════════════════════════════════════════════════════════


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class A2AClient:
    """A2A 1.0 客户端：只调允许列表内远端，全链路安全与预算约束。"""

    def __init__(
        self,
        *,
        allowlist: dict[str, RemoteAgentConfig] | None = None,
        policy: PolicyEngine | None = None,
        ledger: A2ACallLedger | None = None,
        token_provider: TokenProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        default_card_ttl: int = DEFAULT_CARD_TTL_SECONDS,
    ):
        self.allowlist = dict(allowlist or {})
        self.policy = policy or PolicyEngine()
        self.ledger = ledger or MemoryCallLedger()
        self.token_provider = token_provider
        self.resolver = resolver or _default_resolver
        self._sleep = sleep or asyncio.sleep
        self._now_provider = now_provider or _utc_now
        self.default_card_ttl = max(30, int(default_card_ttl))
        if http_client is not None:
            self._http = http_client
            self.tls_verify: bool | None = None  # 自定义连接池：证书策略由调用方保证
        else:
            # SSRF 安全默认：不跟随重定向（手动逐跳校验）、不用环境代理、校验 TLS 证书
            self._http = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                verify=True,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
            )
            self.tls_verify = True
        self._cards: dict[str, _CachedCard] = {}
        self._usage_map: dict[str, BudgetUsage] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._limiters: dict[str, _RateLimiter] = {}
        self._breakers: dict[str, _CircuitBreaker] = {}

    # ══════════════════════════════════════════════════════════
    # 发现层（仅允许列表 + 缓存 TTL）
    # ══════════════════════════════════════════════════════════

    def _ensure_peer(self, agent_key: str) -> RemoteAgentConfig:
        cfg = self.allowlist.get(agent_key)
        if cfg is None:
            raise DiscoveryError(f"agent not in allowlist: {agent_key}")
        return cfg

    def _card_url(self, cfg: RemoteAgentConfig) -> str:
        return cfg.base_url + "/.well-known/agent-card.json"

    async def discover(self, agent_key: str, *, refresh: bool = False) -> AgentCard:
        """从允许列表发现 Agent Card；命中 TTL 内缓存则直接返回。"""
        cfg = self._ensure_peer(agent_key)
        now = self._now_provider()
        cached = self._cards.get(agent_key)
        if not refresh and cached is not None:
            ttl = timedelta(seconds=cfg.card_ttl_seconds or self.default_card_ttl)
            if now - cached.fetched_at < ttl:
                return cached.card

        url = validate_peer_url(
            self._card_url(cfg),
            require_https=cfg.require_https,
            resolver=self.resolver,
        )
        resp = await self._request(
            cfg, "GET", url,
            cap=MAX_AGENT_CARD_BYTES, attach_auth=False, allow_redirects=True,
        )
        if resp.status_code == 404:
            await resp.aclose()
            raise DiscoveryError(f"agent card not found: {agent_key}")
        if resp.status_code != 200:
            await resp.aclose()
            raise self._http_error(cfg, resp)
        data = await self._read_capped(resp, MAX_AGENT_CARD_BYTES)
        try:
            payload = json.loads(data)
            card = AgentCard.model_validate(payload)
        except Exception as exc:
            raise ProtocolClientError("invalid agent card payload") from exc
        if card.protocol_version != PROTOCOL_VERSION:
            raise ProtocolClientError(
                f"unsupported protocol version: {card.protocol_version}"
            )
        self._cards[agent_key] = _CachedCard(card=card, fetched_at=now)
        logger.info(
            "A2A card discovered: peer=%s skills=%s",
            agent_key, ",".join(s.id for s in card.skills),
        )
        return card

    def invalidate_card(self, agent_key: str) -> None:
        """显式失效缓存（远端升级/能力变更时调用）。"""
        self._cards.pop(agent_key, None)

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池（服务关闭时调用）。"""
        await self._http.aclose()

    # ══════════════════════════════════════════════════════════
    # 能力选择（task / stream）
    # ══════════════════════════════════════════════════════════

    def _select_mode(self, card: AgentCard, skill_id: str) -> str:
        skill = next((s for s in card.skills if s.id == skill_id), None)
        if skill is None:
            return "task"
        modes = {m.lower() for m in (skill.output_modes or [])}
        if modes & {"stream", "streaming", "sse", "event-stream"}:
            return "stream"
        return "task"

    def _select_skill(
        self, cfg: RemoteAgentConfig, card: AgentCard, skill_id: str
    ) -> Skill:
        """能力门禁：skill 必须在允许列表启用集合 + Agent Card 真实声明内。"""
        if cfg.enabled_skills and skill_id not in cfg.enabled_skills:
            raise CapabilityError(
                f"skill not enabled for peer {cfg.key}: {skill_id}"
            )
        skill = next((s for s in card.skills if s.id == skill_id), None)
        if skill is None:
            raise CapabilityError(f"agent card does not offer skill: {skill_id}")
        return skill

    # ══════════════════════════════════════════════════════════
    # 门禁：允许列表 / PolicyEngine / 预算 / 限流 / 并发 / 熔断
    # ══════════════════════════════════════════════════════════

    def _usage(self, cfg: RemoteAgentConfig) -> BudgetUsage:
        return self._usage_map.setdefault(cfg.key, BudgetUsage())

    def _breaker(self, cfg: RemoteAgentConfig) -> _CircuitBreaker:
        cb = self._breakers.get(cfg.key)
        if cb is None:
            cb = _CircuitBreaker(
                fail_threshold=cfg.breaker_fail_threshold,
                cooldown_seconds=cfg.breaker_cooldown_seconds,
                now=self._monotonic,
            )
            self._breakers[cfg.key] = cb
        return cb

    def _monotonic(self) -> float:
        import time as _time

        return _time.monotonic()

    async def _gate(self, cfg: RemoteAgentConfig, skill_id: str, idempotency_key: str, principal: str) -> None:
        """PolicyEngine + 预算 + 熔断 + 限流 + 并发 全部门禁。"""
        usage = self._usage(cfg)
        budget = cfg.budget
        stamp = self._now_provider()
        if budget is not None and not usage.can_start_next_action(budget, now=stamp):
            raise BudgetExceededError(
                f"peer budget exhausted: {cfg.key} (steps={usage.steps})"
            )
        if budget is not None and budget.remote_agent_quota > 0:
            used = usage.remote_agent_calls.get(cfg.key, 0)
            if used >= budget.remote_agent_quota:
                raise BudgetExceededError(
                    f"remote agent quota exhausted: {cfg.key} ({used}/{budget.remote_agent_quota})"
                )

        decision = self.policy.evaluate(
            tool_name="a2a_send",
            args={
                "peer": cfg.key,
                "skill_id": skill_id,
                "idempotency_key": idempotency_key,
            },
            user_id=principal,
            usage=usage,
            budget=budget,
            now=stamp,
        )
        if not decision.allowed:
            raise PolicyDeniedError(
                f"policy denied a2a_send: {decision.reason_code} (peer={cfg.key})"
            )

        breaker = self._breaker(cfg)
        if not breaker.allow():
            raise RemoteUnavailableError(f"circuit open for peer: {cfg.key}")

        limiter = self._limiters.get(cfg.key)
        if limiter is None:
            limiter = _RateLimiter(cfg.rps, now=self._monotonic, sleep=self._sleep)
            self._limiters[cfg.key] = limiter
        await limiter.wait()

    # ══════════════════════════════════════════════════════════
    # HTTP：手动重定向（逐跳 SSRF）+ 有限重试 + 响应大小上限
    # ══════════════════════════════════════════════════════════

    async def _request(
        self,
        cfg: RemoteAgentConfig,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        cap: int = MAX_RESPONSE_BYTES,
        attach_auth: bool = True,
        allow_redirects: bool = True,
    ) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(cfg.retry_max + 1):
            try:
                return await self._request_once(
                    cfg, method, url,
                    json_body=json_body, headers=headers, cap=cap,
                    attach_auth=attach_auth, allow_redirects=allow_redirects,
                )
            except _RetryableRequest as exc:
                last = exc
                if attempt >= cfg.retry_max:
                    break
                self._usage(cfg).record_retry(now=self._now_provider())
                delay = 0.2 * (2 ** attempt) * (1 + (hash(url) % 5) / 10)
                await self._sleep(delay)
        if isinstance(last, _RetryableRequest) and last.status == 429:
            raise RateLimitedError(
                f"rate limited after {cfg.retry_max + 1} attempts: {cfg.key}"
            ) from last
        raise RemoteUnavailableError(
            f"remote unavailable after {cfg.retry_max + 1} attempts: {cfg.key}"
        ) from last

    async def _request_once(
        self,
        cfg: RemoteAgentConfig,
        method: str,
        url: str,
        *,
        json_body: dict | None,
        headers: dict[str, str] | None,
        cap: int,
        attach_auth: bool,
        allow_redirects: bool,
    ) -> httpx.Response:
        headers = dict(headers or {})
        if attach_auth:
            await self._attach_auth(cfg, headers, url)

        current = url
        for hop in range(MAX_REDIRECTS + 1):
            # 每一跳都重新执行完整 SSRF 校验（含 DNS 解析）
            validate_peer_url(
                current, require_https=cfg.require_https, resolver=self.resolver
            )
            hop_headers = dict(headers)
            # 跨域重定向：一律剥掉 Authorization，不转发内部凭证
            if _origin(current) != _origin(url):
                hop_headers.pop("Authorization", None)

            try:
                resp = await self._http.request(
                    method,
                    current,
                    json=json_body,
                    headers=hop_headers,
                    follow_redirects=False,
                    timeout=httpx.Timeout(cfg.timeout_seconds),
                )
            except httpx.TimeoutException as exc:
                raise _RetryableRequest(f"timeout: {cfg.key}", status=408) from exc
            except httpx.ConnectError as exc:
                raise _RetryableRequest(f"connect error: {cfg.key}", status=0) from exc
            except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
                raise _RetryableRequest(f"stream interrupted: {cfg.key}", status=0) from exc
            except httpx.RequestError as exc:
                raise RemoteUnavailableError(f"request error: {exc}") from exc

            if resp.status_code in (301, 302, 303, 307, 308) and allow_redirects:
                location = resp.headers.get("location")
                await resp.aclose()
                if not location:
                    raise ProtocolClientError("redirect without location")
                current = urljoin(current, location)
                continue
            if resp.status_code in _RETRYABLE_STATUS:
                await resp.aclose()
                raise _RetryableRequest(f"status {resp.status_code}", status=resp.status_code)
            return resp
        raise RemoteUnavailableError(f"too many redirects: {cfg.key}")

    async def _read_capped(self, resp: httpx.Response, cap: int) -> bytes:
        """流式读取并限制响应体大小（防止远端无限大响应拖垮内部 Runtime）。"""
        data = bytearray()
        try:
            async for chunk in resp.aiter_bytes():
                data.extend(chunk)
                if len(data) > cap:
                    raise ProtocolClientError(
                        f"response too large: > {cap} bytes"
                    )
        finally:
            await resp.aclose()
        return bytes(data)

    async def _attach_auth(
        self, cfg: RemoteAgentConfig, headers: dict[str, str], url: str
    ) -> None:
        """Bearer/OIDC 注入：只绑定允许列表初始源；mTLS 由 http_client 证书承载。"""
        if cfg.auth_mode == "bearer" and self.token_provider is not None:
            token = await self.token_provider.token(
                audience=cfg.audience or _origin(cfg.base_url),
                scopes=cfg.scopes,
            )
            if token:
                headers["Authorization"] = f"Bearer {token}"

    # ══════════════════════════════════════════════════════════
    # Send（task / stream 模式）
    # ══════════════════════════════════════════════════════════

    async def send(
        self,
        agent_key: str,
        message: Message,
        *,
        principal: str = "",
        mode: str = "",
    ) -> Task:
        """发送消息到远端 Agent 并返回 Task（幂等：同 message_id 不重复副作用）。"""
        cfg = self._ensure_peer(agent_key)
        validate_external_input(message)  # 出站内容同样净化（防注入远端）

        skill_id = str(message.metadata.get("skill_id", "") or (cfg.enabled_skills[0] if cfg.enabled_skills else ""))
        if not skill_id:
            raise CapabilityError(f"no skill selected for peer {agent_key}")

        card = await self.discover(agent_key)
        self._select_skill(cfg, card, skill_id)  # 能力门禁：允许列表 ∩ 卡片声明

        idempotency_key = str(
            message.metadata.get("idempotency_key", "") or message.message_id
        )
        # 幂等去重：已成功记账的同 (peer, idempotency_key) 调用直接复用，不重复副作用
        existing = await self.ledger.find(peer=agent_key, idempotency_key=idempotency_key)
        if existing is not None:
            task = existing.to_task()
            if task is not None:
                logger.info("A2A call deduped from ledger: peer=%s task=%s", agent_key, task.id)
                return task

        await self._gate(cfg, skill_id, idempotency_key, principal)
        sem = self._sems.setdefault(agent_key, asyncio.Semaphore(cfg.max_concurrency))
        started = self._now_provider()
        try:
            async with sem:
                selected = mode or self._select_mode(card, skill_id)
                task = await self._dispatch(cfg, card, message, skill_id, selected)
        except A2AError as exc:
            # 仅远端侧失败计入熔断与预算连续失败；本地门禁拒绝不污染远端指标
            if isinstance(exc, (RemoteUnavailableError, RateLimitedError, AuthError,
                                ProtocolClientError, PeerNotFoundError, CapabilityError)):
                self._breaker(cfg).on_failure()
                self._usage(cfg).record_failure(now=self._now_provider())
                await self._record_call(
                    cfg, message, skill_id, idempotency_key, "FAILED",
                    started=started, error=str(exc),
                )
            raise
        self._breaker(cfg).on_success()
        usage = self._usage(cfg)
        usage.record_tool_call(now=self._now_provider())
        usage.record_remote_agent_call(cfg.key, now=self._now_provider())
        usage.record_success(now=self._now_provider())
        await self._record_call(
            cfg, message, skill_id, idempotency_key, task.status.value,
            started=started, task=task,
        )
        return task

    async def _dispatch(
        self,
        cfg: RemoteAgentConfig,
        card: AgentCard,
        message: Message,
        skill_id: str,
        mode: str,
    ) -> Task:
        base = cfg.base_url
        headers = {VERSION_HEADER: PROTOCOL_VERSION}
        if mode == "stream":
            url = validate_peer_url(
                base + "/a2a/message/stream",
                require_https=cfg.require_https, resolver=self.resolver,
            )
            resp = await self._request(cfg, "POST", url, json_body=message.model_dump(mode="json"), headers=headers)
            if resp.status_code != 200:
                await resp.aclose()
                raise self._http_error(cfg, resp)
            task_id = message.task_id or f"a2a-{uuid.uuid4().hex[:12]}"
            last_status: TaskStatus | None = None
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(line[len("data: "):])
                    except Exception:
                        continue
                    if payload.get("status") == "completed":  # done 帧
                        break
                    task_id = payload.get("task_id", task_id)
                    last_status = payload.get("status", last_status)
            finally:
                await resp.aclose()
            # 流结束后拉取终态 Task（远端已终态则保留观测到的状态）
            task = await self.get_task(cfg.key, task_id, principal="")
            if task is not None:
                return task
            now = self._now_provider()
            return Task(
                id=task_id[: _MAX_TASK_ID_CHARS],
                status=last_status or TaskStatus.WORKING,
                created_timestamp=now, last_updated_timestamp=now,
            )
        url = validate_peer_url(
            base + "/a2a/message/send",
            require_https=cfg.require_https, resolver=self.resolver,
        )
        resp = await self._request(cfg, "POST", url, json_body=message.model_dump(mode="json"), headers=headers)
        if resp.status_code != 200:
            await resp.aclose()
            raise self._http_error(cfg, resp)
        data = await self._read_capped(resp, MAX_RESPONSE_BYTES)
        try:
            result = TaskSendResult.model_validate(json.loads(data))
        except Exception as exc:
            raise ProtocolClientError("invalid TaskSendResult payload") from exc
        if result.task is None:
            if result.message is None:
                raise ProtocolClientError("empty TaskSendResult")
            return self._task_from_message(result.message)
        return self._sanitize_task(result.task)

    def _task_from_message(self, message: Message) -> Task:
        """消息级应答 → COMPLETED Task（历史保留该消息）。"""
        now = self._now_provider()
        return Task(
            id=message.task_id or f"a2a-{uuid.uuid4().hex[:12]}",
            status=TaskStatus.COMPLETED,
            history=[message],
            created_timestamp=now,
            last_updated_timestamp=now,
        )

    def _sanitize_task(self, task: Task) -> Task:
        """远端 Task 视为不可信：净化历史消息与 Artifact。"""
        try:
            validate_external_task(task)
        except InvalidInputError as exc:
            raise ProtocolClientError(f"remote response rejected: {exc}") from exc
        return task

    def _http_error(self, cfg: RemoteAgentConfig, resp: httpx.Response) -> A2AError:
        code = resp.status_code
        if code in (401, 403):
            return AuthError(f"remote auth failed: {code} (peer={cfg.key})")
        if code == 404:
            return PeerNotFoundError(f"remote resource not found (peer={cfg.key})")
        if code == 501:
            return CapabilityError(f"remote method not implemented (peer={cfg.key})")
        if code == 400:
            return ProtocolClientError(f"remote rejected request: {code}")
        return RemoteUnavailableError(f"remote error: {code} (peer={cfg.key})")

    async def _record_call(
        self,
        cfg: RemoteAgentConfig,
        message: Message,
        skill_id: str,
        idempotency_key: str,
        status: str,
        *,
        started: datetime,
        task: Task | None = None,
        error: str = "",
    ) -> None:
        """写入本地 Step Ledger（脱敏：只记协议元数据与摘要）。"""
        try:
            await self.ledger.record(
                A2ACallRecord(
                    peer=cfg.key,
                    task_id=(task.id if task else "") or message.task_id or "",
                    message_id=message.message_id,
                    skill_id=skill_id,
                    idempotency_key=idempotency_key,
                    status=status,
                    started_at=started,
                    finished_at=self._now_provider(),
                    artifact_count=len(task.artifacts) if task else 0,
                    error=error[:500],
                )
            )
        except Exception:
            logger.warning("[a2a_client] ledger record failed for peer=%s", cfg.key)

    # ══════════════════════════════════════════════════════════
    # Tasks：Get / List / Cancel
    # ══════════════════════════════════════════════════════════

    async def get_task(self, agent_key: str, task_id: str, *, principal: str = "") -> Task | None:
        cfg = self._ensure_peer(agent_key)
        url = validate_peer_url(
            f"{cfg.base_url}/a2a/tasks/{task_id}",
            require_https=cfg.require_https, resolver=self.resolver,
        )
        resp = await self._request(cfg, "GET", url, headers={VERSION_HEADER: PROTOCOL_VERSION})
        if resp.status_code == 404:
            await resp.aclose()
            return None
        if resp.status_code != 200:
            await resp.aclose()
            raise self._http_error(cfg, resp)
        data = await self._read_capped(resp, MAX_RESPONSE_BYTES)
        try:
            return self._sanitize_task(Task.model_validate(json.loads(data)))
        except Exception as exc:
            raise ProtocolClientError("invalid Task payload") from exc

    async def list_tasks(
        self, agent_key: str, *, status: str = "", limit: int = 50, principal: str = ""
    ) -> list[Task]:
        cfg = self._ensure_peer(agent_key)
        url = validate_peer_url(
            f"{cfg.base_url}/a2a/tasks/query",
            require_https=cfg.require_https, resolver=self.resolver,
        )
        resp = await self._request(
            cfg, "POST", url,
            json_body={"status": status, "history_length": 0},
            headers={VERSION_HEADER: PROTOCOL_VERSION},
        )
        if resp.status_code != 200:
            await resp.aclose()
            raise self._http_error(cfg, resp)
        data = await self._read_capped(resp, MAX_RESPONSE_BYTES)
        try:
            payload = json.loads(data)
        except Exception as exc:
            raise ProtocolClientError("invalid tasks payload") from exc
        tasks = payload.get("tasks", [])[: max(0, min(limit, 200))]
        return [self._sanitize_task(Task.model_validate(t)) for t in tasks]

    async def cancel(self, agent_key: str, task_id: str, *, principal: str = "") -> Task | None:
        cfg = self._ensure_peer(agent_key)
        url = validate_peer_url(
            f"{cfg.base_url}/a2a/tasks/{task_id}/cancel",
            require_https=cfg.require_https, resolver=self.resolver,
        )
        resp = await self._request(cfg, "POST", url, headers={VERSION_HEADER: PROTOCOL_VERSION})
        if resp.status_code == 404:
            await resp.aclose()
            return None
        if resp.status_code != 200:
            await resp.aclose()
            raise self._http_error(cfg, resp)
        data = await self._read_capped(resp, MAX_RESPONSE_BYTES)
        try:
            return self._sanitize_task(Task.model_validate(json.loads(data)))
        except Exception as exc:
            raise ProtocolClientError("invalid cancel payload") from exc

    # ══════════════════════════════════════════════════════════
    # Subscribe（SSE + Last-Event-ID 续传）
    # ══════════════════════════════════════════════════════════

    async def subscribe(
        self,
        agent_key: str,
        task_id: str,
        *,
        last_event_id: str = "",
        principal: str = "",
    ) -> AsyncIterator[TaskStatusUpdateEvent]:
        """订阅远端 Task 事件流；断流时抛 RemoteUnavailableError（可解释失败）。"""
        cfg = self._ensure_peer(agent_key)
        url = validate_peer_url(
            f"{cfg.base_url}/a2a/tasks/{task_id}/resubscribe",
            require_https=cfg.require_https, resolver=self.resolver,
        )
        headers = {VERSION_HEADER: PROTOCOL_VERSION}
        if last_event_id:
            headers["Last-Event-ID"] = str(last_event_id)
        await self._attach_auth(cfg, headers, url)
        try:
            async with self._http.stream(
                "POST", url, headers=headers,
                timeout=httpx.Timeout(cfg.timeout_seconds),
            ) as resp:
                if resp.status_code != 200:
                    raise self._http_error(cfg, resp)
                event: dict[str, str] = {}
                async for line in resp.aiter_lines():
                    if line.startswith("id: "):
                        event["event_id"] = line[len("id: "):].strip()
                    elif line.startswith("event: "):
                        event["event"] = line[len("event: "):].strip()
                    elif line.startswith("data: "):
                        event["data"] = line[len("data: "):].strip()
                        if event.get("event") == "task_status_update" and event.get("data"):
                            try:
                                payload = json.loads(event["data"])
                                yield TaskStatusUpdateEvent.model_validate(payload)
                            except Exception:
                                logger.warning("[a2a_client] invalid SSE payload from peer=%s", cfg.key)
                        event = {}
                    elif line == "":
                        event = {}
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(f"stream interrupted: {cfg.key}") from exc
