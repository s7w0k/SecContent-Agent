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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.wiki.contracts import STATUS_PUBLISHED, STATUS_STAGED
from agent.wiki.index import WikiIndexStore, build_manifest
from agent.wiki.locks import PublishLock
from agent.wiki.manifest import ManifestStore, WikiVersionManifest, build_version_id
from agent.wiki.store import WikiStore

logger = logging.getLogger("backend.agent.wiki.publisher")

BUILD_MANIFEST_FILENAME = "build-manifest.json"
PUBLISH_LOCK_FILENAME = ".publish.lock"


@dataclass
class PublicationResult:
    """发布结果。"""

    ok: bool = True
    wiki_version: str = ""
    version_id: str = ""
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
        *,
        compiler_version: str = "deterministic-1",
        schema_version: int = 2,
        lock_ttl_seconds: float = 120.0,
        source_snapshot: Any | None = None,
        parent_wiki_version: str = "",
        strict_lint: bool = True,
    ):
        self.store = store
        self.linter = linter
        self.meta_dir = Path(meta_dir) if meta_dir else store.root / "_meta"
        self.index_store = WikiIndexStore(self.meta_dir)
        self.source_registry = source_registry
        self.gates = gates or []
        self.require_grounding = require_grounding
        self.compiler_version = compiler_version
        self.schema_version = schema_version
        self.source_snapshot = source_snapshot
        self.parent_wiki_version = parent_wiki_version
        self.strict_lint = strict_lint
        self._lock = PublishLock(self._lock_path(), ttl_seconds=lock_ttl_seconds)
        self.manifest_store = ManifestStore(self.meta_dir / "versions")

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
        return self._lock.acquire()

    def _release_lock(self) -> None:
        self._lock.release()

    # ── 主发布流程 ────────────────────────────────────────

    def publish(self) -> PublicationResult:
        result = PublicationResult()

        # 1. Linter（Production Gate：lint_errors == 0，§1269 Phase15）
        lint = self.linter.lint()
        if not lint.ok or (self.strict_lint and lint.errors):
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

            # 5. 构建并发布索引 + build-manifest + 可选版本 manifest
            manifest = build_manifest(self.store)
            self.index_store.write(manifest)
            self._write_build_manifest(manifest.wiki_version, page_metas)

            version_id = build_version_id(parent_version=self.parent_wiki_version)
            self._publish_version_manifest(manifest, version_id)

            result.wiki_version = manifest.wiki_version
            result.version_id = version_id
            result.pages_published = published or len(page_metas)
            result.ok = True
            logger.info("Wiki 发布完成: version=%s pages=%d", version_id, len(page_metas))
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

    def _publish_version_manifest(self, manifest: Any, version_id: str) -> None:
        """写入不可变版本 Manifest 并切换 Active Pointer（G-10/G-11）。"""
        vm = WikiVersionManifest(
            wiki_version=version_id,
            schema_version=self.schema_version,
            compiler_version=self.compiler_version,
            source_snapshot_id=getattr(self.source_snapshot, "snapshot_id", ""),
            source_snapshot_hash=getattr(self.source_snapshot, "snapshot_hash", ""),
            page_tree_hash=manifest.wiki_version,
            page_count=manifest.page_count,
            created_at=datetime.now(UTC).isoformat(),
            build_id="",
            gate_report_hash="",
            parent_version=self.parent_wiki_version,
            status="published",
        )
        self.manifest_store.write(vm)
        self.manifest_store.set_active(version_id)

    # ── 回滚（§5.6 Rollback）────────────────────────────

    def rollback(
        self,
        version_id: str,
        *,
        registry: Any | None = None,
        snapshot: Any | None = None,
    ) -> dict:
        """回滚到指定已发布版本。

        - 先校验 Manifest 存在、schema 可支持、tree hash 非空；
        - 切换 Active Wiki Version Pointer；
        - 若提供 registry + SourceSnapshot，则同时回滚 Active Registry Snapshot，
          保证 Wiki + Registry 一致（§5.6 / §2.4 RelDoD）。
        真正的版本化页面树恢复由 versioned store 负责（本接口为编排点）。
        """
        m = self.manifest_store.load(version_id)
        errors = []
        snap = m
        if snap is not None:
            errors = m.validate_self()
            if self.schema_version_is_unsupported(m.schema_version):
                errors.append("UNSUPPORTED_SCHEMA")
        else:
            errors.append("MANIFEST_NOT_FOUND")
        if errors:
            return {"ok": False, "active_version": "", "errors": errors}

        self.manifest_store.set_active(version_id)
        registry_ok = False
        if registry is not None and snapshot is not None:
            registry.commit_snapshot(snapshot)
            registry_ok = True
        return {
            "ok": True,
            "active_version": version_id,
            "registry_rolled_back": registry_ok,
            "errors": [],
        }

    @staticmethod
    def schema_version_is_unsupported(schema_version: int) -> bool:
        return schema_version < 1 or schema_version > 2
