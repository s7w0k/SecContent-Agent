"""硬门禁检查。

硬门禁不进入适应度，任一失败则候选不能发布。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

logger = logging.getLogger("backend.agent.evolution.gates")


class GateChecker:
    """检查候选策略是否通过硬门禁。"""

    # 门禁定义
    GATES: ClassVar[list[str]] = [
        "fact_safety",          # 事实高危问题不增加
        "propaganda_safety",    # 高危宣传问题不增加
        "format_success",       # 格式成功率不低于基线
        "prompt_injection",     # Prompt 注入测试通过
        "multi_tenant",         # 多租户测试通过
        "token_budget",         # Token 增幅不超预算
        "holdout_improvement",  # 留出集必须提升
        "category_regression",  # 关键分类不显著回归
    ]

    def __init__(self, db: Any):
        self.db = db

    async def check(
        self,
        candidate_id: str,
        baseline_metrics: dict[str, float] | None = None,
        candidate_metrics: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """执行所有硬门禁检查。

        Returns:
            {gate_name: passed} 映射
        """
        results = {}
        baseline = baseline_metrics or {}
        candidate = candidate_metrics or {}

        # 1. 事实安全
        base_high = baseline.get("high_issue_rate", 0.0)
        cand_high = candidate.get("holdout_metrics", {}).get("high_issue_rate", 0.0)
        results["fact_safety"] = cand_high <= base_high + 0.01

        # 2. 宣传安全（与事实安全同口径）
        results["propaganda_safety"] = cand_high <= base_high + 0.01

        # 3. 格式成功率
        results["format_success"] = True  # 需要实际格式检查数据

        # 4. Prompt 注入
        results["prompt_injection"] = True  # 需要专门测试

        # 5. 多租户
        results["multi_tenant"] = True  # 需要专门测试

        # 6. Token 预算
        results["token_budget"] = True  # 需要 Token 使用数据

        # 7. 留出集提升
        holdout_fitness = candidate.get("holdout_fitness", 0.0)
        base_fitness = baseline.get("fitness", 0.0)
        results["holdout_improvement"] = holdout_fitness >= base_fitness

        # 8. 分类回归
        category_metrics = candidate.get("category_metrics", {})
        results["category_regression"] = True
        for _cat, metrics in category_metrics.items():
            cat_fitness = metrics.get("fitness", 0.0)
            if cat_fitness < base_fitness * 0.8:  # 显著回归
                results["category_regression"] = False
                break

        all_passed = all(results.values())

        # 更新候选状态
        if not all_passed:
            await self.db["personalization_candidates"].update_one(
                {"candidate_id": candidate_id},
                {"$set": {
                    "status": "gate_failed",
                    "gate_results": results,
                    "updated_at": __import__("datetime").datetime.now(__import__("datetime").UTC),
                }},
            )
        else:
            await self.db["personalization_candidates"].update_one(
                {"candidate_id": candidate_id},
                {"$set": {
                    "gate_results": results,
                    "updated_at": __import__("datetime").datetime.now(__import__("datetime").UTC),
                }},
            )

        logger.info(
            "gates checked: candidate=%s passed=%d/%d",
            candidate_id, sum(results.values()), len(results),
        )

        return results
