"""Prompt Registry - 可编辑提示词定义的系统注册表。

注册表是系统默认和编辑边界的唯一来源，API、前端元数据和 Worker 共用。

提示词按三层管理：
- 固定策略层（fixed_policy）：事实准确、安全红线、输出协议等，不可被用户覆盖
- 用户业务层（default_content）：判断口径、写作偏好、改稿原则，可编辑可版本化
- 运行时数据层：原文、产品知识、模板等，由系统注入

本文件仅维护用户业务层的默认内容和编辑边界。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


class PromptDefinition(NamedTuple):
    """单条提示词定义。"""

    prompt_key: str
    display_name: str
    description: str
    stage: str
    default_content: str
    default_version: int
    editable: bool
    required_placeholders: tuple[str, ...]
    allowed_placeholders: tuple[str, ...]
    min_length: int
    max_length: int


# ── 固定策略层（不可编辑，由各 Agent 代码内持有）──────────
# 固定策略层不在注册表中重复维护，各 Agent 的 system_prompt 构造方法
# 始终负责拼接固定策略层 + 用户业务层 + 运行时数据层。

# ── 用户业务层默认内容 ──────────────────────────────────

_CLASSIFY_V2_BUSINESS = """\
## 分类判断补充说明

- 重点关注 AI/Agent 安全相关的突发漏洞、监管政策和重大技术里程碑
- 竞品动态中优先关注与亚信安全产品线有直接竞争关系的厂商
- 行业事件重点关注电信运营商、金融和能源领域的 AI 安全建设
- 学术内容仅收录有明确工程落地价值的顶会论文
"""

_SCORE_V2_BUSINESS = """\
## 评分补充说明

- 产品能力相关度重点关注智能体身份安全、AI-BOM 和智能体安全平台
- 事件影响面优先关注可能引发监管关注或客户主动问询的事件
- 竞品相关事件的产品相关度可适当上调，便于跟踪对标
- 法律法规类事件的事件影响面应考虑合规驱动带来的市场机会
"""

_DRAFT_GENERATION_BUSINESS = """\
## 初稿生成补充说明

- 标题需有冲击力但避免标题党，控制在 20 字以内
- 开篇用新闻事件切入安全痛点，再自然引出产品能力
- 产品关联段落不超过全文 30%，避免过度营销
- 结尾用行业趋势或展望收束，不用行动号召
- 语言风格：专业、有洞察、像行业分析而非产品说明书
"""

_CHAT_ANSWER_BUSINESS = """\
## 问答补充说明

- 回答时优先引用文章中的数据和事实
- 涉及产品能力时参考产品知识库定位
- 传播策略建议要具体可执行，不要空泛建议
- 对标题、结构的评价要有明确判断和改进方向
"""

_CHAT_REVISE_BUSINESS = """\
## 全文改稿补充说明

- 改稿保持原文核心事实不变，调整表达和结构
- 标题修改需更有冲击力但不得偏离事实
- 产品关联段落精简自然，避免生硬植入
- 章节间过渡要流畅，逻辑清晰
- 控制全文篇幅在 800-1200 字
"""

_CHAT_SECTION_REVISE_BUSINESS = """\
## 段落改稿补充说明

- 仅改写选中段落，保持与上下文的衔接
- 不改变段落的核心信息，优化表达和逻辑
- 保持与全文一致的语气和风格
"""

_REVIEW_FOCUS_BUSINESS = """\
## 审核额外关注项

- 检查是否出现内部沟通话术（如"销售话术""控标点""GTM策略"）
- 检查是否有未经证据支持的竞品优劣比较
- 检查产品能力描述是否超出知识库范围
- 检查数据引用是否标注来源
"""

# ── 注册表 ──────────────────────────────────────────────

_DEFINITIONS: list[PromptDefinition] = [
    PromptDefinition(
        prompt_key="classify_v2_business",
        display_name="文章分类判断",
        description="控制分类时的关注重点和判断口径，不影响分类输出格式",
        stage="classify",
        default_content=_CLASSIFY_V2_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=(),
        allowed_placeholders=("article_context",),
        min_length=10,
        max_length=5000,
    ),
    PromptDefinition(
        prompt_key="score_v2_business",
        display_name="产品相关性与事件影响评分",
        description="控制评分时的关注重点和判断口径，不影响评分输出格式",
        stage="score",
        default_content=_SCORE_V2_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=(),
        allowed_placeholders=("article_context", "product_context", "score_mode"),
        min_length=10,
        max_length=5000,
    ),
    PromptDefinition(
        prompt_key="draft_generation_business",
        display_name="初稿生成",
        description="控制初稿生成的写作风格、结构和产品关联方式",
        stage="draft",
        default_content=_DRAFT_GENERATION_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=("knowledge_context", "template_spec", "style_hints"),
        allowed_placeholders=(
            "knowledge_context",
            "template_spec",
            "style_hints",
            "article_context",
            "product_context",
        ),
        min_length=50,
        max_length=20000,
    ),
    PromptDefinition(
        prompt_key="chat_answer_business",
        display_name="稿件问答",
        description="控制问答时的回答风格和产品知识引用方式",
        stage="chat",
        default_content=_CHAT_ANSWER_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=(),
        allowed_placeholders=("article_context", "draft_context", "product_context"),
        min_length=10,
        max_length=5000,
    ),
    PromptDefinition(
        prompt_key="chat_revise_business",
        display_name="全文改稿",
        description="控制全文改稿的修改原则和篇幅控制",
        stage="chat",
        default_content=_CHAT_REVISE_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=(),
        allowed_placeholders=(
            "article_context",
            "draft_context",
            "product_context",
            "style_hints",
        ),
        min_length=10,
        max_length=5000,
    ),
    PromptDefinition(
        prompt_key="chat_section_revise_business",
        display_name="段落改稿",
        description="控制段落改稿的修改原则和衔接要求",
        stage="chat",
        default_content=_CHAT_SECTION_REVISE_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=(),
        allowed_placeholders=("selected_section", "article_context", "product_context"),
        min_length=10,
        max_length=5000,
    ),
    PromptDefinition(
        prompt_key="review_focus_business",
        display_name="审核关注项",
        description="追加审核关注项，固定审核红线不可被覆盖",
        stage="review",
        default_content=_REVIEW_FOCUS_BUSINESS,
        default_version=1,
        editable=True,
        required_placeholders=(),
        allowed_placeholders=("article_context", "draft_context"),
        min_length=10,
        max_length=5000,
    ),
]


# ── 兼容映射 ─────────────────────────────────────────────

_COMPAT_KEY_MAP: dict[str, str] = {
    "draft_system": "draft_generation_business",
}


def resolve_prompt_key(raw_key: str) -> str:
    """将旧 prompt_key 映射到新 key，保持向后兼容。

    旧 `draft_system` 映射到 `draft_generation_business`。
    新 key 原样返回。
    """
    return _COMPAT_KEY_MAP.get(raw_key, raw_key)


# ── 注册表访问接口 ───────────────────────────────────────


@dataclass(frozen=True)
class PromptRegistry:
    """提示词注册表，提供查询接口。"""

    _definitions: dict[str, PromptDefinition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self._definitions:
            object.__setattr__(
                self,
                "_definitions",
                {d.prompt_key: d for d in _DEFINITIONS},
            )

    def get(self, prompt_key: str) -> PromptDefinition | None:
        """获取提示词定义，自动处理兼容映射。"""
        resolved = resolve_prompt_key(prompt_key)
        return self._definitions.get(resolved)

    def require(self, prompt_key: str) -> PromptDefinition:
        """获取提示词定义，不存在则抛出 KeyError。"""
        resolved = resolve_prompt_key(prompt_key)
        definition = self._definitions.get(resolved)
        if definition is None:
            raise KeyError(f"Unsupported prompt key: {prompt_key}")
        return definition

    def list_all(self) -> list[PromptDefinition]:
        """列出全部可编辑提示词定义。"""
        return list(self._definitions.values())

    def is_registered(self, prompt_key: str) -> bool:
        """检查 prompt_key 是否已注册。"""
        resolved = resolve_prompt_key(prompt_key)
        return resolved in self._definitions


# 全局单例
_registry = PromptRegistry()


def get_registry() -> PromptRegistry:
    """返回全局 PromptRegistry 实例。"""
    return _registry
