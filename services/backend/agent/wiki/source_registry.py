"""SourceRegistry - Raw Source 的稳定注册表。

PR-02 产物：
  - 稳定 source_id（基于 relative_path 的 sha256，不随内容变化）
  - 内容变化用 content_hash（sha256）识别
  - 扫描 Raw Source 树，区分 NEW / CHANGED / UNCHANGED / DELETED
  - 将注册表持久化到 Wiki `_meta/source-registry.json`
  - 命令行入口：`python -m agent.wiki.source_registry <root> [registry_path]`

设计约束：
  - 不把整个目录每次无状态扫描交给 Compiler；通过本注册表做增量。
  - `_wiki`、排除目录的原始文档不进入注册表（避免把 Wiki 产物当源）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.source_registry")

# 原始文档中需要排除的目录（复用 Legacy 索引的排除集 + `_wiki`）
EXCLUDED_DIRS = frozenset(
    {"skills", "_index", "海外版", ".git", "__pycache__", "_wiki", "原始文档"}
)
# 根级管理/非事实文档
EXCLUDED_ROOT_FILES = frozenset({"AGENTS.md", "CLAUDE.md", "README.md", "qa-log.md"})

DEFAULT_FILENAME = "source-registry.json"
SOURCE_TYPE_OFFICIAL = "official_product_doc"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk")


def stable_source_id(relative_path: str) -> str:
    """基于稳定输入（relative_path）生成 source_id，内容变化不影响 source_id。"""
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
    return "src_" + digest[:16]


def content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_safe_rel(rel: str) -> bool:
    if not rel:
        return False
    if any(part in {"..", ".", ""} for part in rel.split("/")):
        return False
    if os.path.isabs(rel) or rel.startswith("/"):
        return False
    return not ("%2e" in rel.lower() or "%2f" in rel.lower())


class SourceEntry(BaseModel):
    """单个 Raw Source 条目。"""

    source_id: str = Field(description="稳定 source_id，基于 relative_path")
    relative_path: str = Field(description="相对 Raw Source 根的路径")
    sha256: str = Field(description="内容 SHA-256 哈希")
    product_ids: list[str] = Field(default_factory=list)
    source_type: str = Field(default=SOURCE_TYPE_OFFICIAL)
    last_seen_at: str = Field(default="")
    status: str = Field(default="active", description="active / deleted")


class SourceRegistrySnapshot(BaseModel):
    """注册表快照（持久化格式）。"""

    schema_version: int = Field(default=1)
    updated_at: str = Field(default="")
    sources: list[SourceEntry] = Field(default_factory=list)


class DiffReport(BaseModel):
    """扫描差异报告。"""

    new: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"NEW: {len(self.new)}, CHANGED: {len(self.changed)}, "
            f"UNCHANGED: {len(self.unchanged)}, DELETED: {len(self.deleted)}"
        )


class SourceRegistry:
    """Raw Source 注册表。"""

    def __init__(
        self,
        source_root: str | Path,
        registry_path: str | Path | None = None,
        product_catalog: Any = None,
    ):
        self.root = Path(source_root).resolve()
        self.registry_path = (
            Path(registry_path)
            if registry_path
            else self.root / "_wiki" / "_meta" / DEFAULT_FILENAME
        )
        self._catalog = product_catalog
        self._entries: dict[str, SourceEntry] = {}
        self._load()

    # ── 持久化 ──────────────────────────────────────────────

    def _load(self) -> None:
        if not self.registry_path.exists():
            return
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            snap = SourceRegistrySnapshot.model_validate(data)
            self._entries = {e.source_id: e for e in snap.sources}
        except Exception as exc:
            logger.warning("SourceRegistry 加载失败: %s", exc)

    def save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        snap = SourceRegistrySnapshot(
            schema_version=1,
            updated_at=datetime.now(UTC).isoformat(),
            sources=sorted(self._entries.values(), key=lambda e: e.relative_path),
        )
        tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(snap.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.registry_path)

    # ── 扫描与发现 ──────────────────────────────────────────

    def _product_id_for(self, rel: str) -> str | None:
        if self._catalog is None:
            return None
        for product in self._catalog.list_products(published_only=False):
            root = product.knowledge_root
            if rel == root or rel.startswith(root + "/"):
                return product.product_id
        return None

    def scan_relative_paths(self) -> list[str]:
        """发现 Raw Source 下所有应注册的 .md 文件（排除 Wiki 与排除目录）。"""
        results: list[str] = []
        if not self.root.is_dir():
            return results
        for fp in sorted(self.root.rglob("*.md")):
            if fp.is_symlink():
                continue
            rel = str(fp.relative_to(self.root)).replace("\\", "/")
            if not _is_safe_rel(rel):
                continue
            parts = rel.split("/")
            if parts[0] in EXCLUDED_DIRS:
                continue
            if len(parts) == 1 and parts[0] in EXCLUDED_ROOT_FILES:
                continue
            results.append(rel)
        return results

    def build_state(self) -> dict[str, str]:
        """扫描当前所有源文件，返回 {relative_path: sha256}。"""
        state: dict[str, str] = {}
        for rel in self.scan_relative_paths():
            try:
                content = _read_text(self.root / rel)
            except Exception as exc:
                logger.warning("读取失败跳过 %s: %s", rel, exc)
                continue
            state[rel] = content_hash(content)
        return state

    def diff(self) -> DiffReport:
        """对比当前磁盘状态与上次注册表，输出 NEW/CHANGED/UNCHANGED/DELETED。"""
        state = self.build_state()
        prev = {e.relative_path: e.sha256 for e in self._entries.values()}
        return self._diff_against(state, prev)

    @staticmethod
    def _diff_against(state: dict[str, str], prev: dict[str, str]) -> DiffReport:
        report = DiffReport()
        for rel in sorted(state):
            cur_hash = state[rel]
            old_hash = prev.get(rel)
            if old_hash is None:
                report.new.append(rel)
            elif old_hash != cur_hash:
                report.changed.append(rel)
            else:
                report.unchanged.append(rel)
        for rel in prev:
            if rel not in state:
                report.deleted.append(rel)
        return report

    # ── 同步 ────────────────────────────────────────────────

    def sync(self) -> DiffReport:
        """扫描并更新注册表到当前磁盘状态，返回差异报告。"""
        state = self.build_state()
        # 先取基线，避免新文件在 diff 前已被当作 prev，导致 new 判为 unchanged
        prev = {e.relative_path: e.sha256 for e in self._entries.values()}
        now = datetime.now(UTC).isoformat()

        for rel, cur_hash in state.items():
            source_id = stable_source_id(rel)
            entry = self._entries.get(source_id)
            if entry is None:
                product_id = self._product_id_for(rel)
                self._entries[source_id] = SourceEntry(
                    source_id=source_id,
                    relative_path=rel,
                    sha256=cur_hash,
                    product_ids=[product_id] if product_id else [],
                    last_seen_at=now,
                    status="active",
                )
            else:
                entry.sha256 = cur_hash
                entry.last_seen_at = now
                entry.status = "active"

        for entry in self._entries.values():
            if entry.relative_path not in state:
                entry.status = "deleted"

        report = self._diff_against(state, prev)
        self.save()
        return report

    # ── 查询 ────────────────────────────────────────────────

    def get(self, source_id: str) -> SourceEntry | None:
        return self._entries.get(source_id)

    def get_by_path(self, relative_path: str) -> SourceEntry | None:
        target = stable_source_id(relative_path)
        return self._entries.get(target)

    def all_entries(self) -> list[SourceEntry]:
        return sorted(self._entries.values(), key=lambda e: e.relative_path)


def run(source_root: str | None = None, registry_path: str | None = None) -> int:
    """命令行入口：扫描并输出差异报告。"""
    import sys

    root = (
        source_root or sys.argv[1]
        if len(sys.argv) > 1 and source_root is None
        else (source_root or "/app/docs")
    )
    reg_path = registry_path or (sys.argv[2] if len(sys.argv) > 2 else None)
    registry = SourceRegistry(root, reg_path)
    report = registry.sync()
    print(report.summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
