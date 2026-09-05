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
    system_version=2,
    name="爆点A",
    category="爆点事件",
    title_template="# [事件名称]：发生了什么？安全防护怎么做？",
    sections=[
        {
            "heading": "事件概述",
            "guide": "2-3句概述事件基本信息：时间、涉及方、事件性质、影响范围，让读者快速了解事件全貌",
        },
        {
            "heading": "技术解读",
            "guide": "用通俗易懂的方式解读事件的技术原理、攻击路径或漏洞机制，避免过度使用专业术语",
        },
        {
            "heading": "影响分析",
            "guide": "分析事件对企业、用户和行业的影响，说明为什么读者应该关注这件事",
        },
        {
            "heading": "防护方案",
            "guide": "结合亚信安全的产品能力，介绍针对此类威胁的防护思路和方案，自然引出产品价值",
        },
        {"heading": "安全建议", "guide": "给读者提供3-5条可落地的安全防护建议"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "以技术科普为主，用通俗语言解读事件原理和防护思路",
        "以行业影响为主，分析事件对企业和用户的安全启示",
    ],
)

BREAKING_B = PRTemplate(
    template_key="breaking_b",
    slot="B",
    system_version=2,
    name="爆点B",
    category="爆点事件",
    title_template="# 深度解读：[事件名称]背后的安全趋势",
    sections=[
        {
            "heading": "事件还原",
            "guide": "详细还原事件经过，包括时间线、涉及方、技术细节，像讲故事一样引人入胜",
        },
        {
            "heading": "根因分析",
            "guide": "深入分析根本原因：是架构缺陷？配置问题？还是新攻击面？让读者理解问题本质",
        },
        {"heading": "行业趋势", "guide": "分析事件反映出的安全趋势，对行业格局和安全标准的影响"},
        {
            "heading": "方案解读",
            "guide": "结合亚信安全的产品方案，介绍如何应对此类安全挑战，让读者理解技术方案的value",
        },
        {"heading": "延伸思考", "guide": "从事件中提炼对读者安全建设有价值的思考和启发"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "以趋势分析为主，解读事件对安全产业格局的影响",
        "以方案解读为主，介绍亚信安全产品如何应对此类安全挑战",
    ],
)

# ═══════════════════════════════════════════════════════════════
# 法律法规/监管动态 — 模板
# ═══════════════════════════════════════════════════════════════

LAW_A = PRTemplate(
    template_key="law_a",
    slot="A",
    system_version=2,
    name="法规A",
    category="法律法规/监管动态",
    title_template="# [法规名称]：政策要点与合规解读",
    sections=[
        {
            "heading": "政策概述",
            "guide": "2-3句概述法规/政策基本信息：发布机构、生效时间、核心要求，让读者快速理解政策背景",
        },
        {
            "heading": "关键条款解读",
            "guide": "选取3-5条与智能体/AI安全直接相关的条款，逐条用通俗语言简要解读",
        },
        {"heading": "合规影响", "guide": "分析该法规对企业的影响，说明读者需要关注的合规要点"},
        {
            "heading": "应对方案",
            "guide": "结合亚信安全的产品能力，介绍如何帮助企业满足合规要求，自然引出产品价值",
        },
        {"heading": "实践建议", "guide": "给读者提供3-5条合规实践建议，帮助其落地合规要求"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "以政策解读为主，侧重条文分析和合规要点",
        "以实践指导为主，侧重合规落地方案和读者启示",
    ],
)

LAW_B = PRTemplate(
    template_key="law_b",
    slot="B",
    system_version=2,
    name="法规B",
    category="法律法规/监管动态",
    title_template="# 政策影响评估：[法规名称]对安全行业的影响",
    sections=[
        {"heading": "政策背景", "guide": "介绍政策出台的背景和国内外环境，让读者理解政策脉络"},
        {"heading": "核心变化", "guide": "与之前相比，本次法规的最大变化和新要求是什么"},
        {"heading": "行业影响", "guide": "对安全行业、企业用户的影响分析，帮助读者理解趋势"},
        {
            "heading": "技术应对",
            "guide": "结合亚信安全的产品技术，介绍如何响应新规要求，展示技术方案的价值",
        },
        {"heading": "趋势展望", "guide": "对未来合规趋势的展望，帮助读者提前布局"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "以趋势分析为主，侧重政策走向和行业趋势",
        "以技术解读为主，侧重合规要求与技术方案的结合",
    ],
)

# ═══════════════════════════════════════════════════════════════
# AI技术重大进展 — 模板
# ═══════════════════════════════════════════════════════════════

AI_A = PRTemplate(
    template_key="ai_a",
    slot="A",
    system_version=2,
    name="AI技术A",
    category="AI技术重大进展",
    title_template="# 技术速览：[技术/产品名称]的核心突破与安全启示",
    sections=[
        {
            "heading": "技术概述",
            "guide": "2-3句概述该技术/产品的基本信息：发布方、核心能力、发布时间，让读者快速了解",
        },
        {
            "heading": "核心亮点",
            "guide": "3-5个核心技术突破点，重点关注与智能体安全相关的特性，用通俗语言解释",
        },
        {"heading": "安全启示", "guide": "该技术对Agent安全、身份认证、权限管控等领域的影响和启发"},
        {
            "heading": "产品关联",
            "guide": "结合亚信安全的产品能力，分析我们可以如何利用或应对该技术，自然引出产品价值",
        },
        {"heading": "未来展望", "guide": "该技术未来发展对安全领域的影响展望，帮助读者提前思考"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "以技术解读为主，侧重原理分析和安全影响",
        "以应用视角为主，侧重技术落地和安全实践",
    ],
)

AI_B = PRTemplate(
    template_key="ai_b",
    slot="B",
    system_version=2,
    name="AI技术B",
    category="AI技术重大进展",
    title_template="# 产品对标：[技术/产品名称]与智能体身份安全的机会分析",
    sections=[
        {
            "heading": "行业背景",
            "guide": "该技术/产品所处领域的行业现状和发展趋势，让读者理解大环境",
        },
        {
            "heading": "技术拆解",
            "guide": "技术架构、核心组件、与现有方案的对比，用读者能理解的方式解释",
        },
        {
            "heading": "安全挑战",
            "guide": "该技术带来的新安全风险和现有安全方案的不足，帮助读者认识风险",
        },
        {
            "heading": "我们的方案",
            "guide": "结合亚信安全的产品方案，介绍如何应对这些新安全挑战，展示方案价值",
        },
        {"heading": "技术展望", "guide": "该技术领域的发展方向和对安全行业的长期影响"},
        {"heading": "关键词", "guide": "3-5个关键词标签"},
    ],
    perspectives=[
        "以技术分析为主，侧重安全风险和防护方案",
        "以行业视角为主，侧重技术趋势和安全启示",
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
