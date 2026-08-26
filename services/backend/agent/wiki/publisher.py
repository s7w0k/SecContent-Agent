"""Wiki Publisher - 把通过校验的 Wiki Staging 发布为正式版本。

PR-04 产物：
  - Linter 通过 → Grounding Gate → Conflict Gate → Lock → 原子发布 → 构建 wiki-index
  - 复用知识发布体系的“锁 + 原子写”思想（见 knowledge_admin/publication.py）
  - 产出新 wiki_version
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.wiki.contracts import STATUS_PUBLISHED, STATUS_STAGED
from agent.wiki.index import WikiIndexStore, build_manifest
from agent.wiki.store import WikiStore

logger = logging.getLogger("backend.agent.wiki.publisher")

BUILD_MANIFEST_FILENAME = "build-manifest.json"
PUBLISH_LOCK_FILENAME = ".publish.lock"


@dataclass
class PublicationResult:
    """发布结果。"""

    ok: bool = True
    wiki_version: str = ""
    pages_published: int = 0
    errors: list[str] = field(default_factory=list)


# 门卫签名：接收一个 WikiPageMeta，返回错误列表（空=通过）
Gate = Callable[[Any], list[str]]


class WikiPublisher:
    """Wiki 发布器。在 Wiki Root 内就地发布（staging 与发布共用同一树，通过状态区分）。

    gates: 逐页门卫，如 grounding gate / conflict gate。
    """

    def __init__(
        self,
        store: WikiStore,
        linter: Any,
        meta_dir: str | Path | None = None,
        source_registry: Any | None = None,
        gates: list[Gate] | None = None,
        require_grounding: bool = True,
    ):
        self.store = store
        self.linter = linter
        self.meta_dir = Path(meta_dir) if meta_dir else store.root / "_meta"
        self.index_store = WikiIndexStore(self.meta_dir)
        self.source_registry = source_registry
        self.gates = gates or []
        self.require_grounding = require_grounding

    # ── 门卫缺省 ──────────────────────────────────────────

    def _grounding_gate(self, meta) -> list[str]:
        if not self.require_grounding:
            return []
        if meta.page_type in {"capability", "limitation", "scenario"} and not meta.source_refs:
            return [f"grounding[{meta.page_id}] 事实页缺少 source_refs"]
        return []

    def _lock_path(self) -> Path:
        return self.meta_dir / PUBLISH_LOCK_FILENAME

    def _acquire_lock(self) -> bool:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        lock = self._lock_path()
        if lock.exists():
            return False
        lock.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
        return True

    def _release_lock(self) -> None:
        with suppress(FileNotFoundError):
            self._lock_path().unlink()

    # ── 主发布流程 ────────────────────────────────────────

    def publish(self) -> PublicationResult:
        result = PublicationResult()

        # 1. Linter
        lint = self.linter.lint()
        if not lint.ok:
            result.ok = False
            result.errors.extend(lint.errors)
            return result

        # 2. Gates（逐页）
        page_metas = []
        for page_id in self.store.list_page_ids():
            try:
                meta = self.store.open_page_meta(page_id)
            except Exception:
                continue
            errors = self._grounding_gate(meta)
            for gate in self.gates:
                gate_errors = gate(meta)
                if gate_errors:
                    errors.extend(gate_errors)
            if errors:
                result.ok = False
                result.errors.extend(errors)
                return result
            page_metas.append(meta)

        # 3. Lock
        if not self._acquire_lock():
            result.ok = False
            result.errors.append("publish_lock 已被占用")
            return result

        try:
            # 4. 将 staged 页面标记为 published
            published = 0
            for meta in page_metas:
                if meta.status == STATUS_STAGED:
                    try:
                        page = self.store.open_page(meta.page_id)
                        new_meta = page.meta.model_copy(
                            update={
                                "status": STATUS_PUBLISHED,
                                "updated_at": datetime.now(UTC).isoformat(),
                            }
                        )
                        from agent.wiki.contracts import WikiPage

                        updated = WikiPage(meta=new_meta, body=page.body, sections=page.sections)
                        self.store.write_page(updated)
                        published += 1
                    except Exception as exc:
                        result.ok = False
                        result.errors.append(f"publish[{meta.page_id}] {exc}")
                        return result

            # 5. 构建并发布索引 + build-manifest
            manifest = build_manifest(self.store)
            self.index_store.write(manifest)
            self._write_build_manifest(manifest.wiki_version, page_metas)

            result.wiki_version = manifest.wiki_version
            result.pages_published = published or len(page_metas)
            result.ok = True
            logger.info(
                "Wiki 发布完成: version=%s pages=%d", manifest.wiki_version, len(page_metas)
            )
            return result
        finally:
            self._release_lock()

    def _write_build_manifest(self, wiki_version: str, page_metas: list[Any]) -> None:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "wiki_version": wiki_version,
            "built_at": datetime.now(UTC).isoformat(),
            "page_count": len(page_metas),
            "status": "published",
        }
        target = self.meta_dir / BUILD_MANIFEST_FILENAME
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
