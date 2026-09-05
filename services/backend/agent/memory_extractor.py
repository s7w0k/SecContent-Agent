"""RuntimeMemoryExtractor — 阶段四 4A Step 4A-7。

从自主 Agent 运行中提取可复用的运行记忆（确定性规则，不依赖 LLM）：
  - 只提取：明确偏好、项目约束、已验证方案、失败模式；
  - 每条记忆带来源（run/step/工具）、置信度、作用域（user/project/repository/thread）
    和过期时间；
  - 不持久化提示注入、临时秘密、访问令牌和私有思维链（输入过滤 + 内容脱敏）；
  - 记忆召回按 user/project/repository/thread 作用域隔离（scope_value 过滤）。

安全约束（代码级强制，不可被输入绕过）：
  1. 内容中出现敏感键（api_key/token/password/secret/credential/authorization/cookie）
     直接丢弃该条候选，绝不下沉到记忆；
  2. 提示注入特征（ignore previous/system prompt 改写等）直接丢弃；
  3. 私有思维链文本（chain-of-thought / 思考过程）直接丢弃；
  4. content 只保存脱敏摘要，长度受上限约束。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 默认记忆有效期（天）
DEFAULT_TTL_DAYS = 90
# 失败模式记忆保留较短（快速失效，避免噪声累积）
FAILURE_TTL_DAYS = 30

# 敏感键：出现即丢弃（与 policy_engine.SENSITIVE_KEYS 保持一致）
SENSITIVE_KEYS = frozenset(
    {"api_key", "token", "password", "secret", "credential", "authorization", "cookie"}
)

# 提示注入特征（大小写不敏感子串）
_INJECTION_PATTERNS = (
    "ignore previous",
    "ignore all previous",
    "disregard previous",
    "system prompt",
    "系统提示",
    "忽略之前",
    "请忽略",
    "不要理会之前的指令",
    "override your instructions",
    "jailbreak",
)

# 私有思维链特征
_COT_PATTERNS = (
    "chain of thought",
    "chain-of-thought",
    "思考过程",
    "思维链",
    "内部推理",
    "private reasoning",
)

# 明确偏好信号词
_PREFERENCE_SIGNALS = (
    "prefer",
    "偏好",
    "希望",
    "总是",
    "不要",
    "避免",
    "喜欢",
)
# 项目约束信号词
_CONSTRAINT_SIGNALS = (
    "约束",
    "必须",
    "禁止",
    "限制",
    "仅允许",
    "只允许",
    "cannot",
    "must not",
    "only allow",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeMemoryKind(StrEnum):
    """自主运行记忆类别。"""

    PREFERENCE = "preference"  # 明确偏好
    CONSTRAINT = "constraint"  # 项目约束
    SOLUTION = "solution"  # 已验证方案
    FAILURE = "failure"  # 失败模式


class RuntimeMemoryScope(StrEnum):
    """记忆作用域（召回按此隔离）。"""

    USER = "user"
    PROJECT = "project"
    REPOSITORY = "repository"
    THREAD = "thread"


class RuntimeMemoryRecord(BaseModel):
    """单条可审计的自主运行记忆（脱敏）。"""

    model_config = ConfigDict(extra="ignore")

    memory_id: str
    user_id: str
    kind: RuntimeMemoryKind
    content: str = Field(..., max_length=500)
    scope: RuntimeMemoryScope = RuntimeMemoryScope.USER
    scope_value: str = ""  # 作用域取值：如 repository="repoA" / thread="t1"
    source_run_id: str = ""
    source_step_id: str = ""
    source_tool: str = ""
    confidence: float = Field(ge=0, le=1)
    refs: list[str] = Field(default_factory=list)  # 证据引用哈希（脱敏）
    expires_at: datetime
    created_at: datetime = Field(default_factory=_utc_now)


class MemorySignal(BaseModel):
    """一条待提取的运行信号（来源受信任，内容按安全规则过滤）。"""

    run_id: str
    user_id: str
    step_id: str = ""
    tool_name: str = ""
    text: str = Field(..., max_length=2000)
    kind_hint: RuntimeMemoryKind | None = None  # 调用方已做语义分类则直接采用
    outcome: str = ""  # success / failed / skipped / approval
    scope: RuntimeMemoryScope = RuntimeMemoryScope.USER
    scope_value: str = ""
    ref: str = ""  # 证据哈希引用


class MemoryExtractionResult(BaseModel):
    """提取结果。"""

    records: list[RuntimeMemoryRecord] = Field(default_factory=list)
    dropped: list[dict[str, str]] = Field(default_factory=list)  # 被安全规则丢弃的候选


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in patterns)


class RuntimeMemoryExtractor:
    """确定性记忆提取器（无外部依赖，纯规则可测）。"""

    def __init__(
        self,
        *,
        preference_ttl_days: int = DEFAULT_TTL_DAYS,
        failure_ttl_days: int = FAILURE_TTL_DAYS,
        now_provider: Any | None = None,
    ):
        self.preference_ttl_days = max(1, preference_ttl_days)
        self.failure_ttl_days = max(1, failure_ttl_days)
        self._now = now_provider or _utc_now

    # ── 公开接口 ──────────────────────────────────────────────

    def extract(self, signals: list[MemorySignal]) -> MemoryExtractionResult:
        """对一组信号做安全过滤与记忆化。"""
        records: list[RuntimeMemoryRecord] = []
        dropped: list[dict[str, str]] = []
        now = self._now()

        for sig in signals:
            # 安全规则先于分类：空内容跳过；注入/敏感/思维链直接丢弃，
            # 与能否分类无关（即使无法记忆化也不允许原始文本被处理/记录）
            if not sig.text or not sig.text.strip():
                continue
            drop_reason = self._security_check(sig.text)
            if drop_reason:
                dropped.append(
                    {"step_id": sig.step_id, "tool": sig.tool_name, "reason": drop_reason}
                )
                continue
            kind = sig.kind_hint or self._classify(sig)
            if kind is None:
                continue
            content = self._sanitize_content(sig.text)
            if not content:
                continue
            ttl_days = (
                self.failure_ttl_days
                if kind == RuntimeMemoryKind.FAILURE
                else self.preference_ttl_days
            )
            records.append(
                RuntimeMemoryRecord(
                    memory_id="mem-" + uuid.uuid4().hex[:12],
                    user_id=sig.user_id,
                    kind=kind,
                    content=content,
                    scope=sig.scope,
                    scope_value=sig.scope_value,
                    source_run_id=sig.run_id,
                    source_step_id=sig.step_id,
                    source_tool=sig.tool_name,
                    confidence=self._confidence(kind, sig),
                    refs=[sig.ref] if sig.ref else [],
                    expires_at=now + timedelta(days=ttl_days),
                    created_at=now,
                )
            )
        return MemoryExtractionResult(records=records, dropped=dropped)

    # ── 分类与置信度 ──────────────────────────────────────────

    def _classify(self, sig: MemorySignal) -> RuntimeMemoryKind | None:
        """按文本信号与执行结果分类（确定性规则）。"""
        if sig.outcome in ("failed", "skipped") or (
            sig.outcome == ""
            and any(
                w in sig.text.lower()
                for w in ("失败", "报错", "error", "failed", "超时", "timeout")
            )
        ):
            return RuntimeMemoryKind.FAILURE
        if any(s in sig.text for s in _PREFERENCE_SIGNALS):
            return RuntimeMemoryKind.PREFERENCE
        if any(s in sig.text for s in _CONSTRAINT_SIGNALS):
            return RuntimeMemoryKind.CONSTRAINT
        if sig.outcome == "success" and sig.ref:
            return RuntimeMemoryKind.SOLUTION
        return None

    @staticmethod
    def _confidence(kind: RuntimeMemoryKind, sig: MemorySignal) -> float:
        if sig.kind_hint is not None:
            return 0.9
        return {
            RuntimeMemoryKind.PREFERENCE: 0.7,
            RuntimeMemoryKind.CONSTRAINT: 0.8,
            RuntimeMemoryKind.SOLUTION: 0.75,
            RuntimeMemoryKind.FAILURE: 0.8,
        }[kind]

    # ── 安全规则（不可被输入绕过）─────────────────────────────

    def _security_check(self, text: str) -> str:
        """返回丢弃原因（空串 = 通过）。优先级：敏感键 > 注入 > 思维链。"""
        lowered = text.lower()
        # 1. 敏感键：api_key= / token: / Bearer <...> 等赋值形式
        for key in SENSITIVE_KEYS:
            if re.search(rf"{re.escape(key)}\s*[=:：]\s*\S+", lowered) or re.search(
                rf"\b{re.escape(key)}\b", lowered
            ):
                return "sensitive_key"
        if re.search(r"bearer\s+[a-z0-9._-]{16,}", lowered):
            return "access_token"
        if re.search(r"\b[a-f0-9]{32,}\b", lowered):
            return "credential_like_hash"
        # 2. 提示注入
        if _contains_any(text, _INJECTION_PATTERNS):
            return "prompt_injection"
        # 3. 私有思维链
        if _contains_any(text, _COT_PATTERNS):
            return "private_cot"
        return ""

    @staticmethod
    def _sanitize_content(text: str) -> str:
        """内容脱敏：截断、压缩空白、去除换行。"""
        cleaned = re.sub(r"\s+", " ", text).strip()
        return cleaned[:500]
