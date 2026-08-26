"""LLM Wiki Knowledge Layer（Agent-Native Product Wiki）。

遵循 SecContent-Agent LLM Wiki 实施计划：
- Knowledge Plane（编译/校验/发布）与 Runtime Plane（导航/取证/推理）分离
- 业务 Agent 只消费可追溯的 EvidenceBundle
- 编译知识一次、持久化组织；运行时由 Agent 主动导航 Wiki
"""

from __future__ import annotations

__version__ = "0.2.0"

APPROVED_PAGE_TYPES = frozenset(
    {
        "product",
        "capability",
        "scenario",
        "integration",
        "limitation",
        "positioning",
        "concept",
        "competitor",
        "synthesis",
        "overview",
    }
)

# 公开子模块入口（按需惰性导入，避免循环依赖）
from agent.wiki import conflict_detector, maintainer, telemetry  # noqa: E402

__all__ = [
    "APPROVED_PAGE_TYPES",
    "conflict_detector",
    "maintainer",
    "telemetry",
]
