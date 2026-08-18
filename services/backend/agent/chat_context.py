"""ChatContext — 聊天式 Agent 的 token 感知上下文组装器。

在 ChatAgentService 调用 AgentEngine 之前，把三部分在一个 token 预算内组装成
可注入的 system prompt：
  - base：聊天 Agent 的基准 SYSTEM_PROMPT（排序第一，始终保留）
  - skill：按用户本次诉求命中的 Skill 指令（skill_core，次优保留）
  - memory：用户长期偏好记忆（memory_preference，优先级最低，预算不足时丢弃）

复用 context_manager 的 token 估算与模型窗口推导，保证同一套定价口径。

预算规则：
  - 输入预算 = 模型窗口 - 输出预留 - 历史预留（当 CHAT_AGENT_MAX_INPUT_TOKENS
    显式配置时，直接用该值作为输入预算）
  - base 与 skill 为"指令类"，默认全量保留；memory 为"偏好类"，溢出时整块丢弃并记录
  - 粒度到块（不逐 token 截断指令），避免注入半截规则造成模型误读
"""

from __future__ import annotations

import logging
from typing import Any

from agent.context_manager import estimate_tokens, resolve_model_window

logger = logging.getLogger("backend.agent.chat_context")

# 输出/历史预留（与 context_manager 默认一致，保证口径统一）
_RESERVED_OUTPUT = 4000
_RESERVED_HISTORY = 4000
# 最低可用输入预算下限：设置过小或模型窗口推导异常时兜底
_MIN_INPUT_BUDGET = 4096


def derive_input_budget(
    *,
    model_id: str = "deepseek-chat",
    max_input_tokens: int = 0,
    base_tokens: int = 0,
) -> int:
    """推导本轮可注入的输入 token 预算。

    max_input_tokens>0 时直接取用（配置显式上限），否则从模型窗口推导：
        窗口 - 输出预留 - 历史预留 - 基准提示词
    Returns:
        预算 token 数（不小于 0）
    """
    if max_input_tokens and max_input_tokens > 0:
        return max(0, max_input_tokens)
    window = resolve_model_window(model_id)
    budget = window - _RESERVED_OUTPUT - _RESERVED_HISTORY - base_tokens
    return max(0, budget)


def _estimate_messages(messages: list[dict[str, Any]]) -> int:
    """估算消息列表占用 token（与 llm 输出口径统一的字符/4 估算）。"""
    return estimate_tokens(_messages_text(messages))


def _messages_text(messages: list[dict[str, Any]]) -> str:
    parts = []
    for m in messages or []:
        role = str(m.get("role", ""))
        content = str(m.get("content", ""))
        parts.append(f"{role}: {content}\n")
    return "\n".join(parts)


class ChatContext:
    """一次组装的结果：可注入的 system prompt + 各项度量。"""

    __slots__ = (
        "system_prompt",
        "input_budget",
        "used_tokens",
        "base_tokens",
        "skill_tokens",
        "memory_tokens",
        "memory_dropped",
        "skill_names",
        "model_window",
    )

    def __init__(
        self,
        *,
        system_prompt: str,
        input_budget: int,
        used_tokens: int,
        base_tokens: int,
        skill_tokens: int,
        memory_tokens: int,
        memory_dropped: int,
        skill_names: list[str],
        model_window: int,
    ) -> None:
        self.system_prompt = system_prompt
        self.input_budget = input_budget
        self.used_tokens = used_tokens
        self.base_tokens = base_tokens
        self.skill_tokens = skill_tokens
        self.memory_tokens = memory_tokens
        self.memory_dropped = memory_dropped
        self.skill_names = skill_names
        self.model_window = model_window

    def telemetry(self) -> dict[str, Any]:
        """转成 LLM 日志 telemetry（不含知识全文）。"""
        return {
            "input_budget": self.input_budget,
            "used_tokens": self.used_tokens,
            "base_tokens": self.base_tokens,
            "skill_tokens": self.skill_tokens,
            "memory_tokens": self.memory_tokens,
            "memory_dropped": self.memory_dropped,
            "skill_names": self.skill_names,
            "model_window": self.model_window,
        }


def build_chat_context(
    *,
    base_system_prompt: str,
    model_id: str = "deepseek-chat",
    max_input_tokens: int = 0,
    skill_instructions: list[tuple[str, str]] | None = None,
    memory_text: str = "",
    strip_memory: bool = True,
) -> ChatContext:
    """在 token 预算内组装 system prompt。

    Args:
        base_system_prompt: 基准提示词（始终保留）
        model_id: 模型名（用于推导窗口）
        max_input_tokens: 显式输入上限；0=按窗口推导
        skill_instructions: [(skill_name, instruction_text), ...] 命中 Skill 指令
        memory_text: 用户偏好记忆渲染文本（可空）
        strip_memory: 记忆块去空白后参与预算（默认 True）

    Returns:
        ChatContext
    """
    model_window = resolve_model_window(model_id)
    base = (base_system_prompt or "").strip()
    base_tokens = estimate_tokens(base)

    budget = derive_input_budget(
        model_id=model_id,
        max_input_tokens=max_input_tokens,
        base_tokens=base_tokens,
    )
    # 显式上限时也需覆盖最低预算兜底
    effective_budget = max(budget, _MIN_INPUT_BUDGET)

    # 指令类（base + skill）默认全量保留：它们定义能力边界，绝不能被整块丢弃
    used = base_tokens
    skill_tokens = 0
    skill_names: list[str] = []
    skill_blocks: list[str] = []

    for name, text in skill_instructions or []:
        block = (text or "").strip()
        if not block:
            continue
        tokens = estimate_tokens(block)
        skill_tokens += tokens
        skill_blocks.append(block)
        skill_names.append(name)
        used += tokens

    # 记忆块：优先级最低，跨出输入预算时整块丢弃（不影响指令完整性）
    memory_clean = (memory_text or "").strip() if strip_memory else (memory_text or "")
    memory_tokens = estimate_tokens(memory_clean) if memory_clean else 0
    memory_dropped = 0
    if memory_clean and used + memory_tokens > effective_budget:
        memory_dropped = memory_tokens
        memory_tokens = 0
        logger.info(
            "chat_context: memory dropped (budget=%d used=%d need=%d)",
            effective_budget, used, memory_tokens,
        )
    else:
        used += memory_tokens

    sections: list[str] = [base]
    for block in skill_blocks:
        sections.append(f"# Skill 指令\n\n{block}")
    if memory_clean and memory_tokens > 0:
        sections.append(f"# 用户长期偏好（仅作表达参考）\n\n{memory_clean}")

    return ChatContext(
        system_prompt="\n\n".join(s for s in sections if s),
        input_budget=effective_budget,
        used_tokens=used,
        base_tokens=base_tokens,
        skill_tokens=skill_tokens,
        memory_tokens=memory_tokens,
        memory_dropped=memory_dropped,
        skill_names=skill_names,
        model_window=model_window,
    )