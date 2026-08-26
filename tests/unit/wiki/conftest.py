"""Wiki 测试共享 fixtures 与构造工具。"""

from __future__ import annotations

from pathlib import Path

import pytest
from agent.wiki.source_registry import SourceRegistry
from agent.wiki.store import WikiStore


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    """Raw Source 根目录。"""
    return tmp_path / "docs"


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    """Wiki 根目录。"""
    return tmp_path / "docs" / "_wiki"


@pytest.fixture
def store(wiki_root: Path) -> WikiStore:
    return WikiStore(wiki_root)


@pytest.fixture
def registry(source_root: Path, wiki_root: Path) -> SourceRegistry:
    reg = SourceRegistry(source_root, wiki_root / "_meta" / "source-registry.json")
    reg.sync()
    return reg
