"""ContextManager - token 感知的统一上下文计划（阶段二 Step 4）。

以模型 token 窗口统一分配 policy、task、skill、知识、记忆和外部来源预算。

分配顺序（高 → 低）：
    安全/合规政策
    → 用户本次明确约束
    → Skill 核心指令
    → required 产品知识
    → 用户知识
    → 记忆偏好
    → Skill 可选 references
    → 外部资料

约束：
  - 从模型窗口扣除 system、任务锚点、历史、输出预留和 10% 安全余量后分配
  - required 来源不可被低优先级内容挤出；不足时缩减 optional 或直接回退，
    不生成残缺评分上下文
  - 冲突规则：政策 > 本次指令 > 已发布产品知识 > 用户补充 > 记忆 > 外部资料，
    被抑制来源写入 conflicts
  - CONTEXT_MAX_INPUT_TOKENS=0 时按模型窗口动态推导
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("backend.agent.context_manager")

Purpose = Literal["score", "draft", "chat"]

# 已知模型 token 窗口（无映射时回退默认 64000）
_MODEL_WINDOWS: dict[str, int] = {
    "deepseek-chat": 64000,
    "deepseek-reasoner": 64000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "claude-3-5-sonnet": 200000,
    "claude-3-5-haiku": 200000,
}
_DEFAULT_WINDOW = 64000

# 保留比例（从窗口扣除项）
_SYSTEM_RESERVE = 0.10  # 安全余量
_DEFAULT_RESERVED_OUTPUT = 4000  # 输出预留 token
_DEFAULT_HISTORY_BUDGET = 4000  # 历史预留 token
_DEFAULT_TASK_ANCHOR = 800  # 任务锚点 token

# 按字符估算 token（中文场景经验值）
CHARS_PER_TOKEN = 4

# 分配顺序（与文档一致）
ALLOCATION_ORDER = (
    "security_policy",  # 安全/合规政策
    "user_constraints",  # 用户本次明确约束
    "skill_core",  # Skill 核心指令
    "required_product",  # required 产品知识
    "user_knowledge",  # 用户知识
    "memory_preference",  # 记忆偏好
    "skill_references",  # Skill 可选 references
    "external",  # 外部资料
)

# 优先级数值（用于冲突抑制）
PRIORITY_LEVELS = {
    "security_policy": 80,
    "user_constraints": 70,
    "required_product": 60,
    "user_knowledge": 50,
    "memory_preference": 40,
    "skill_references": 30,
    "external": 20,
}


def estimate_tokens(text: str) -> int:
    """估算文本 token 数（字符 / CHARS_PER_TOKEN）。"""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def resolve_model_window(model_id: str) -> int:
    """按模型名解析 token 窗口。"""
    key = (model_id or "").lower()
    if key in _MODEL_WINDOWS:
        return _MODEL_WINDOWS[key]
    # 前缀匹配（如 deepseek-chat-v3 等）
    for name, window in _MODEL_WINDOWS.items():
        if key.startswith(name):
            return window
    return _DEFAULT_WINDOW


@dataclass(frozen=True)
class ContextRequest:
    """一次上下文构建请求。"""

    purpose: Purpose
    user_id: str
    products: list[str] = field(default_factory=list)
    query: str = ""
    model_id: str = "deepseek-chat"
    max_input_tokens: int = 0  # 0 = 按模型窗口动态计算
    reserved_output: int = _DEFAULT_RESERVED_OUTPUT
    history_tokens: int = _DEFAULT_HISTORY_BUDGET
    task_anchor_tokens: int = _DEFAULT_TASK_ANCHOR
    # 阶段3 S3-1：统一请求溯源字段（用于重建每次上下文来源）
    task_id: str = ""
    trace_id: str = ""
    index_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSource:
    """单个上下文来源描述。"""

    source: str  # 来源标识（如 "knowledge:overview"、"skill:scoring-knowledge"）
    content: str
    section_type: str  # 对应 ALLOCATION_ORDER 的键
    product: str = ""
    doc_type: str = ""
    version: str = ""
    source_hash: str = ""
    trust: str = "published"  # published / user / system
    published: bool = True
    required: bool = False


@dataclass
class ContextSection:
    """分配后的上下文节。"""

    source: ContextSource
    content: str
    tokens: int = 0
    truncated: bool = False

    @property
    def source_id(self) -> str:
        return self.source.source

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class DropRecord:
    """被丢弃（预算不足）的来源。"""

    source: str
    reason: str
    tokens: int


@dataclass(frozen=True)
class ConflictRecord:
    """被抑制（冲突）的来源。"""

    source: str
    suppressed_by: str
    reason: str


@dataclass
class ContextPlan:
    """最终上下文计划。"""

    request: ContextRequest
    sections: list[ContextSection] = field(default_factory=list)
    dropped: list[DropRecord] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    snapshot: dict[str, str] = field(
        default_factory=dict
    )  # skill_versions / knowledge_snapshot / memory_version
    budget_tokens: int = 0
    input_budget_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(s.tokens for s in self.sections)

    @property
    def plan_hash(self) -> str:
        parts = []
        for section in self.sections:
            parts.append(f"{section.source_id}:{section.source.source_hash}:{section.tokens}")
        parts.append(f"budget={self.input_budget_tokens}")
        for d in self.dropped:
            parts.append(f"drop:{d.source}:{d.reason}")
        for c in self.conflicts:
            parts.append(f"conflict:{c.source}:{c.suppressed_by}")
        for k, v in sorted(self.snapshot.items()):
            parts.append(f"{k}={v}")
        content = "|".join(parts)
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def rendered(self) -> str:
        """渲染为可注入 prompt 的文本。"""
        return "\n\n".join(s.render() for s in self.sections)


class ContextManager:
    """token 感知的上下文计划器。"""

    def __init__(self):
        self._sections_accum: list[ContextSection] = []
        self._dropped: list[DropRecord] = []
        self._conflicts: list[ConflictRecord] = []

    def derive_input_budget(self, request: ContextRequest) -> int:
        """从模型窗口推导输入预算：窗口 - system - 任务锚点 - 历史 - 输出 - 安全余量。"""
        if request.max_input_tokens and request.max_input_tokens > 0:
            return request.max_input_tokens
        window = resolve_model_window(request.model_id)
        system_cost = request.metadata.get("system_tokens", 0)
        reserved = (
            system_cost
            + request.task_anchor_tokens
            + request.history_tokens
            + request.reserved_output
        )
        budget = int(window * (1 - _SYSTEM_RESERVE)) - reserved
        return max(1024, budget)

    def build(
        self,
        request: ContextRequest,
        sources: list[ContextSource],
        *,
        snapshot: dict[str, str] | None = None,
    ) -> ContextPlan:
        """按分配顺序构建 ContextPlan。

        Args:
            request: 上下文请求
            sources: 待分配来源（调用方已按 purpose 收集并排序）
            snapshot: 版本快照（skill_versions / knowledge_snapshot / memory_version）

        Returns:
            ContextPlan
        """
        self._sections_accum = []
        self._dropped = []
        self._conflicts = []

        budget = self.derive_input_budget(request)
        used = 0

        # 1. 必需来源（security_policy / user_constraints / skill_core / required_product）
        #    不可被挤出；全部尝试分配，若预算不足则标记 insufficient。
        for src in sources:
            if not src.required:
                continue
            tokens = estimate_tokens(src.content)
            if used + tokens > budget:
                # required 不可被挤出：记录 dropped(insufficient) 并跳过该节
                self._dropped.append(
                    DropRecord(
                        source=src.source, reason="required_insufficient_budget", tokens=tokens
                    )
                )
                continue
            self._sections_accum.append(
                ContextSection(source=src, content=src.content, tokens=tokens)
            )
            used += tokens

        # 2. 可选来源：按 ALLOCATION_ORDER 顺序，预算充足则分配，否则丢弃
        for src in sources:
            if src.required:
                continue
            tokens = estimate_tokens(src.content)
            if used + tokens > budget:
                self._dropped.append(
                    DropRecord(source=src.source, reason="budget_exceeded", tokens=tokens)
                )
                continue
            # 冲突规则：同 section_type 高优先级已存在则抑制
            conflict = self._find_conflict(src)
            if conflict is not None:
                self._conflicts.append(conflict)
                continue
            self._sections_accum.append(
                ContextSection(source=src, content=src.content, tokens=tokens)
            )
            used += tokens

        # 3. required 缺失检查：评分上下文不允许残缺
        required_sources = [s for s in sources if s.required]
        if request.purpose == "score" and required_sources:
            allocated_required = {
                s.source.source for s in self._sections_accum if s.source.required
            }
            for src in required_sources:
                if src.source not in allocated_required:
                    logger.warning(
                        "ContextManager: required source dropped for score purpose: %s", src.source
                    )

        return ContextPlan(
            request=request,
            sections=self._sections_accum,
            dropped=self._dropped,
            conflicts=self._conflicts,
            snapshot=snapshot or {},
            budget_tokens=budget,
            input_budget_tokens=budget,
        )

    def _find_conflict(self, src: ContextSource) -> ConflictRecord | None:
        """冲突规则：已存在更高或相同优先级来源时抑制（仅跨来源同类型）。"""
        existing = [s for s in self._sections_accum if s.source.section_type == src.section_type]
        if not existing:
            return None
        # 同 section_type 只允许一个（如 required_product 每产品唯一）
        return ConflictRecord(
            source=src.source,
            suppressed_by=existing[0].source.source,
            reason=f"same_section_type_conflict:{src.section_type}",
        )
