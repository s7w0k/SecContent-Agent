"""A2A ↔ 内部 RuntimeState 映射与不可信输入净化 — 阶段四 4B Step 4B-1。

映射关系（ADR-001）：
  a2a_task_id <-> internal_run_id
  context_id   <-> thread_id
  message_id   <-> runtime event_id（审计追溯）

安全约束：
  - 外部 Message/Artifact/URL/文件一律视为不可信输入：validate_external_input
    执行内容类型、大小、来源与恶意指令检查；
  - 对外 Task/Artifact 只含脱敏信息（不含参数原文、提示词、密钥与私有推理）；
  - 未知 TaskStatus 不静默映射为成功。
"""

from __future__ import annotations

import re
from typing import Any

from agent.a2a.models import (
    Artifact,
    InvalidInputError,
    Message,
    Part,
    ProtocolError,
    Task,
    TaskStatus,
    TERMINAL_TASK_STATUSES,
)
from agent.runtime_state import RuntimeState, RuntimeStatus

# ── 常量：输入净化阈值 ──────────────────────────────────────
DEFAULT_MAX_MESSAGE_BYTES = 1_048_576  # 1 MiB
DEFAULT_MAX_ARTIFACT_BYTES = 5_242_880  # 5 MiB
DEFAULT_MAX_PARTS_PER_MESSAGE = 64
DEFAULT_MAX_MESSAGES_PER_TASK = 256
MAX_TEXT_PART_CHARS = 200_000

# 恶意/注入模式：脚本、协议重定向、凭证关键字
_MALICIOUS_TEXT_PATTERNS = re.compile(
    r"(<\s*script[\s>]|javascript\s*:|data\s*:\s*text/html|onerror\s*=|"
    r"SELECT\s+.*\s+FROM\s+|BEGIN\s+.*;.*COMMIT)",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERNS = re.compile(
    r"(api[_-]?key|secret|password|bearer\s+[a-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)

# ── 状态映射（完整覆盖 4B-1 要求的八种 TaskStatus）─────────────
RUNTIME_TO_TASK: dict[RuntimeStatus, TaskStatus] = {
    RuntimeStatus.PENDING: TaskStatus.SUBMITTED,
    RuntimeStatus.PLANNING: TaskStatus.WORKING,
    RuntimeStatus.RUNNING: TaskStatus.WORKING,
    RuntimeStatus.WAITING_APPROVAL: TaskStatus.INPUT_REQUIRED,
    RuntimeStatus.CANCEL_REQUESTED: TaskStatus.WORKING,
    RuntimeStatus.COMPLETED: TaskStatus.COMPLETED,
    RuntimeStatus.FAILED: TaskStatus.FAILED,
    RuntimeStatus.CANCELED: TaskStatus.CANCELED,
    RuntimeStatus.BUDGET_EXCEEDED: TaskStatus.FAILED,
    RuntimeStatus.STOPPED: TaskStatus.FAILED,
}

TASK_TO_RUNTIME: dict[TaskStatus, RuntimeStatus] = {
    TaskStatus.SUBMITTED: RuntimeStatus.PENDING,
    TaskStatus.WORKING: RuntimeStatus.RUNNING,
    TaskStatus.INPUT_REQUIRED: RuntimeStatus.WAITING_APPROVAL,
    TaskStatus.AUTH_REQUIRED: RuntimeStatus.WAITING_APPROVAL,
    TaskStatus.COMPLETED: RuntimeStatus.COMPLETED,
    TaskStatus.FAILED: RuntimeStatus.FAILED,
    TaskStatus.CANCELED: RuntimeStatus.CANCELED,
    TaskStatus.REJECTED: RuntimeStatus.STOPPED,  # 拒绝映射策略熔断终态
}


def _as_runtime_status(status: RuntimeStatus | str) -> RuntimeStatus:
    """字符串 → RuntimeStatus；未知值抛 ProtocolError（协议边界不泄露堆栈）。"""
    if isinstance(status, RuntimeStatus):
        return status
    try:
        return RuntimeStatus(status)
    except ValueError:
        raise ProtocolError(f"unknown runtime status: {status!r}") from None


def _as_task_status(status: TaskStatus | str) -> TaskStatus:
    """字符串 → TaskStatus；未知值抛 ProtocolError。"""
    if isinstance(status, TaskStatus):
        return status
    try:
        return TaskStatus(status)
    except ValueError:
        raise ProtocolError(f"unknown task status: {status!r}") from None


def map_runtime_to_task(status: RuntimeStatus | str) -> TaskStatus:
    """内部 RuntimeStatus → A2A TaskStatus（未知状态抛协议错误）。"""
    status = _as_runtime_status(status)
    try:
        return RUNTIME_TO_TASK[status]
    except KeyError:
        raise ProtocolError(f"unmappable runtime status: {status.value}") from None


def map_task_to_runtime(status: TaskStatus | str) -> RuntimeStatus:
    """A2A TaskStatus → 内部 RuntimeStatus（恢复时使用；未知状态拒绝）。"""
    status = _as_task_status(status)
    try:
        return TASK_TO_RUNTIME[status]
    except KeyError:
        raise ProtocolError(f"unmappable task status: {status.value}") from None


# ── Task ↔ RuntimeState 映射 ────────────────────────────────


def map_state_to_task(state: RuntimeState, *, task_id: str) -> Task:
    """将内部 RuntimeState 映射为对外 Task（只含脱敏信息）。

    artifacts 来自运行证据（tool_result 摘要），history 来自决策摘要，
    均不含参数原文、提示词或私有推理链。
    """
    artifacts = [_map_evidence_to_artifact(ev) for ev in state.evidence]
    artifacts = [a for a in artifacts if a is not None]
    history = [
        Message(
            message_id=f"dec-{d.step_id}",
            task_id=task_id,
            role="agent",
            parts=[
                Part(
                    kind="text",
                    text=(
                        f"[{d.phase}] {d.action}"
                        f"{(' → ' + d.reason) if d.reason else ''}"
                    )[:MAX_TEXT_PART_CHARS],
                )
            ],
            metadata={"outcome": d.outcome},
        )
        for d in state.decision_summaries[-50:]
    ]
    return Task(
        id=task_id,
        context_id=state.thread_id,
        status=map_runtime_to_task(state.status),
        artifacts=artifacts,
        history=history,
        metadata={
            "run_id": state.run_id,
            "completed_steps": len(state.completed_steps),
            "failed_steps": len(state.failed_steps),
        },
        created_timestamp=state.created_at,
        last_updated_timestamp=state.updated_at,
        internal_run_id=state.run_id,
    )


def _map_evidence_to_artifact(evidence: Any) -> Artifact | None:
    """运行证据 → 对外 Artifact（脱敏；无 kind 时跳过）。"""
    kind = getattr(evidence, "kind", "") or ""
    if not kind:
        return None
    note = getattr(evidence, "note", "") or ""
    return Artifact(
        artifact_id=getattr(evidence, "evidence_id", "") or f"ev-{kind}",
        name=kind,
        description=note[:2000],
        parts=[Part(kind="text", text=(note or kind)[:MAX_TEXT_PART_CHARS])],
        metadata={"acceptance_index": getattr(evidence, "acceptance_index", None)},
    )


# ── 不可信输入净化与校验（4B-1：外部输入一律视为不可信）──────


def validate_external_input(
    message: Message,
    *,
    max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    max_parts: int = DEFAULT_MAX_PARTS_PER_MESSAGE,
    allow_file_uri: bool = False,
) -> None:
    """外部 Message 校验：大小、类型、来源与恶意内容检查。

    Raises:
        InvalidInputError: 内容超限、类型非法、URI 非法或含凭证/脚本注入标记。
    """
    if message.task_id and len(message.task_id) > 100:
        raise InvalidInputError("task_id too long")
    if len(message.parts) > max_parts:
        raise InvalidInputError(f"too many parts: {len(message.parts)} > {max_parts}")

    total_chars = 0
    for part in message.parts:
        total_chars += _validate_part(part, allow_file_uri=allow_file_uri)

    encoded = _encode_message_size(message)
    if encoded > max_bytes:
        raise InvalidInputError(
            f"message too large: {encoded} bytes > {max_bytes} bytes"
        )
    if total_chars > MAX_TEXT_PART_CHARS:
        raise InvalidInputError("aggregate text content exceeds limit")


def _validate_part(part: Part, *, allow_file_uri: bool) -> int:
    chars = 0
    if part.kind == "text":
        text = part.text or ""
        chars = len(text)
        if _CREDENTIAL_PATTERNS.search(text):
            raise InvalidInputError("text contains credential-like content")
        if _MALICIOUS_TEXT_PATTERNS.search(text):
            raise InvalidInputError("text contains malicious content pattern")
    elif part.kind == "file":
        if part.uri:
            if not allow_file_uri:
                # 首版禁用 file:// 与 data: URI（SSRF/注入防线之一）
                if part.uri.lower().startswith(("file:", "data:")):
                    raise InvalidInputError("file URI scheme not allowed")
            if not part.uri.lower().startswith(("https://", "http://")):
                raise InvalidInputError("uri must be http(s)")
        if not part.name and not part.uri:
            raise InvalidInputError("file part requires name or uri")
    elif part.kind == "data":
        if part.data is None:
            raise InvalidInputError("data part requires data")
    return chars


def _encode_message_size(message: Message) -> int:
    try:
        import json

        return len(
            json.dumps(message.model_dump(mode="json"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
    except Exception:
        return 0


def build_denied_task(task_id: str, reason: str) -> Task:
    """构造 REJECTED 终态 Task（不可信输入被拒绝时的可解释返回）。"""
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    return Task(
        id=task_id,
        status=TaskStatus.REJECTED,
        metadata={"error": reason[:500]},
        created_timestamp=now,
        last_updated_timestamp=now,
    )
