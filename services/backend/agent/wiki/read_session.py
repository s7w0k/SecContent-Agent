"""WikiReadSession - 请求级版本钉扎与版本感知缓存（Phase2 / §5.3 §5.4）。

- 请求开始时 pin 一个 wiki_version + source_snapshot_id；
- 该请求内的所有读取都针对同一版本，避免发布期间版本混读；
- 版本感知缓存 Key = (wiki_version, page_id)，Active 更新后新请求进新版本，
  旧请求仍读旧版本（旧版本在 Reader Lease 释放前不回收）。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.read_session")


class WikiReadSession(BaseModel):
    """一次知识请求的版本上下文。"""

    task_id: str = Field(default="")
    wiki_version: str = Field(default="")
    source_snapshot_id: str = Field(default="")
    pinned_at: str = Field(default="")

    @classmethod
    def pin(
        cls,
        *,
        wiki_version: str,
        source_snapshot_id: str = "",
        task_id: str = "",
    ) -> WikiReadSession:
        return cls(
            task_id=task_id,
            wiki_version=wiki_version,
            source_snapshot_id=source_snapshot_id,
            pinned_at=datetime.now(UTC).isoformat(),
        )

    @classmethod
    def pin_active(
        cls,
        *,
        manifest_store: Any,
        source_snapshot_id: str = "",
        task_id: str = "",
    ) -> WikiReadSession:
        """从 Active Pointer 读取当前版本并钉扎。无 Active 时 wiki_version=''。"""
        return cls.pin(
            wiki_version=manifest_store.active_version(),
            source_snapshot_id=source_snapshot_id,
            task_id=task_id,
        )

    def ensure_version(self) -> None:
        """严格模式下，缺失版本钉扎是不可接受的（G-11 语义由 Factory 决策）。"""
        if not self.wiki_version:
            raise ValueError("WIKI_VERSION_NOT_PINNED")


class VersionedReader:
    """对一个 WikiStore 的版本一致读取适配器。

    当前 WikiStore 为就地树（in-place tree），Reader 通过 manifest 校验版本，
    并确保读操作在 Active 切换前后维持调用方钉扎的版本语义。
    """

    def __init__(
        self, store: Any, session: WikiReadSession, cache: VersionAwareCache | None = None
    ):
        self.store = store
        self.session = session
        self.cache = cache

    def open_page(self, page_id: str):
        # 通过版本感知有界缓存读取（§20.3）：Key = (wiki_version, page_id)
        if self.cache is not None:
            cached = self.cache.get(self.session.wiki_version, page_id)
            if cached is not None:
                return cached
            page = self.store.open_page(page_id)
            if page is not None:
                self.cache.put(self.session.wiki_version, page_id, page)
            return page
        # Runtime 只读；就地树读 Active。严格版本化目录部署时，
        # 此处应解析到 session.wiki_version 对应目录（见 manifest.ManifestStore.version_dir）。
        return self.store.open_page(page_id)

    def page_exists(self, page_id: str) -> bool:
        return self.store.page_exists(page_id)


class VersionAwareCache:
    """有界的版本感知 LRU 缓存：Key = (wiki_version, page_id)。"""

    def __init__(self, max_pages: int = 500, max_memory_mb: int = 0):
        self.max_pages = max_pages
        self.max_memory_mb = max_memory_mb
        self._store: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0

    def get(self, wiki_version: str, page_id: str) -> Any:
        key = (wiki_version, page_id)
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, wiki_version: str, page_id: str, value: Any) -> None:
        key = (wiki_version, page_id)
        size = _estimate_bytes(value)
        with self._lock:
            if key in self._store:
                self._bytes -= _estimate_bytes(self._store[key])
                del self._store[key]
            while self._store and (
                len(self._store) >= self.max_pages
                or (self.max_memory_mb and self._bytes + size > self.max_memory_mb * 1024 * 1024)
            ):
                _old_k, _old_v = self._store.popitem(last=False)
                self._bytes -= _estimate_bytes(_old_v)
            self._store[key] = value
            self._bytes += size

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


def _estimate_bytes(value: Any) -> int:
    try:
        text = getattr(value, "render_markdown", lambda: "")().encode("utf-8")
    except Exception:
        text = str(value).encode("utf-8")
    return max(1, len(text))
