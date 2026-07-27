"""安全文件写入服务 - 路径校验与原子替换。"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("backend.knowledge_admin.file_store")


class KnowledgeFileStore:
    """在知识库根目录中安全地读取和原子写入 Markdown 文件。"""

    def __init__(self, root_dir: str | Path):
        self.root_dir = Path(root_dir).resolve()

    def read_file(self, relative_path: str) -> str:
        """安全读取文件内容。"""
        safe_path = self._validate_path(relative_path)
        if not safe_path.exists():
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        return safe_path.read_text(encoding="utf-8")

    def compute_hash(self, relative_path: str) -> str:
        """计算文件 SHA-256 哈希。"""
        content = self.read_file(relative_path)
        return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"

    def atomic_write(self, relative_path: str, content: str) -> str:
        """原子写入文件，返回新内容哈希。

        步骤：
        1. 验证路径安全
        2. 在同目录创建临时文件（后缀不是 .md）
        3. 写入 UTF-8 内容
        4. flush + fsync
        5. os.replace() 原子替换
        6. 重新读取验证哈希
        """
        safe_path = self._validate_path(relative_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        # Create temp file in same directory (not .md suffix)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(safe_path.parent),
            prefix=f".{safe_path.stem}.",
            suffix=".kbp-tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace
            os.replace(tmp_path, safe_path)
            logger.info("Atomically wrote: %s", relative_path)
        except Exception:
            # Cleanup temp file on failure
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        # Verify
        written = safe_path.read_text(encoding="utf-8")
        written_hash = f"sha256:{hashlib.sha256(written.encode('utf-8')).hexdigest()}"
        return written_hash

    def _validate_path(self, relative_path: str) -> Path:
        """验证路径安全性（同 KnowledgeCatalog._validate_path 逻辑）。"""
        if not relative_path:
            raise ValueError("路径不能为空")

        normalized = relative_path.replace("\\", "/")
        if ".." in normalized.split("/"):
            raise ValueError("路径不允许包含 '..'")

        if os.path.isabs(relative_path):
            raise ValueError("路径不允许为绝对路径")

        target = (self.root_dir / relative_path).resolve()

        if self.root_dir not in target.parents and target != self.root_dir:
            raise ValueError("路径超出知识库根目录")

        if target.is_symlink():
            raise ValueError("路径不允许为符号链接")

        if target.suffix != ".md":
            raise ValueError("仅允许操作 Markdown 文件")

        return target
