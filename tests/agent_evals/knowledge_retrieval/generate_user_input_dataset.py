"""多角色真实用户输入评测集生成器（阶段0 增强 S0-2v2）。

通过"扮演真实用户"生成多样化、更接近现实的产品路由评测输入，
每条自动标注期望产品真值，产出 `dataset.v2.jsonl`。

设计原则：
- 输入 = 用户真实发文形态（标题/摘要/正文），覆盖不同角色与口吻
- 真值 = 由产品目录关键词 + 知识库内容人工审定的 expected/forbidden
- 与 deterministic_checks 的字段契约完全兼容（见 dataset.v1）

用法：
    cd pr-agent-demo-v2
    python -m tests.agent_evals.knowledge_retrieval.generate_user_input_dataset
    # 生成后运行评测：
    python -m pytest tests/agent_evals/knowledge_retrieval/test_eval.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUTPUT = Path(__file__).parent / "dataset.v2.jsonl"

# 产品目录稳定 ID（与 product_catalog.py 对应，仅已发布产品）
PUBLISHED = ["agent-identity-security", "agent-security", "ai-bom"]


def _doc(product: str) -> list[str]:
    """产品 → 知识库文档路径（用于 required_doc_ids）。"""
    mapping = {
        "agent-identity-security": ["1-智能体身份安全/overview.md"],
        "agent-security": ["2-智能体安全/overview.md"],
        "ai-bom": ["3-AI-BOM/overview.md"],
    }
    return mapping.get(product, [])


def _case(
    case_id: str,
    role: str,
    tone: str,
    title: str,
    summary_cn: str,
    content_md: str,
    expected: list[str],
    *,
    forbidden: list[str] | None = None,
    requires_expansion: bool = False,
    tags: list[str] | None = None,
    category_v2: str = "产品实践",
    source: str = "user-post",
) -> dict[str, Any]:
    """构造一条评测用例，自动派生 expect/forbidden/required_doc_ids。

    forbidden 默认 = 全部已发布产品中除 expected 之外的产品，
    但允许多产品命中的用例通过显式传参调整。
    """
    if forbidden is None:
        forbidden = [p for p in PUBLISHED if p not in expected]
    required = _doc(expected[0]) if requires_expansion and expected else []
    return {
        "case_id": case_id,
        "mode": "auto",
        "role": role,
        "tone": tone,
        "expected_product_ids": expected,
        "required_doc_ids": required,
        "forbidden_product_ids": forbidden,
        "requires_expansion": requires_expansion,
        "allowed_product_claims": [c for c in title.split("：")[-1].split("，") if c],
        "article": {
            "title": title,
            "summary_cn": summary_cn,
            "content_md": content_md,
            "category_v2": category_v2,
            "tags": tags or [],
            "source": source,
        },
    }


# ── 多角色真实输入样本（扮演不同用户身份撰写） ──────────────
# 每个样本尽量口语化、贴近真实发文，部分含含糊/多产品/竞品等压力场景。


def _build_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []

    # ── 角色1：安全研究员（权威、专业、偏技术） ────────────
    samples.append(
        _case(
            "u-001",
            "安全研究员",
            "专业",
            title="智能体权限过大：最小权限原则在 Agent 落地有多难",
            summary_cn="调研多智能体系统里权限边界模糊导致的越权，讨论最小权限、动态授权与风险缓解。",
            content_md="现在的 Agent 框架普遍默认给全量权限，凭证和密钥散落在各处。本文梳理了把最小权限、临时授权落实到智能体运行时的几种工程做法。",
            expected=["agent-identity-security"],
            requires_expansion=True,
            tags=["授权", "最小权限", "凭证"],
        )
    )
    # ── 角色2：厂商售前/技术布道（偏产品、偏实践） ──────────
    samples.append(
        _case(
            "u-002",
            "厂商技术布道",
            "产品向",
            title="智能体安全平台：从检测到防护的一体化思路",
            summary_cn="介绍智能体安全平台覆盖威胁检测、实时防护、行为分析与治理的产品组合。",
            content_md="我们提供面向智能体运行时的安全平台，覆盖提示词注入防护、异常行为检测、沙箱隔离与供应链安全，帮助企业在生产环境落地治理。",
            expected=["agent-security"],
            requires_expansion=True,
            tags=["智能体防护", "检测", "沙箱"],
        )
    )
    # ── 角色3：客户/使用者（口语、偏诉求、含糊） ────────────
    samples.append(
        _case(
            "u-003",
            "客户",
            "口语",
            title="谁能帮我管管我们 AI 项目里用到的那些组件？",
            summary_cn="公司上了很多 AI 模型和第三方组件，想知道怎么盘点、怎么管依赖和来源。",
            content_md="我们项目里用了好多开源模型和组件，领导让搞清楚这些资产到底有哪些、来路正不正、依赖关系怎么样，有没有现成的管理清单方案？",
            expected=["ai-bom"],
            tags=["AI资产", "组件", "供应链"],
        )
    )
    # ── 角色4：媒体/行业观察（宏观、偏趋势） ────────────────
    samples.append(
        _case(
            "u-004",
            "行业媒体",
            "宏观",
            title="智能体正在成为企业最大的攻击面，安全怎么跟上？",
            summary_cn="从行业视角讨论智能体身份与权限失控带来的安全风险与治理方向。",
            content_md="当智能体开始自主调用 API、访问系统，身份认证和权限管理就成了生死线。企业需要重新审视 Agent 的身份治理与运行时防护。",
            expected=["agent-identity-security"],
            forbidden=["agent-security", "ai-bom"],
            tags=["身份", "权限", "攻击面"],
        )
    )
    # ── 角色5：测评/选型（对比、竞品、多产品） ──────────────
    samples.append(
        _case(
            "u-005",
            "测评博主",
            "对比",
            title="智能体安全三件套横评：身份、运行时、物料清单",
            summary_cn="对比身份安全、运行时防护、AI 资产管理三类产品在智能体安全场景的表现。",
            content_md="要搞智能体安全，身份认证、运行时防护、资产清单三个维度都得抓。分别对比了市场上主流方案在这三方面的能力差异。",
            expected=["agent-identity-security", "agent-security", "ai-bom"],
            forbidden=[],
            tags=["对比", "选型"],
        )
    )
    # ── 角色6：安全运营（案例、痛点、需确认） ───────────────
    samples.append(
        _case(
            "u-006",
            "安全运营",
            "案例",
            title="一次提示词注入事件复盘：智能体被带偏了",
            summary_cn="复盘企业内部智能体遭提示词注入、数据泄露的应急与防护经验。",
            content_md="上周我们的客服 Agent 被恶意提示词带偏，输出里带了不该带的数据。复盘下来，运行时防护和输入校验必须要做，沙箱隔离也得上。",
            expected=["agent-security"],
            requires_expansion=True,
            tags=["提示词注入", "数据泄露", "应急"],
        )
    )
    # ── 角色7：无命中/模糊（无明显产品，测试不编造） ─────────
    samples.append(
        _case(
            "u-007",
            "行业杂谈",
            "模糊",
            title="聊聊 AI 落地一年来的酸甜苦辣",
            summary_cn="泛谈 AI 在大中型企业落地的组织、流程与心态变化，未聚焦具体安全产品。",
            content_md="这一年 AI 落地最大的感受是：不是技术不够，是组织没跟上。跨部门协作、数据治理、人才缺位，这些比算法本身更费劲。",
            expected=[],
            forbidden=[],
            tags=["落地", "组织"],
        )
    )
    # ── 角色8：竞品比较（涉及竞品词，测禁止产品隔离） ───────
    samples.append(
        _case(
            "u-008",
            "测评",
            "对比",
            title="相比传统 SSE，我们的 Agent 身份方案领先在哪",
            summary_cn="从身份安全角度对比传统安全服务边缘与传统 SSE，突出 Agent 身份治理优势。",
            content_md="传统 SSE 主要管网络流量，对智能体身份的精细化治理不够。我们的方案在 Agent 身份认证、最小权限和动态授权上更贴合智能体场景。",
            expected=["agent-identity-security"],
            forbidden=["ans", "agent-security"],
            tags=["对比", "SSE", "身份"],
        )
    )
    # ── 角色9：跨产品（身份+运行时，混合诉求） ──────────────
    samples.append(
        _case(
            "u-009",
            "企业架构师",
            "方案",
            title="智能体上生产前，安全团队该先解决哪几件事？",
            summary_cn="架构视角梳理智能体上生产的安全清单：身份、权限、运行时监控与资产登记。",
            content_md="上生产前建议先做：Agent 身份认证与最小权限、运行时行为监控与异常检测、以及把用到的模型组件登记成资产清单。三块可以分步推进。",
            expected=["agent-identity-security", "agent-security", "ai-bom"],
            forbidden=[],
            tags=["上生产", "安全清单"],
        )
    )
    # ── 角色10：纯运行时防护（无身份诉求，测关键词区分） ─────
    samples.append(
        _case(
            "u-010",
            "DevOps",
            "运维",
            title="给 Agent 加个沙箱，隔离一下模型调用",
            summary_cn="运维视角讨论给智能体模型调用加沙箱、做进程隔离与异常检测。",
            content_md="把 Agent 的模型调用丢进沙箱，做进程隔离和异常检测，能挡住不少恶意输入。运行时防护这块我们实践下来效果不错。",
            expected=["agent-security"],
            forbidden=["agent-identity-security", "ai-bom"],
            tags=["沙箱", "进程隔离", "异常检测"],
        )
    )
    return samples


def generate() -> list[dict[str, Any]]:
    """生成并写入 dataset.v2.jsonl。"""
    samples = _build_samples()
    OUTPUT.write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n",
        encoding="utf-8",
    )
    return samples


if __name__ == "__main__":
    samples = generate()
    print(f"已生成 {len(samples)} 条多角色真实输入评测用手写加到 {OUTPUT}")
    for s in samples:
        print(
            f"  [{s['case_id']}] ({s.get('role')}/{s.get('tone')}) -> {s['expected_product_ids']}"
        )
