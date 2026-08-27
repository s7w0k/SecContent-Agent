"""Wiki Version Manifest - 不可变版本元数据与 Active Pointer（Phase2 / G-10）。

- 每个发布的 immutable Wiki Version 记录一份 Manifest；
- Active Pointer 指向当前生产 Wiki Version，作为 Runtime 的读取入口；
- Rollback 只需切换 Active Pointer（Wiki + Registry snapshot 原文一致，见 publisher.rollback）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.manifest")

MANIFEST_FILENAME = "manifest.json"
ACTIVE_POINTER_FILENAME = "active-wiki-version.txt"


def build_version_id(parent_version: str = "", seed: str = "") -> str:
    """生成有序且大概率唯一的版本 ID。

    形如：wiki-20260827T123456.123456Z-<hash8>-<uuid8>
    避免仅用秒级时间戳（同一秒内重复发布冲突）。
    """
    ts = datetime.now(UTC).isoformat()
    digest = hashlib.sha256(f"{ts}|{parent_version}|{seed}|{uuid.uuid4()}".encode()).hexdigest()
    return f"wiki-{ts}-{digest[:8]}-{uuid.uuid4().hex[:8]}"


def page_tree_hash(pages: list[Any]) -> str:
    """基于 (page_id, content_hash) 的确定性树哈希（与 index.compute_wiki_version 对齐）。"""
    payload = sorted(
        f"{p.page_id};{p.content_hash}" for p in pages if getattr(p, "content_hash", "")
    )
    blob = "\n".join(payload)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class WikiVersionManifest(BaseModel):
    """单个不可变 Wiki 版本的元数据。"""

    wiki_version: str = Field(description="wiki_version ID")
    schema_version: int = Field(default=2, ge=1)
    compiler_version: str = Field(default="")
    source_snapshot_id: str = Field(default="")
    source_snapshot_hash: str = Field(default="")
    page_tree_hash: str = Field(default="")
    page_count: int = Field(default=0, ge=0)
    created_at: str = Field(default="")
    build_id: str = Field(default="")
    gate_report_hash: str = Field(default="")
    parent_version: str = Field(default="")
    status: str = Field(default="published")

    def validate_self(self) -> list[str]:
        """校验 manifest 自身一致性（G-11 fail-fast 输入）。"""
        errors: list[str] = []
        if not self.wiki_version:
            errors.append("MISSING_WIKI_VERSION")
        if not self.source_snapshot_hash:
            errors.append("MISSING_SOURCE_SNAPSHOT_HASH")
        if not self.page_tree_hash:
            errors.append("MISSING_PAGE_TREE_HASH")
        if self.page_count < 0:
            errors.append("NEGATIVE_PAGE_COUNT")
        return errors


class ManifestStore:
    """Manifest 与 Active Pointer 的持久化读写（原子写）。"""

    def __init__(self, versions_dir: str | Path):
        self.versions_dir = Path(versions_dir)
        self.active_path = self.versions_dir / ACTIVE_POINTER_FILENAME

    def version_dir(self, wiki_version: str) -> Path:
        return self.versions_dir / _safe_version_dirname(wiki_version)

    def write(self, manifest: WikiVersionManifest) -> Path:
        vdir = self.version_dir(manifest.wiki_version)
        vdir.mkdir(parents=True, exist_ok=True)
        target = vdir / MANIFEST_FILENAME
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)
        return target

    def load(self, wiki_version: str) -> WikiVersionManifest | None:
        path = self.version_dir(wiki_version) / MANIFEST_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return WikiVersionManifest.model_validate(data)
        except Exception as exc:
            logger.error("Manifest 加载失败 %s: %s", wiki_version, exc)
            return None

    def list_versions(self) -> list[WikiVersionManifest]:
        out: list[WikiVersionManifest] = []
        if not self.versions_dir.is_dir():
            return out
        for child in self.versions_dir.iterdir():
            if not child.is_dir():
                continue
            m = self.load(child.name)
            if m is not None:
                out.append(m)
        return sorted(out, key=lambda m: m.created_at)

    def set_active(self, wiki_version: str) -> None:
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.active_path.with_suffix(".tmp")
        tmp.write_text(wiki_version, encoding="utf-8")
        tmp.replace(self.active_path)

    def active_version(self) -> str:
        if not self.active_path.exists():
            return ""
        return self.active_path.read_text(encoding="utf-8").strip()

    def has_active(self) -> bool:
        active = self.active_version()
        return bool(active) and self.load(active) is not None


def _safe_version_dirname(wiki_version: str) -> str:
    # 版本 ID 由 ASCII 组成，但为防御外部构造仍做严格白名单
    cleaned = "".join(ch for ch in wiki_version if ch.isalnum() or ch in "-._")
    return cleaned or "version-unknown"
