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
import re
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


class WikiPathError(ValueError):
    """Source/Page 路径安全校验失败（§7.2 / §19.5）。"""


# 常见 Secret 指纹（§19.4 Secret Quarantine）
SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    ),
    ("api_key", re.compile(r"(?i)(api[_-]?key|secret[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{16,}")),
    (
        "password",
        re.compile(r"(?i)(password|passwd|pwd|secret)\s*[:=]\s*['\"]?\S+"),
    ),
    (
        "connection_string",
        re.compile(r"(?i)(mongodb(\+srv)?|postgres(ql)?|mysql|redis|amqp|jdbc|clickhouse):"),
    ),
)


def detect_secret(content: str) -> list[str]:
    """检测 Raw Source 内容是否含密钥/凭据，返回命中的 Secret 类型列表（空 = 安全）。"""
    kinds: list[str] = []
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(content or ""):
            kinds.append(kind)
    return kinds


def resolve_source_path(root: str | Path, rel: str) -> Path:
    """把相对路径安全解析为 Root 下真实存在的文件；越界/symlink 逃逸抛 WikiPathError。

    校验（§7.2）：
      - 拒绝空、绝对路径、`../`、URL 编码穿越、NUL、Windows 盘符
      - 解析后必须仍在 source_root 内（防 symlink escape）
      - 必须是普通文件
    """
    if not rel or not _is_safe_rel(rel):
        raise WikiPathError(f"不安全的相对路径: {rel!r}")
    root_resolved = Path(root).resolve()
    candidate = root_resolved / rel.replace("\\", "/")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WikiPathError(f"路径解析失败（可能 symlink 无效）: {rel}") from exc
    if not resolved.is_relative_to(root_resolved):
        raise WikiPathError(f"路径越界（symlink 逃逸）: {rel}")
    if not resolved.is_file():
        raise WikiPathError(f"目标不是普通文件: {rel}")
    return resolved


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
    status: str = Field(default="active", description="active / deleted / quarantined")
    secret_kinds: list[str] = Field(
        default_factory=list, description="命中 Secret 的类型（§19.4），非空表示已被隔离"
    )


class SourceRegistrySnapshot(BaseModel):
    """注册表快照（持久化格式）。"""

    schema_version: int = Field(default=1)
    updated_at: str = Field(default="")
    sources: list[SourceEntry] = Field(default_factory=list)


def compute_source_snapshot_hash(sources: dict[str, SourceEntry]) -> str:
    """SourceSnapshot 的确定性 hash（用于幂等 build_id 判定）。"""
    blob = "\n".join(
        f"{e.source_id};{e.relative_path};{e.sha256};{e.status}"
        for e in sorted(sources.values(), key=lambda x: x.relative_path)
    )
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SourceSnapshot(BaseModel):
    """Raw Source 在某次 Build 时点的不可变快照（Phase1 双快照 Registry）。

    - active：上一次成功发布后固化的快照
    - pending：当前 Build 扫描结果；只有 Publish 成功后才 commit 为 active
    因此 Compile/Lint/Publish 失败不会污染 active，下一轮仍能发现同一变更（G-01）。
    """

    snapshot_id: str = Field(description="快照 ID（稳定，基于 parent+hash）")
    parent_snapshot_id: str | None = Field(default=None)
    created_at: str = Field(default="")
    sources: dict[str, SourceEntry] = Field(
        default_factory=dict, description="source_id -> SourceEntry"
    )
    snapshot_hash: str = Field(default="", description="真实输入的 FAIL-safe 指纹")

    @classmethod
    def build(
        cls,
        sources: dict[str, SourceEntry],
        *,
        snapshot_id: str = "",
        parent_snapshot_id: str | None = None,
    ) -> SourceSnapshot:
        """构造快照并计算稳定 hash。snapshot_id 缺省时由内容 hash 派生（幂等）。"""
        snap_hash = compute_source_snapshot_hash(sources)
        return cls(
            snapshot_id=snapshot_id or ("snap_" + snap_hash.replace("sha256:", "")[:16]),
            parent_snapshot_id=parent_snapshot_id,
            created_at=datetime.now(UTC).isoformat(),
            sources=sources,
            snapshot_hash=snap_hash,
        )


class DiffReport(BaseModel):
    """扫描差异报告。renamed 记录 (旧路径, 新路径)。"""

    new: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    renamed: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"NEW: {len(self.new)}, CHANGED: {len(self.changed)}, "
            f"UNCHANGED: {len(self.unchanged)}, DELETED: {len(self.deleted)}, "
            f"RENAMED: {len(self.renamed)}"
        )


class SourceRegistry:
    """Raw Source 注册表。"""

    def __init__(
        self,
        source_root: str | Path,
        registry_path: str | Path | None = None,
        product_catalog: Any = None,
        *,
        max_source_bytes: int = 1_000_000,
        max_source_lines: int = 50_000,
        allowed_extensions: frozenset[str] | set[str] | None = None,
        secret_quarantine: bool = True,
    ):
        self.root = Path(source_root).resolve()
        self.registry_path = (
            Path(registry_path)
            if registry_path
            else self.root / "_wiki" / "_meta" / DEFAULT_FILENAME
        )
        self._catalog = product_catalog
        # DoS 边界（§7.3 / §19.5）
        self.max_source_bytes = max_source_bytes
        self.max_source_lines = max_source_lines
        self.allowed_extensions = frozenset(allowed_extensions or {".md"})
        self.secret_quarantine = secret_quarantine
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
        """发现 Raw Source 下所有应注册的文件（排除 Wiki、排除目录、限扩展名）。"""
        results: list[str] = []
        if not self.root.is_dir():
            return results
        for fp in sorted(self.root.rglob("*")):
            if fp.is_symlink():
                continue
            if not fp.is_file():
                continue
            if fp.suffix.lower() not in self.allowed_extensions:
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
        """扫描当前所有源文件，返回 {relative_path: sha256}（超限文件跳过，§7.3/§19.5）。"""
        state: dict[str, str] = {}
        for rel in self.scan_relative_paths():
            try:
                content = _read_text(self.root / rel)
            except Exception as exc:
                logger.warning("读取失败跳过 %s: %s", rel, exc)
                continue
            if len(content.encode("utf-8")) > self.max_source_bytes:
                logger.warning("源文件超过大小上限，跳过 %s", rel)
                continue
            if content.count("\n") + 1 > self.max_source_lines:
                logger.warning("源文件超过行数上限，跳过 %s", rel)
                continue
            state[rel] = content_hash(content)
        return state

    def diff(self) -> DiffReport:
        """对比当前磁盘状态与上次注册表，输出差异（含 Rename 检测，§7.1）。"""
        state = self.build_state()
        prev = {e.relative_path: e.sha256 for e in self._entries.values()}
        return self._apply_rename(self._diff_against(state, prev), state, prev)

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

    @staticmethod
    def _apply_rename(
        report: DiffReport, state: dict[str, str], prev: dict[str, str]
    ) -> DiffReport:
        """把"同内容消失+新增"识别为重命名（§7.1），而不是 DELETE + NEW。"""
        by_hash: dict[str, list[str]] = {}
        for rel in report.new:
            by_hash.setdefault(state.get(rel, ""), []).append(rel)
        kept_deleted: list[str] = []
        for old in report.deleted:
            candidates = by_hash.get(prev.get(old, ""), [])
            if candidates:
                new_path = candidates.pop(0)
                report.renamed.append((old, new_path))
                if new_path in report.new:
                    report.new.remove(new_path)
            else:
                kept_deleted.append(old)
        report.deleted = kept_deleted
        return report

    # ── 同步 ────────────────────────────────────────────────

    def _apply_secret_quarantine(self, state: dict[str, str]) -> None:
        """扫描 active 源内容，命中 Secret 则标记 quarantined（§19.4），禁止自动编译发布。"""
        if not self.secret_quarantine:
            return
        for entry in self._entries.values():
            if entry.relative_path not in state or entry.status != "active":
                continue
            try:
                content = _read_text(self.root / entry.relative_path)
            except Exception:
                continue
            kinds = detect_secret(content)
            entry.secret_kinds = kinds
            if kinds:
                logger.warning("Source %s quarantined: %s", entry.relative_path, ",".join(kinds))
                entry.status = "quarantined"
            else:
                entry.status = "active"

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

        if self.secret_quarantine:
            self._apply_secret_quarantine(state)

        report = self._apply_rename(self._diff_against(state, prev), state, prev)
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

    # ── 快照（Phase1 双快照 Registry）────────────────────────────

    def active_snapshot(self) -> SourceSnapshot:
        """固化当前已持久化的注册表为其 active 快照（不可变，不写盘）。"""
        return SourceSnapshot.build(dict(self._entries), parent_snapshot_id=None)

    def snapshot_pending(self) -> SourceSnapshot:
        """扫描磁盘得到 pending 快照；不写盘，失败不影响 active。

        parent 指向当前 active 快照，便于追踪连续 Build 的演进。
        """
        state = self.build_state()
        now = datetime.now(UTC).isoformat()
        sources: dict[str, SourceEntry] = {}
        for rel, cur_hash in state.items():
            source_id = stable_source_id(rel)
            existing = self._entries.get(source_id)
            if existing is not None:
                sources[source_id] = existing.model_copy(
                    update={
                        "sha256": cur_hash,
                        "last_seen_at": now,
                        "status": "active",
                    }
                )
            else:
                product_id = self._product_id_for(rel)
                sources[source_id] = SourceEntry(
                    source_id=source_id,
                    relative_path=rel,
                    sha256=cur_hash,
                    product_ids=[product_id] if product_id else [],
                    last_seen_at=now,
                    status="active",
                )
        # 仍保留因删除而不再存在的条目，标记 deleted（供 Compiler 归档/清理）
        for source_id, entry in self._entries.items():
            if source_id not in sources:
                sources[source_id] = entry.model_copy(update={"status": "deleted"})
        if self.secret_quarantine:
            for entry in sources.values():
                if entry.status != "active" or entry.relative_path not in state:
                    continue
                try:
                    content = _read_text(self.root / entry.relative_path)
                except Exception:
                    continue
                kinds = detect_secret(content)
                if kinds:
                    sources[entry.source_id] = entry.model_copy(
                        update={"status": "quarantined", "secret_kinds": kinds}
                    )
        parent = self.active_snapshot()
        return SourceSnapshot.build(
            sources,
            parent_snapshot_id=parent.snapshot_id if parent.sources else None,
        )

    def commit_snapshot(self, snapshot: SourceSnapshot) -> None:
        """把已成功发布的快照固化为 active 注册表并持久化（G-01 的 commit 点）。"""
        self._entries = dict(snapshot.sources)
        self.save()

    def snapshot_diff(
        self, snapshot: SourceSnapshot, active: SourceSnapshot | None = None
    ) -> DiffReport:
        """比较 pending 快照相对 active 快照的差异（不读盘、不写盘）。"""
        active = active if active is not None else self.active_snapshot()
        prev = {e.relative_path: e.sha256 for e in active.sources.values()}
        snap_state = {e.relative_path: e.sha256 for e in snapshot.sources.values()}
        report = self._diff_against(snap_state, prev)
        return self._apply_rename(report, snap_state, prev)


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
