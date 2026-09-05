"""知识检索 Shadow 遥测与灰度推进（阶段七 10.1/10.3/10.4）。

职责：
  1. 决策知识检索模式 off / shadow / active，结合 KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED
     与 KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT 对 user_id 确定性分流；
  2. 记录新旧链路差异：产品路由差异、文档来源、token/字符差异、required 缺失、
     新链路异常与耗时（shadow 遥测）；
  3. 评估停止条件（10.4）：跨产品注入、required 丢失、索引版本不一致等 → 建议回滚。

模式语义：
  - off    ：完全走旧知识路径（无检索注入）
  - shadow ：新旧链路并行构建，LLM 仍用旧上下文，仅记录差异
  - active ：命中灰度用户，注入新检索上下文
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("backend.agent.knowledge_shadow")

RetrievalMode = Literal["off", "shadow", "active"]

# 停止条件阈值（10.4 经验值，可随评测调整）
STOP_FACT_PARAM: dict[str, float] = {
    "required_lost_ratio": 0.0,  # 新链路 required 缺失数 > 旧链路即视为 required 丢失
}


def knowledge_retrieval_mode(settings: Any) -> RetrievalMode:
    """根据配置判断知识检索全局模式。

    - KNOWLEDGE_RETRIEVAL_ENABLED 关闭 → off
    - SHADOW_ENABLED 开启 → shadow
    - 否则 → active
    """
    if not getattr(settings, "KNOWLEDGE_RETRIEVAL_ENABLED", False):
        return "off"
    if getattr(settings, "KNOWLEDGE_RETRIEVAL_SHADOW_ENABLED", False):
        return "shadow"
    return "active"


def user_in_retrieval_rollout(user_id: str, percent: int) -> bool:
    """按 user_id 确定性分流（知识检索灰度）。percent<=0 恒 False，>=100 恒 True。"""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    digest = __import__("hashlib").sha256((user_id or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100 < percent


def effective_retrieval_mode(settings: Any, user_id: str = "") -> RetrievalMode:
    """结合灰度后的实际模式：active 且未命中灰度 → off。"""
    mode = knowledge_retrieval_mode(settings)
    if mode == "active" and not user_in_retrieval_rollout(
        user_id, int(getattr(settings, "KNOWLEDGE_RETRIEVAL_ROLLOUT_PERCENT", 0))
    ):
        return "off"
    return mode


@dataclass
class RetrievalShadowDiff:
    """一次新旧链路对比的 shadow 遥测记录。"""

    purpose: str = ""
    user_id: str = ""
    query: str = ""
    product_ids: list[str] = field(default_factory=list)
    index_version: str = ""  # 请求时传入的期望索引版本
    new_index_version: str = ""  # 新链路实际使用的索引版本
    # 新旧链路文档来源
    old_source_docs: list[str] = field(default_factory=list)
    new_source_docs: list[str] = field(default_factory=list)
    # 字符/token 差异（以字符数近似）
    old_char_count: int = 0
    new_char_count: int = 0
    # required 缺失
    required_missing_old: list[str] = field(default_factory=list)
    required_missing_new: list[str] = field(default_factory=list)
    # 新链路异常与耗时
    error: str = ""
    latency_ms: float = 0.0

    @property
    def char_delta(self) -> int:
        return self.new_char_count - self.old_char_count

    @property
    def required_lost(self) -> list[str]:
        """新链路相对旧链路新增的 required 缺失。"""
        return [k for k in self.required_missing_new if k not in self.required_missing_old]

    @property
    def cross_product_terms(self) -> None:
        """占位：跨产品术语检测由生成后事实审查（S6-5）补充；此处保持 None。"""
        return None


def record_retrieval_shadow(diff: RetrievalShadowDiff) -> None:
    """记录一次 shadow 差异（结构化日志）。"""
    extras = f" required_lost={diff.required_lost}" if diff.required_lost else ""
    if diff.error:
        logger.warning(
            "retrieval shadow error purpose=%s user=%s err=%s"
            " old_chars=%d new_chars=%d delta=%+d ms=%.1f%s",
            diff.purpose,
            diff.user_id,
            diff.error,
            diff.old_char_count,
            diff.new_char_count,
            diff.char_delta,
            diff.latency_ms,
            extras,
        )
        return
    logger.info(
        "retrieval shadow purpose=%s user=%s index=%s"
        " old_chars=%d new_chars=%d delta=%+d old_docs=%d new_docs=%d"
        " required_old=%d required_new=%d ms=%.1f%s",
        diff.purpose,
        diff.user_id,
        diff.index_version or "-",
        diff.old_char_count,
        diff.new_char_count,
        diff.char_delta,
        len(diff.old_source_docs),
        len(diff.new_source_docs),
        len(diff.required_missing_old),
        len(diff.required_missing_new),
        diff.latency_ms,
        extras,
    )


def evaluate_stop_conditions(diff: RetrievalShadowDiff) -> list[str]:
    """评估是否触发停止/回滚条件（10.4）。

    返回触发的条件描述列表；空列表表示无需回滚。
    - required 文档丢失：新链路 required 缺失数相对旧链路增加
    - 索引版本不一致：请求 index_version 存在但新链路 index_version 与之不符
    - 新链路异常：error 非空
    """
    conditions: list[str] = []
    if diff.required_lost:
        conditions.append(f"required_docs_lost: {','.join(diff.required_lost)}")
    # 索引版本不一致：请求期望版本与生效版本都存在且不一致
    if (
        diff.index_version
        and diff.new_index_version
        and (diff.index_version != diff.new_index_version)
    ):
        conditions.append(
            f"index_version_mismatch: expected={diff.index_version} actual={diff.new_index_version}"
        )
    if diff.error:
        conditions.append(f"new_chain_error: {diff.error}")
    return conditions


class RetrievalShadowTracker:
    """帮助在调用方以上下文管理器方式记录 shadow 差异与耗时。"""

    def __init__(self, **init_kwargs: Any):
        self._diff = RetrievalShadowDiff(**init_kwargs)
        self._start = time.perf_counter()

    def finish(
        self,
        *,
        old_char_count: int,
        new_char_count: int,
        old_source_docs: list[str] | None = None,
        new_source_docs: list[str] | None = None,
        required_missing_old: list[str] | None = None,
        required_missing_new: list[str] | None = None,
        new_index_version: str = "",
        error: str = "",
    ) -> RetrievalShadowDiff:
        self._diff.old_char_count = old_char_count
        self._diff.new_char_count = new_char_count
        if old_source_docs is not None:
            self._diff.old_source_docs = list(old_source_docs)
        if new_source_docs is not None:
            self._diff.new_source_docs = list(new_source_docs)
        if required_missing_old is not None:
            self._diff.required_missing_old = list(required_missing_old)
        if required_missing_new is not None:
            self._diff.required_missing_new = list(required_missing_new)
        if new_index_version:
            self._diff.new_index_version = new_index_version
        self._diff.error = error
        self._diff.latency_ms = (time.perf_counter() - self._start) * 1000.0
        return self._diff
