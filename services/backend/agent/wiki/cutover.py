"""Production Cutover 助手（Final Plan PR-C，§7.6）。

Canary 分桶：
  - 使用稳定 hash（user_id / trace_id）分桶，绝不每次随机。
  - `in_canary(seed, percent)` 判断某请求是否命中 wiki 灰度桶（percent∈[0,100]）。
  - 便于 5% → 20% → 50% → 100% 的渐进切流；命中桶外的请求仍走旧后端。

注意：wiki 请求无法完成时必须显式报错 / NO_SCORE，禁止静默回退 legacy（§7.7）。
"""

from __future__ import annotations

import hashlib


def stable_bucket(seed: str | None, buckets: int = 100) -> int:
    """把任意 seed（user_id / trace_id / 空串）映射到 [0, buckets)。

    哈希稳定：同 seed 永远同一桶（可复现、可回滚）。
    """
    seed = str(seed or "")
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % buckets


def in_canary(seed: str | None, percent: float, buckets: int = 100) -> bool:
    """是否命中 wiki 灰度桶（percent∈[0,100]）。percent<=0 永远 False；>=100 恒 True。"""
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    return stable_bucket(seed, buckets) < (round((buckets * percent) / 100))
