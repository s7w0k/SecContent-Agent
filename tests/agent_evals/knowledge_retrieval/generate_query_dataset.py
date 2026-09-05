"""真实线上用户 query 语料评测集生成器（阶段0 增强 S0-2v3）。

按"真实线上用户搜索 query"形态，生成大规模短 query 评测输入（`dataset.query.jsonl`）。

与 v1（合成文章）/ v2（多角色长文）不同，本评测集：
- 输入 = 单条短 query（贴合真实搜索框，无标题/摘要/正文拆分）
- 覆盖真正线检索高频词、口语化提问、缩写、中英混排、同义近义、竞品词、无命中
- 自动标注 expected/forbidden 真值，喂给 ProductRoutingService.resolve(auto)

query 以 `article.title` 形式传入（ProductMatcher 中 title 字段权重最高，最贴合真实检索）。

用法：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.knowledge_retrieval.generate_query_dataset
    python -m tests.agent_evals.knowledge_retrieval.evaluator --dataset query --report
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUTPUT = Path(__file__).parent / "dataset.query.jsonl"

PUBLISHED = ["agent-identity-security", "agent-security", "ai-bom"]

# ── 产品识别强词（与 product_catalog.py keywords 对齐，用于自动派生 expected） ──
_STRONG: dict[str, list[str]] = {
    "agent-identity-security": [
        "身份认证",
        "身份治理",
        "授权",
        "最小权限",
        "权限边界",
        "凭证",
        "密钥",
        "单点登录",
        "SSO",
        "委托授权",
        "反冒用",
        "agent身份",
    ],
    "agent-security": [
        "智能体防护",
        "智能体运行时",
        "agent防护",
        "agent runtime",
        "智能体检测",
        "运行时防护",
        "沙箱",
        "提示词注入",
        "数据泄露",
        "行为分析",
        "异常检测",
        "威胁检测",
        "进程隔离",
        "agent安全",
    ],
    "ai-bom": [
        "AI资产",
        "AI组件",
        "AI供应链",
        "模型供应链",
        "物料清单",
        "SBOM",
        "模型来源",
        "数据血缘",
        "依赖图谱",
        "资产台账",
        "AI-BOM",
    ],
}

# ── 混扰弱词（命中但不构成强产品信号，用于 forbidden 隔离压力） ──
_WEAK = (
    "安全",
    "防护",
    "检测",
    "治理",
    "风险",
    "合规",
    "数据",
    "模型",
    "组件",
    "AI",
    "智能体",
    "供应链安全",
)

# ── 真实 query 语料表：(query, expected, requires_expansion, forbidden覆盖) ──
# forbidden 默认 = 除 expected 外全部已发布产品；None 表示沿用默认。
# expected=[] 表示无命中（不得编造产品）。
_QUERIES: list[tuple[str, list[str], bool, list[str] | None]] = [
    # ── 智能体身份安全（agent-identity-security） ──────────────
    ("Agent 最小权限怎么落地", ["agent-identity-security"], True, None),
    ("智能体身份认证方案", ["agent-identity-security"], False, None),
    ("agent 身份治理最佳实践", ["agent-identity-security"], False, None),
    ("多智能体权限边界怎么切", ["agent-security", "agent-identity-security"], True, ["ai-bom"]),
    ("Agent 凭证管理", ["agent-identity-security"], False, None),
    ("智能体密钥托管", ["agent-identity-security"], False, None),
    ("Agent SSO 单点登录接入", ["agent-identity-security"], False, None),
    ("委托授权在 Agent 里怎么做", ["agent-identity-security"], True, None),
    ("反冒用机制", ["agent-identity-security"], False, None),
    ("agent 身份认证 防冒用", ["agent-identity-security"], False, None),
    ("智能体身份安全 认证授权", ["agent-identity-security"], False, None),
    ("最小权限 Agent 落地", ["agent-identity-security"], True, None),
    ("智能体 权限边界 越权", ["agent-identity-security"], False, None),
    ("Agent 动态授权", ["agent-identity-security"], False, None),
    ("agent identity security", ["agent-identity-security"], False, None),
    ("Agent 身份认证 授权", ["agent-identity-security"], False, None),
    ("智能体身份治理 最小权限", ["agent-identity-security"], True, None),
    ("Agent 凭证 密钥 安全", ["agent-identity-security"], False, None),
    ("agent 身份 反冒用", ["agent-identity-security"], False, None),
    ("智能体身份安全平台", ["agent-identity-security"], False, None),
    ("Agent 权限过大 授权怎么管", ["agent-identity-security"], True, None),
    ("智能体身份认证 最小权限 授权", ["agent-identity-security"], False, None),
    ("agent 身份 凭证 越权", ["agent-identity-security"], False, None),
    ("智能体身份安全 权限 反冒用", ["agent-identity-security"], False, None),
    # ── 智能体安全（agent-security） ──────────────────────────
    ("智能体运行时防护", ["agent-security"], True, None),
    ("提示词注入怎么防", ["agent-security"], False, None),
    ("Agent 沙箱隔离", ["agent-security"], False, None),
    ("智能体安全检测", ["agent-security"], False, None),
    ("agent 行为分析", ["agent-security"], False, None),
    ("智能体 异常检测", ["agent-security"], False, None),
    ("Agent 威胁检测", ["agent-security"], False, None),
    ("智能体 进程隔离", ["agent-security"], False, None),
    ("agent 数据泄露防护", ["agent-security"], False, None),
    ("智能体安全平台 检测防护", ["agent-security"], True, None),
    ("agent runtime 防护", ["agent-security"], False, None),
    ("多智能体 运行时安全", ["agent-security"], False, None),
    ("智能体提示词注入 应急", ["agent-security"], False, None),
    ("Agent 沙箱 异常检测", ["agent-security"], False, None),
    ("agent 安全 威胁情报", ["agent-security"], False, None),
    ("智能体安全 数据泄露", ["agent-security"], False, None),
    ("agent 防护 行为分析", ["agent-security"], False, None),
    ("智能体运行时 检测 防护", ["agent-security"], True, None),
    ("Agent 安全隔离", ["agent-security"], False, None),
    ("智能体安全平台", ["agent-security"], False, None),
    ("智能体提示词注入 防护 沙箱", ["agent-security"], True, None),
    ("agent 运行时 威胁检测", ["agent-security"], False, None),
    ("智能体 数据泄露 应急", ["agent-security"], False, None),
    # ── AI-BOM（ai-bom） ─────────────────────────────────────
    ("AI 资产怎么盘点", ["ai-bom"], False, None),
    ("AI 组件清单", ["ai-bom"], False, None),
    ("AI 供应链安全", ["ai-bom"], False, None),
    ("模型供应链管理", ["ai-bom"], False, None),
    ("AI 物料清单 SBOM", ["ai-bom"], True, None),
    ("模型来源审计", ["ai-bom"], False, None),
    ("AI 数据血缘", ["ai-bom"], False, None),
    ("依赖图谱 资产台账", ["ai-bom"], False, None),
    ("AI-BOM 模型商店", ["ai-bom"], False, None),
    ("AI 组件 供应链 安全", ["ai-bom"], False, None),
    ("模型供应链 审计", ["ai-bom"], False, None),
    ("AI 资产 物料清单", ["ai-bom"], True, None),
    ("AI 供应链 依赖图谱", ["ai-bom"], False, None),
    ("AI-BOM 资产台账", ["ai-bom"], False, None),
    ("AI 组件 来源 血缘", ["ai-bom"], False, None),
    ("模型商店 供应链", ["ai-bom"], False, None),
    ("AI 物料清单 怎么建", ["ai-bom"], False, None),
    ("AI 资产 台账 管理", ["ai-bom"], False, None),
    ("AI 供应链 模型来源", ["ai-bom"], False, None),
    ("AI-BOM 管理方案", ["ai-bom"], False, None),
    ("AI 组件 供应链 审计 台账", ["ai-bom"], False, None),
    ("模型供应链 依赖 血缘", ["ai-bom"], False, None),
    ("AI 资产 模型 来源 清单", ["ai-bom"], False, None),
    # ── 多产品（一个 query 命中多个产品） ────────────────────
    ("智能体身份认证 + 运行时防护", ["agent-identity-security", "agent-security"], False, None),
    ("智能体安全 从身份到运行时", ["agent-identity-security", "agent-security"], True, None),
    ("AI 资产 和 智能体防护一起管", ["ai-bom", "agent-security"], False, None),
    (
        "智能体安全三件套 身份 运行时 清单",
        ["agent-identity-security", "agent-security", "ai-bom"],
        False,
        None,
    ),
    ("Agent 身份 + 沙箱", ["agent-identity-security", "agent-security"], False, None),
    ("智能体供应链 资产 防护", ["ai-bom", "agent-security"], False, None),
    ("AI 物料清单 和 智能体安全", ["ai-bom", "agent-security"], False, None),
    ("智能体身份认证 AI 资产", ["agent-identity-security", "ai-bom"], False, None),
    ("Agent 权限 + 提示词注入", ["agent-identity-security", "agent-security"], True, None),
    (
        "智能体安全 全栈 身份 资产 运行时",
        ["agent-identity-security", "agent-security", "ai-bom"],
        False,
        None,
    ),
    # ── 无命中（不得编造产品） ───────────────────────────────
    ("今天天气怎么样", [], False, []),
    ("公司组织架构调整怎么办", [], False, []),
    ("如何提升团队协作效率", [], False, []),
    ("AI 落地一年来的感受", [], False, []),
    ("数据治理和合规", [], False, []),
    ("企业数字化转型", [], False, []),
    ("网络安全行业趋势", [], False, []),
    ("怎么给领导写周报", [], False, []),
    ("开源社区怎么运营", [], False, []),
    ("项目管理工具推荐", [], False, []),
    # ── 竞品/边界（forbidden 隔离压力） ─────────────────────
    ("Agent 身份认证 相比传统 SSE", ["agent-identity-security"], False, ["ans", "agent-security"]),
    ("ANS 网络服务", [], False, []),  # ANS 未发布，不应作为正式路由结果
    ("智能体安全网关", ["agent-security"], False, ["ans", "ai-bom"]),  # 网关未发布，路由到运行时
    ("智能体供应链安全", ["ai-bom", "agent-security"], False, ["agent-identity-security"]),
    ("agent 安全 供应链", ["ai-bom", "agent-security"], False, ["agent-identity-security"]),
    ("威胁检测 身份认证", ["agent-security", "agent-identity-security"], False, ["ai-bom"]),
    ("智能体 数据 治理 方案", [], False, []),
    ("安全 防护 检测 平台", [], False, []),
    ("智能体安全 身份认证 二选一", ["agent-identity-security", "agent-security"], False, None),
    ("AI 组件 模型 供应链 治理", ["ai-bom"], False, ["agent-security"]),
]

# 无命中专用禁用（不列入 forbidden 时保持空）
_NO_HIT_FORBIDDEN = {"agent-identity-security", "agent-security", "ai-bom"}

# 产品 → 章节文档路径（用于 required_doc_ids）
_DOC_BY_PRODUCT = {
    "agent-identity-security": ["1-智能体身份安全/overview.md"],
    "agent-security": ["2-智能体安全/overview.md"],
    "ai-bom": ["3-AI-BOM/overview.md"],
}


def _derive_forbidden(expected: list[str]) -> list[str]:
    """默认 forbidden = 已发布产品中除 expected 外全部，避免漏隔离。"""
    if not expected:
        return []
    return [p for p in PUBLISHED if p not in expected]


def _case(
    case_id: str,
    query: str,
    expected: list[str],
    *,
    requires_expansion: bool = False,
    forbidden: list[str] | None = None,
) -> dict[str, Any]:
    fb = list(_derive_forbidden(expected)) if forbidden is None else list(forbidden)
    required = []
    if requires_expansion and expected:
        # 章节展开需标注期望产品首个的章节文档
        required = list(_DOC_BY_PRODUCT.get(expected[0], []))
    return {
        "case_id": case_id,
        "mode": "auto",
        "query": query,
        "input_type": "user-query",
        "expected_product_ids": expected,
        "required_doc_ids": required,
        "forbidden_product_ids": fb,
        "requires_expansion": requires_expansion,
        "allowed_product_claims": [],
        "article": {
            "title": query,
            "summary_cn": "",
            "content_md": "",
            "category_v2": "用户检索",
            "tags": [],
            "source": "user-query",
        },
    }


def _build_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for i, (query, expected, expand, forbidden) in enumerate(_QUERIES, start=1):
        samples.append(
            _case(
                f"q-{i:03d}",
                query,
                expected,
                requires_expansion=expand,
                forbidden=forbidden,
            )
        )
    return samples


def generate() -> list[dict[str, Any]]:
    """生成并写入 dataset.query.jsonl。"""
    samples = _build_samples()
    OUTPUT.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n",
        encoding="utf-8",
    )
    return samples


def summary(samples: list[dict[str, Any]]) -> None:
    """打印语料分布摘要。"""
    from collections import Counter

    exp = Counter(tuple(sorted(c["expected_product_ids"])) for c in samples)
    print(f"总条数: {len(samples)}")
    print(f"单产品 agent-identity-security: {exp[('agent-identity-security',)]}")
    print(f"单产品 agent-security: {exp[('agent-security',)]}")
    print(f"单产品 ai-bom: {exp[('ai-bom',)]}")
    multi = sum(v for k, v in exp.items() if len(k) >= 2)
    nohit = exp[()]
    print(
        f"多产品: {multi}  无命中: {nohit}  章节展开: {sum(1 for c in samples if c['requires_expansion'])}"
    )


if __name__ == "__main__":
    s = generate()
    summary(s)
