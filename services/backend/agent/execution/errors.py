"""Execution Layer 统一异常（§30 无隐式回退）。"""

from __future__ import annotations


class ExecutionError(RuntimeError):
    """Execution 层基础异常。"""

    code: str = "execution_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class UnsupportedExecutionMode(ExecutionError):
    """Router 遇到未声明执行模式（§30）。"""

    def __init__(self, mode: str) -> None:
        super().__init__(f"unsupported execution mode: {mode}", code="unsupported_mode")
        self.mode = mode


class EngineNotConfigured(ExecutionError):
    """Router 需要某一 Engine 但未装配（§30 / §78 lazy legacy）。"""

    def __init__(self, engine: str) -> None:
        super().__init__(f"execution engine not configured: {engine}", code="engine_not_configured")
        self.engine = engine


class LegacyNotAvailable(ExecutionError):
    """skill_planned 模式下显式要求 legacy，但 legacy 未装配（§79）。"""

    def __init__(self) -> None:
        super().__init__("legacy executor is not loaded in this mode", code="legacy_not_loaded")


class ResumeNotSupported(ExecutionError):
    """当前引擎不支持 checkpoint resume，仅支持幂等 replay（§66）。"""

    def __init__(self, engine: str) -> None:
        super().__init__(f"resume not supported by engine: {engine}", code="resume_not_supported")
        self.engine = engine


__all__ = [
    "EngineNotConfigured",
    "ExecutionError",
    "LegacyNotAvailable",
    "ResumeNotSupported",
    "UnsupportedExecutionMode",
]
