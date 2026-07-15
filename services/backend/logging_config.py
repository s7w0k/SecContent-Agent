"""
企业级日志配置 - JSON 结构化 + 按日期轮转 + 敏感脱敏 + 分级文件 + 归档压缩

日志目录结构:
  logs/
  ├── app/              # 应用业务日志 (INFO+)
  │   └── 2026-07-13.log
  ├── error/            # ERROR + CRITICAL 级别
  │   └── 2026-07-13.log
  ├── access/           # HTTP 请求/响应日志
  │   └── 2026-07-13.log
  ├── audit/            # 审计日志（用户关键操作）
  │   └── 2026-07-13.log
  └── archive/          # 归档压缩（自动生成）
      └── app-2026-07-10.log.gz

日志格式 (JSON 单行):
  {"timestamp":"2026-07-13T14:09:05.515+08:00","level":"INFO",
   "logger":"backend.api.pipeline","trace_id":"trace-001","user_id":"u-xxx",
   "message":"流水线执行完成","module":"api.pipeline","function":"run_v2",
   "line":125,"extra":{"phase":"draft","duration_ms":45000}}
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import re
import shutil
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import override

# ── 上下文变量（trace_id / user_id 透传）─────────────────

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_trace_id(trace_id: str | None) -> None:
    _trace_id_var.set(trace_id)


def set_user_id(user_id: str | None) -> None:
    _user_id_var.set(user_id)


def set_request_id(request_id: str | None) -> None:
    _request_id_var.set(request_id)


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def get_request_id() -> str | None:
    return _request_id_var.get()


# ── 敏感信息脱敏 ────────────────────────────────────────

_SENSITIVE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # password=xxx, "password": "xxx"
    (re.compile(r'(?i)(password|passwd|pwd)(["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)'), r'\1\2***MASKED***'),
    # api_key=xxx, "api_key": "xxx"
    (re.compile(r'(?i)(api[_-]?key)(["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)'), r'\1\2***MASKED***'),
    # token=xxx, "token": "xxx"
    (re.compile(r'(?i)(token|jwt|secret)(["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)'), r'\1\2***MASKED***'),
    # Authorization: Bearer xxx
    (re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9\-\.=_]+'), r'\1***MASKED***'),
    # mongodb://user:password@host
    (re.compile(r'(mongodb(?:\+srv)?://)[^:]+:[^@]+(@)'), r'\1***:***\2'),
    # "Authorization": "xxx"
    (re.compile(r'(?i)("authorization"\s*:\s*")[^"]+(")'), r'\1***MASKED***\2'),
]


def mask_sensitive(text: str) -> str:
    """脱敏敏感信息"""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ── JSON 格式化器 ──────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """将日志记录格式化为 JSON 单行，便于 ELK/Loki 采集"""

    # 标准 ISO 8601 时间戳，带时区
    @override
    def formatTime(
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        tz = timezone(timedelta(hours=8))
        dt = datetime.fromtimestamp(record.created, tz=tz)
        return dt.isoformat()

    def format(self, record: logging.LogRecord) -> str:
        tz = timezone(timedelta(hours=8))
        dt = datetime.fromtimestamp(record.created, tz=tz)

        log_entry = {
            "timestamp": dt.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_sensitive(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # 上下文变量
        trace_id = _trace_id_var.get()
        if trace_id:
            log_entry["trace_id"] = trace_id

        user_id = _user_id_var.get()
        if user_id:
            log_entry["user_id"] = user_id

        request_id = _request_id_var.get()
        if request_id:
            log_entry["request_id"] = request_id

        # 异常信息
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]) if record.exc_info[1] else "",
                "stack_trace": self.formatException(record.exc_info),
            }

        # 额外字段（通过 logger.info(msg, extra={...}) 传入）
        for key in ("phase", "action", "duration_ms", "method", "path", "status",
                     "client_ip", "task_id", "detail", "error_code"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        # 其余 extra 属性
        reserved = set(dir(logging.LogRecord("", 0, "", 0, "", None, None)))
        for key, val in record.__dict__.items():
            if key not in reserved and key not in log_entry and not key.startswith("_"):
                try:
                    json.dumps(val)
                    log_entry[key] = val
                except (TypeError, ValueError):
                    log_entry[key] = str(val)

        return json.dumps(log_entry, ensure_ascii=False, default=str)


# ── 级别过滤器 ──────────────────────────────────────────

class LevelFilter(logging.Filter):
    """按日志级别过滤"""

    def __init__(self, min_level: str, max_level: str | None = None):
        self.min_level = getattr(logging, min_level.upper(), logging.INFO)
        self.max_level = getattr(logging, max_level.upper(), logging.CRITICAL) if max_level else logging.CRITICAL

    def filter(self, record: logging.LogRecord) -> bool:
        return self.min_level <= record.levelno <= self.max_level


# ── 自定义审计 Logger ───────────────────────────────────

class AuditLogger:
    """审计日志：记录用户关键操作（登录/流水线/改稿/反馈/注销等）"""

    _instance: AuditLogger | None = None
    _lock = threading.Lock()

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    @classmethod
    def get_instance(cls) -> AuditLogger:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    logger = logging.getLogger("audit")
                    cls._instance = cls(logger)
        return cls._instance

    def log(self, user_id: str, action: str, resource: str = "",
            detail: dict | None = None, level: str = "INFO"):
        """记录审计日志

        Args:
            user_id: 用户 ID
            action: 操作类型（login/logout/pipeline_trigger/draft_generate/
                    draft_revise/draft_apply/feedback_submit/profile_rebuild/
                    account_delete/developer_mode_change）
            resource: 操作资源（如 article_url_hash）
            detail: 额外详情
        """
        set_user_id(user_id)
        msg = f"[AUDIT] {action}"
        if resource:
            msg += f" resource={resource}"
        extra = {"action": action, "detail": detail or {}}
        getattr(self._logger, level.lower(), self._logger.info)(msg, extra=extra)


# ── 归档压缩（轮转后自动 gzip）──────────────────────────

def _compress_rotated_log(deleted_path: str) -> None:
    """TimedRotatingFileHandler 的 rotator 回调：压缩旧日志"""
    if not os.path.exists(deleted_path):
        return
    gz_path = deleted_path + ".gz"
    try:
        with open(deleted_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(deleted_path)
    except Exception:
        pass  # 压缩失败不影响主流程


# ── 核心配置入口 ────────────────────────────────────────

def setup_logging(
    log_dir: str = "/app/logs",
    log_level: str = "INFO",
    app_retention_days: int = 30,
    error_retention_days: int = 90,
    access_retention_days: int = 7,
    audit_retention_days: int = 365,
) -> None:
    """初始化企业级日志系统

    Args:
        log_dir: 日志根目录
        log_level: 全局最低日志级别
        app_retention_days: 应用日志保留天数
        error_retention_days: 错误日志保留天数
        access_retention_days: 访问日志保留天数
        audit_retention_days: 审计日志保留天数
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # 清除已有 handlers（避免重复）
    root_logger.handlers.clear()

    formatter = JSONFormatter()

    # ── 控制台 handler（开发调试用）──────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root_logger.addHandler(console_handler)

    # 如果日志目录不可写（如 read-only 未挂载），仅用控制台
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        test_file = Path(log_dir) / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except (OSError, PermissionError):
        logging.getLogger("backend").warning(
            "日志目录 %s 不可写，仅输出到控制台", log_dir
        )
        return

    # ── 应用日志 handler（INFO+，按日期轮转）─────────────
    app_dir = Path(log_dir) / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    app_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(app_dir / "app.log"),
        when="midnight",
        interval=1,
        backupCount=app_retention_days,
        encoding="utf-8",
        utc=False,
    )
    app_handler.suffix = "%Y-%m-%d"
    app_handler.rotator = _compress_rotated_log
    app_handler.setFormatter(formatter)
    app_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root_logger.addHandler(app_handler)

    # ── 错误日志 handler（ERROR+CRITICAL，独立文件）──────
    error_dir = Path(log_dir) / "error"
    error_dir.mkdir(parents=True, exist_ok=True)
    error_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(error_dir / "error.log"),
        when="midnight",
        interval=1,
        backupCount=error_retention_days,
        encoding="utf-8",
        utc=False,
    )
    error_handler.suffix = "%Y-%m-%d"
    error_handler.rotator = _compress_rotated_log
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)
    root_logger.addHandler(error_handler)

    # ── 访问日志 handler（HTTP 请求，独立 logger）─────────
    access_dir = Path(log_dir) / "access"
    access_dir.mkdir(parents=True, exist_ok=True)
    access_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(access_dir / "access.log"),
        when="midnight",
        interval=1,
        backupCount=access_retention_days,
        encoding="utf-8",
        utc=False,
    )
    access_handler.suffix = "%Y-%m-%d"
    access_handler.rotator = _compress_rotated_log
    access_handler.setFormatter(formatter)
    access_handler.setLevel(logging.INFO)
    access_logger = logging.getLogger("access")
    access_logger.handlers.clear()
    access_logger.addHandler(access_handler)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False  # 不向 root 传播

    # ── 审计日志 handler（用户关键操作，独立 logger）──────
    audit_dir = Path(log_dir) / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(audit_dir / "audit.log"),
        when="midnight",
        interval=1,
        backupCount=audit_retention_days,
        encoding="utf-8",
        utc=False,
    )
    audit_handler.suffix = "%Y-%m-%d"
    audit_handler.rotator = _compress_rotated_log
    audit_handler.setFormatter(formatter)
    audit_handler.setLevel(logging.INFO)
    audit_logger = logging.getLogger("audit")
    audit_logger.handlers.clear()
    audit_logger.addHandler(audit_handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False  # 不向 root 传播

    logging.getLogger("backend").info(
        "日志系统初始化完成",
        extra={
            "detail": {
                "log_dir": log_dir,
                "log_level": log_level,
                "retention": {
                    "app": app_retention_days,
                    "error": error_retention_days,
                    "access": access_retention_days,
                    "audit": audit_retention_days,
                },
            }
        },
    )


# ── 便捷函数 ────────────────────────────────────────────

def get_audit_logger() -> AuditLogger:
    """获取审计日志单例"""
    return AuditLogger.get_instance()


def log_request(
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    client_ip: str,
    user_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """记录 HTTP 请求日志"""
    access_logger = logging.getLogger("access")
    set_request_id(request_id)
    if user_id:
        set_user_id(user_id)
    access_logger.info(
        f"{method} {path} -> {status} ({duration_ms}ms)",
        extra={
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "client_ip": client_ip,
        },
    )
