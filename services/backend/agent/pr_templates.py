"""
PR 报道模板库 (V2)

按 V2 6分类中可进入 PR 流程的 3 个类别，每类设计 2 套模板，
DraftGenerator 对每篇文章使用 2 套模板 × 2 个角度 = 4 篇草稿。

模板结构:
  爆点事件:
    Template A — 事件速报型
    Template B — 深度解读型
  法律法规/监管动态:
    Template A — 政策摘要型
    Template B — 影响分析型
  AI技术重大进展:
    Template A — 技术速览型
    Template B — 产品对标型

使用:
    from agent.pr_templates import PR_TEMPLATES, match_templates
    templates = match_templates("爆点事件")
    # → [TemplateA, TemplateB]
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════
# 模板数据结构
# ═══════════════════════════════════════════════════════════════


class PRTemplate:
    """单套 PR 模板"""

    def __init__(
        self,
        name: str,
        category: str,
        title_template: str,
        sections: list[dict],
        perspectives: list[str],
        *,
        template_key: str = "",
        slot: str = "",
        system_version: int = 1,
        extra_instructions: str = "",
    ):
        """
        Args:
            name: 模板名（如 "爆点A"）
            category: 对应 V2 分类
            title_template: 标题模板（{event_name} 占位）
            sections: 章节列表 [{"heading": "...", "guide": "..."}, ...]
            perspectives: 可选写作角度列表
            template_key: 跨版本稳定模板键
            slot: 同一分类下的固定槽位（A/B）
            system_version: 系统模板结构版本
            extra_instructions: 模板级补充写作要求
        """
        self.name = name
        self.category = category
        self.title_template = title_template
        self.sections = sections
        self.perspectives = perspectives
        self.template_key = template_key
        self.slot = slot
        self.system_version = system_version
        self.extra_instructions = extra_instructions

    def build_system_prompt(self, perspective: str = "") -> str:
        """构建注入 LLM 的模板 System Prompt 片段"""
        lines = [
            f"## 报道模板: {self.name}",
            f"标题格式: {self.title_template}",
            f"写作角度: {perspective or self.perspectives[0] if self.perspectives else '标准'}",
            "",
            "请按以下章节结构撰写：",
        ]
        for sec in self.sections:
            lines.append(f"### {sec['heading']}")
            lines.append(f"[{sec['guide']}]")
            lines.append("")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# 爆点事件 — 模板
# ═══════════════════════════════════════════════════════════════

BREAKING_A = PRTemplate(
    template_key="breaking_a",
    slot="A",
    system_version=1,
    name="爆点A",
    category="爆点事件",
    title_template="# [事件名称]：影响分析与产品应对建议",
    sections=[
        {"heading": "事件概述", "guide": "2-3句概述事件基本信息：时间、涉及方、事件性质、影响范围"},
        {
            "heading": "技术分析",
            "guide": "事件的技术原理、攻击路径或漏洞机制，结合我们的产品能力分析",
        },
        {
            "heading": "产品关联",
            "guide": "我们的产品（智能体身份安全）如何应对/检测/防护此类事件，关联具体功能模块",
        },
        {
            "heading": "市场机会",
            "guide": "可作为销售话术、客户沟通素材的要点，结合控标点和客户案例",
        },
        {"heading": "行动建议", "guide": "产品侧/销售侧/市场侧的具体行动项，3-5条可执行建议"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "技术分析为主，侧重漏洞原理和产品防护能力",
        "市场影响为主，侧重客户沟通和销售机会",
    ],
)

BREAKING_B = PRTemplate(
    template_key="breaking_b",
    slot="B",
    system_version=1,
    name="爆点B",
    category="爆点事件",
    title_template="# 深度解读：[事件名称]背后的安全趋势",
    sections=[
        {"heading": "事件还原", "guide": "详细还原事件经过，包括时间线、涉及方、技术细节"},
        {"heading": "根因分析", "guide": "深入分析根本原因：是架构缺陷？配置问题？还是新攻击面？"},
        {"heading": "行业影响", "guide": "对行业格局、安全标准、监管方向的潜在影响"},
        {
            "heading": "我们的机会",
            "guide": "结合智能体身份安全产品的切入点，本次事件如何证明产品价值",
        },
        {"heading": "推荐策略", "guide": "短期（1周内）+ 中期（1月内）+ 长期策略建议"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "行业趋势分析，侧重对安全产业格局的影响",
        "产品战略分析，侧重对我们产品路线图的启示",
    ],
)

# ═══════════════════════════════════════════════════════════════
# 法律法规/监管动态 — 模板
# ═══════════════════════════════════════════════════════════════

LAW_A = PRTemplate(
    template_key="law_a",
    slot="A",
    system_version=1,
    name="法规A",
    category="法律法规/监管动态",
    title_template="# [法规名称]：政策要点与企业合规应对",
    sections=[
        {
            "heading": "政策概述",
            "guide": "2-3句概述法规/政策基本信息：发布机构、生效时间、核心要求",
        },
        {
            "heading": "关键条款解读",
            "guide": "选取3-5条与智能体/AI安全直接相关的条款，逐条简要解读",
        },
        {"heading": "合规影响分析", "guide": "该法规对企业客户和我们自身产品的合规影响"},
        {"heading": "产品机遇", "guide": "我们的产品如何帮助客户满足合规要求，可关联哪些功能模块"},
        {"heading": "行动建议", "guide": "产品侧合规改造 + 销售侧合规话术 + 市场侧白皮书/方案"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "合规解读为主，侧重条文分析和客户合规需求",
        "市场机会为主，侧重如何借政策推动产品销售",
    ],
)

LAW_B = PRTemplate(
    template_key="law_b",
    slot="B",
    system_version=1,
    name="法规B",
    category="法律法规/监管动态",
    title_template="# 政策影响评估：[法规名称]对智能体安全行业的影响",
    sections=[
        {"heading": "政策背景", "guide": "国内外政策环境、出台背景、与已有法规的关系"},
        {"heading": "核心变化", "guide": "与之前相比，本次法规的最大变化是什么？新要求有哪些？"},
        {"heading": "行业冲击", "guide": "对智能体安全行业、客户、友商的短期和长期影响"},
        {"heading": "我们的应对", "guide": "产品功能如何满足新规要求？竞品有无先发优势？"},
        {"heading": "时间线建议", "guide": "按时间线给出产品、销售、市场的阶段性任务"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "横向对比分析，侧重国内外类似法规的差异和趋势",
        "行业竞争分析，侧重友商应对策略和我们的差异化优势",
    ],
)

# ═══════════════════════════════════════════════════════════════
# AI技术重大进展 — 模板
# ═══════════════════════════════════════════════════════════════

AI_A = PRTemplate(
    template_key="ai_a",
    slot="A",
    system_version=1,
    name="AI技术A",
    category="AI技术重大进展",
    title_template="# 技术速览：[技术/产品名称]的核心突破与安全启示",
    sections=[
        {
            "heading": "技术概述",
            "guide": "2-3句概述该技术/产品的基本信息：发布方、核心能力、发布时间",
        },
        {"heading": "技术亮点", "guide": "3-5个核心技术突破点，重点关注与智能体安全相关的特性"},
        {"heading": "安全启示", "guide": "该技术对Agent安全、身份认证、权限管控等领域的影响和启发"},
        {"heading": "我们的关联", "guide": "结合产品功能矩阵，分析我们可以如何利用或应对该技术"},
        {"heading": "行动建议", "guide": "产品集成/技术跟进 + 市场传播 + 客户沟通建议"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "技术解读为主，侧重架构分析和安全影响",
        "产品对标为主，侧重我们产品的差异化优势",
    ],
)

AI_B = PRTemplate(
    template_key="ai_b",
    slot="B",
    system_version=1,
    name="AI技术B",
    category="AI技术重大进展",
    title_template="# 产品对标：[技术/产品名称]与智能体身份安全的机会分析",
    sections=[
        {"heading": "行业背景", "guide": "该技术/产品所处领域的行业现状和发展趋势"},
        {"heading": "技术拆解", "guide": "技术架构、核心组件、与现有方案的对比"},
        {"heading": "安全缺口", "guide": "该技术带来的新安全风险和现有安全方案的不足"},
        {"heading": "产品机会", "guide": "该技术为智能体身份安全产品带来的市场机会和产品方向"},
        {"heading": "竞品动态", "guide": "如有友商在该领域的布局，简要分析竞争态势"},
        {"heading": "战略建议", "guide": "我们的短期应对 + 中长期布局建议"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "市场分析为主，侧重商业机会和GTM策略",
        "技术对标为主，侧重产品功能差距和追赶路径",
    ],
)

# ═══════════════════════════════════════════════════════════════
# 模板索引
# ═══════════════════════════════════════════════════════════════

PR_TEMPLATES: dict[str, list[PRTemplate]] = {
    "爆点事件": [BREAKING_A, BREAKING_B],
    "法律法规/监管动态": [LAW_A, LAW_B],
    "AI技术重大进展": [AI_A, AI_B],
}

SYSTEM_TEMPLATES_BY_KEY: dict[str, PRTemplate] = {
    template.template_key: template for templates in PR_TEMPLATES.values() for template in templates
}


def match_templates(category_v2: str) -> list[PRTemplate]:
    """根据 V2 分类匹配 PR 模板。

    Args:
        category_v2: V2 6分类标签

    Returns:
        2 套模板列表。如果分类不匹配任何 PR 类别，返回空列表。
    """
    return PR_TEMPLATES.get(category_v2, [])


def get_all_template_names() -> dict[str, list[str]]:
    """返回所有模板名（按分类分组），用于调试和展示。"""
    return {cat: [t.name for t in tmpls] for cat, tmpls in PR_TEMPLATES.items()}


def get_system_template(template_key: str) -> PRTemplate | None:
    """按稳定模板键查询系统模板。"""
    return SYSTEM_TEMPLATES_BY_KEY.get(template_key)
