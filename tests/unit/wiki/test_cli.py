"""Phase 22 Wiki CLI（bootstrap/validate）单元测试（PR-21）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent.wiki.cli import _cmd_validate, main


def _legacy_settings() -> SimpleNamespace:
    return SimpleNamespace(
        KNOWLEDGE_BACKEND="legacy",
        KNOWLEDGE_BASE_DIR="/app/docs",
        WIKI_ROOT_DIR="",
        WIKI_REQUIRE_SOURCE_GROUNDING=True,
    )


def test_cmd_validate_legacy_not_enforced():
    rc = asyncio.run(_cmd_validate(_legacy_settings(), None))
    assert rc == 0


def test_main_validate_returns_zero(monkeypatch):
    # 非 wiki 模式仅打印提示并返回 0，不触碰文件系统
    monkeypatch.delenv("KNOWLEDGE_BACKEND", raising=False)
    monkeypatch.setenv("KNOWLEDGE_BACKEND", "legacy")
    assert main(["validate"]) == 0


def test_cli_imports_bootstrap_handler():
    # bootstrap handler 应可装配（不实际运行发布）
    from agent.wiki.cli import _wire_maintainer

    settings = _legacy_settings()
    settings.KNOWLEDGE_BACKEND = "wiki"
    settings.KNOWLEDGE_BASE_DIR = "/nonexistent/docs"
    settings.WIKI_ROOT_DIR = "/nonexistent/_wiki"
    # 缺少 wiki 产物时装配可成功（不校验），运行 bootstrap 才失败
    maintainer = _wire_maintainer(settings, None)
    assert hasattr(maintainer, "run_transaction")
