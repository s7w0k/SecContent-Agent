"""安全目录扫描与文档读取服务。"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("backend.knowledge_admin.catalog")


class KnowledgeCatalog:
    """安全读取知识库目录树和文档内容。

    与 KnowledgeLoader 不同，本类展示全部 Markdown 文件，
    不进行评分相关性过滤。
    """

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).resolve()

    # ── 目录树 ────────────────────────────────────────────────

    def build_tree(
        self,
        *,
        include_empty: bool = True,
        include_raw: bool = True,
    ) -> dict:
        """构建真实文件系统目录树。"""
        children = self._scan_dir(self.root_dir, include_empty, include_raw)
        return {
            "root_name": self.root_dir.name,
            "children": children,
        }

    def _scan_dir(
        self,
        directory: Path,
        include_empty: bool,
        include_raw: bool,
    ) -> list[dict]:
        nodes: list[dict] = []
        try:
            entries = sorted(directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            return nodes

        for entry in entries:
            if entry.name.startswith(".git"):
                continue

            if entry.is_dir():
                if not include_raw and entry.name == "原始文档":
                    continue
                children = self._scan_dir(entry, include_empty, include_raw)
                if not children and not include_empty:
                    continue
                nodes.append(
                    {
                        "name": entry.name,
                        "path": self._relative_path(entry),
                        "node_type": "dir",
                        "children": children,
                    }
                )
            elif entry.is_file() and entry.suffix == ".md":
                nodes.append(
                    {
                        "name": entry.name,
                        "path": self._relative_path(entry),
                        "node_type": "file",
                    }
                )
        return nodes

    # ── 文档读取 ───────────────────────────────────────────────

    def get_document(self, relative_path: str) -> dict:
        """读取单个 Markdown 文档。

        Raises:
            ValueError: 路径不安全或文件不存在。
        """
        safe_path = self._validate_path(relative_path)
        if not safe_path.exists():
            raise ValueError(f"文件不存在: {relative_path}")

        content = safe_path.read_text(encoding="utf-8")
        stat = safe_path.stat()

        return {
            "relative_path": self._relative_path(safe_path),
            "content": content,
            "content_hash": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
            "size": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        }

    # ── 搜索 ───────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        role_filter: str | None = None,
        direct_scoring_only: bool = False,
    ) -> list[dict]:
        """按文件名和正文子串搜索。"""
        results: list[dict] = []
        query_lower = query.lower()

        for md_file in sorted(self.root_dir.rglob("*.md")):
            if ".git" in md_file.parts:
                continue

            rel_path = self._relative_path(md_file)
            filename_lower = md_file.name.lower()

            try:
                content = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            if query_lower not in filename_lower and query_lower not in content.lower():
                continue

            results.append(
                {
                    "relative_path": rel_path,
                    "name": md_file.name,
                    "content_hash": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
                    "size": md_file.stat().st_size,
                    "snippet": content[:200] if content else "",
                }
            )

        return results

    # ── 文档 ID ────────────────────────────────────────────────

    @staticmethod
    def get_document_id(relative_path: str) -> str:
        """根据规范化相对路径生成文档 ID。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def resolve_document_id(self, document_id: str) -> str | None:
        """根据文档 ID 反查相对路径。"""
        for md_file in self.root_dir.rglob("*.md"):
            if ".git" in md_file.parts:
                continue
            rel_path = self._relative_path(md_file)
            if self.get_document_id(rel_path) == document_id:
                return rel_path
        return None

    # ── 统计 ───────────────────────────────────────────────────

    def count_files(self) -> int:
        """统计全部 Markdown 文件数量。"""
        return sum(1 for f in self.root_dir.rglob("*.md") if ".git" not in f.parts)

    # ── 内部工具 ───────────────────────────────────────────────

    def _relative_path(self, abs_path: Path) -> str:
        """返回相对于 root_dir 的路径（正斜杠）。"""
        return str(abs_path.relative_to(self.root_dir)).replace("\\", "/")

    def _validate_path(self, relative_path: str) -> Path:
        """验证路径安全性，返回绝对路径。

        Raises:
            ValueError: 路径不合法。
        """
        if not relative_path:
            raise ValueError("路径不能为空")

        if ".." in relative_path.replace("\\", "/").split("/"):
            raise ValueError("路径不允许包含 '..'")

        if os.path.isabs(relative_path):
            raise ValueError("路径不允许为绝对路径")

        raw_target = self.root_dir / relative_path
        if raw_target.is_symlink():
            raise ValueError("路径不允许为符号链接")

        target = raw_target.resolve()

        if self.root_dir not in target.parents and target != self.root_dir:
            raise ValueError("路径超出知识库根目录")

        if target.suffix != ".md":
            raise ValueError("仅允许访问 Markdown 文件")

        return target
