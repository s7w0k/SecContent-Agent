"""Canary / Shadow 的确定性、sticky 路由（§48-51）。

不用 random.random()；用 stable_hash(seed, tenant_id, user_id) % 100 决定桶，
保证同一 tenant:user 稳定（§50-51）。rollout 单调：增至更大比例时旧子集是新子集的子集。
"""

from __future__ import annotations

import hashlib

from agent.execution.contracts import ExecutionEngine, ExecutionRequest


def stable_hash(seed: str, tenant_id: str, user_id: str) -> int:
    """对 tenant:user 生成 0..2^64-1 的稳定散列桶。"""
    key = f"{seed}|{tenant_id}|{user_id}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


def rollout_bucket(percent: int, seed: str, tenant_id: str, user_id: str) -> int:
    """返回 0..99 的桶号；``bucket < percent`` 时进入 skill 侧。"""
    return stable_hash(seed, tenant_id, user_id) % 100


class LevelCanaryRollout:
    """多级 Canary 投放：percent 每级递增，保证单调、sticky、可逆（§53-57）。"""

    def __init__(self, *, percent: int = 0, seed: str = "seccontent-agent-v1") -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"canary percent out of range: {percent}")
        self.percent = percent
        self.seed = seed

    def choose(self, request: ExecutionRequest) -> ExecutionEngine:
        if self.percent <= 0:
            return "legacy"
        if self.percent >= 100:
            return "skill_planned"
        bucket = rollout_bucket(self.percent, self.seed, request.tenant_id, request.user_id)
        return "skill_planned" if bucket < self.percent else "legacy"

    def is_sticky(self, percent: int) -> bool:
        """当前 percent 配置越级调大时，旧子集是否仍 ⊆ 新子集（§96）。"""
        return 0 <= percent <= 100 and percent >= self.percent


class ShadowSampler:
    """Shadow 抽样（§117）：AGENT_SHADOW_SAMPLE_PERCENT 控制是否双跑。"""

    def __init__(self, *, sample_percent: int = 100, seed: str = "shadow-sampling-v1") -> None:
        if not 0 <= sample_percent <= 100:
            raise ValueError(f"shadow sample percent out of range: {sample_percent}")
        self.sample_percent = sample_percent
        self.seed = seed

    def should_sample(self, request: ExecutionRequest) -> bool:
        if self.sample_percent >= 100:
            return True
        if self.sample_percent <= 0:
            return False
        return (
            rollout_bucket(self.sample_percent, self.seed, request.tenant_id, request.user_id)
            < self.sample_percent
        )


__all__ = [
    "LevelCanaryRollout",
    "ShadowSampler",
    "rollout_bucket",
    "stable_hash",
]
