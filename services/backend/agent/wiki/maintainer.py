"""Wiki Maintainer - 增量维护（PR-11 / 文档 §21 §22）。

流水线：
  detect source changes
  → calculate impacted pages
  → regenerate impacted pages
  → repair links
  → lint
  → stage
  → publish

设计约束：
  - 不做"任意文件变化 → 全量重建"
  - 通过 SourceRegistry 的增量 diff + WikiIndex 的 source_id→page_ids 反向映射，
    只重建受影响页面
  - 规则确定性回退：无 LLM 也能运行
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.wiki.compiler import PageCompiler, SourceSection, build_wiki_page
from agent.wiki.source_registry import DiffReport, stable_source_id

logger = logging.getLogger("backend.agent.wiki.maintainer")


@dataclass
class MaintenanceReport:
    """一次维护的分析结果。"""

    new_sources: list[str] = field(default_factory=list)
    changed_sources: list[str] = field(default_factory=list)
    deleted_sources: list[str] = field(default_factory=list)
    impacted_page_ids: list[str] = field(default_factory=list)

    @property
    def needs_action(self) -> bool:
        return bool(self.new_sources or self.changed_sources)


class WikiMaintainer:
    """Wiki 增量维护器。"""

    def __init__(
        self,
        store: Any,
        registry: Any,
        index: Any | None = None,
        compiler: PageCompiler | None = None,
        publisher: Any | None = None,
        raw_reader: Any | None = None,
        *,
        compiler_version: str = "deterministic-1",
        schema_version: int = 1,
    ):
        self.store = store
        self.registry = registry
        self.index = index
        self.compiler = compiler or PageCompiler()
        self.publisher = publisher
        self._raw_reader = raw_reader or _read_text
        self.compiler_version = compiler_version
        self.schema_version = schema_version

    # ── 分析 ──────────────────────────────────────────────

    def detect_changes(self) -> DiffReport:
        return self.registry.diff()

    def impacted_pages(self, diff: DiffReport) -> list[str]:
        """计算受影响页面：引用任一新增/变更源文件的页面。"""
        affected_source_ids = {stable_source_id(rel) for rel in diff.new + diff.changed}
        impacted: set[str] = set()
        # 从 index manifest 反向映射 source_id → page_ids
        if self.index is not None and self.index.manifest is not None:
            for page in self.index.manifest.pages:
                if set(page.source_ids) & affected_source_ids:
                    impacted.add(page.page_id)
        else:
            impacted.update(self._scan_source_references(affected_source_ids))
        return sorted(impacted)

    def analyze(self) -> MaintenanceReport:
        diff = self.detect_changes()
        return MaintenanceReport(
            new_sources=diff.new,
            changed_sources=diff.changed,
            deleted_sources=diff.deleted,
            impacted_page_ids=self.impacted_pages(diff),
        )

    # ── 重建 ──────────────────────────────────────────────

    def regenerable_page_ids(self, report: MaintenanceReport) -> list[str]:
        """返回真正需要重建的页面（新增/变更源影响的页面）。"""
        return report.impacted_page_ids if report.needs_action else []

    async def regenerate_pages(self, page_ids: list[str], status: str = "staged") -> list[str]:
        """按页面重建并写回 store（staged）。返回成功重建的 page_id。"""
        rebuilt: list[str] = []
        for page_id in page_ids:
            try:
                page = await self._rebuild_one(page_id, status)
            except Exception as exc:
                logger.warning("重建页面失败 %s: %s", page_id, exc)
                continue
            self.store.write_page(page)
            rebuilt.append(page_id)
        return rebuilt

    async def _rebuild_one(self, page_id: str, status: str):
        page = self.store.open_page(page_id)
        meta = page.meta
        # 收集该页引用的源章节
        sections: list[SourceSection] = []
        seen: set[tuple] = set()
        for ref in meta.source_refs:
            entry = self.registry.get(ref.source_id)
            if entry is None:
                continue
            text = self._raw_reader(self.registry.root / entry.relative_path)
            key = (entry.source_id, ref.section_id)
            if key in seen:
                continue
            seen.add(key)
            sections.append(SourceSection(ref=ref, text=text))
        draft = await self.compiler.compile_async(
            page_id=page_id,
            page_type=meta.page_type,
            title=meta.title,
            product_id=meta.product_id,
            source_sections=sections,
        )
        return build_wiki_page(draft, self.registry, status=status)

    # ── 编排 ──────────────────────────────────────────────

    async def run(self) -> dict:
        """完整流程：detect → impacted → regenerate → repair(rebuild index) → lint → publish。

        若提供了 publisher，则 lint + publish 由它完成；否则只做重建与索引重建。
        """
        report = self.analyze()
        page_ids = self.regenerable_page_ids(report)
        rebuilt = await self.regenerate_pages(page_ids)
        if report.needs_action:
            self._rebuild_index()
        outcome: dict = {"report": report, "rebuilt_page_ids": rebuilt}
        if self.publisher is not None:
            result = self.publisher.publish()
            outcome["published"] = result.ok
            outcome["wiki_version"] = result.wiki_version
            outcome["publish_errors"] = result.errors
        return outcome

    # ── 事务式构建（Phase1，G-01）─────────────────────────────

    async def run_transaction(
        self,
        *,
        transaction_dir: Any | None = None,
        auto_publish: bool = True,
    ) -> dict:
        """事务式全流程：pending 快照 → compile → lint/gate → publish → 成功后 commit registry。

        关键修复（G-01）：注册表 **不会** 在 Compile 前持久化；
        只有 Publish 成功后，pending 快照才被 commit 为 active。
        因此 Compile/Lint/Publish 失败后，下一轮仍能检测同一 Source 变更。
        """
        from agent.wiki.transaction import KnowledgeBuildTransaction, TransactionStore

        pending = self.registry.snapshot_pending()
        diff = self.registry.snapshot_diff(pending)
        report = MaintenanceReport(
            new_sources=diff.new,
            changed_sources=diff.changed,
            deleted_sources=diff.deleted,
            impacted_page_ids=self._impacted_from_snapshot(pending, diff),
        )

        meta_dir = getattr(self.store, "root", None)
        default_tx_dir = meta_dir / "_meta" / "transactions" if meta_dir is not None else None
        tx_store = TransactionStore(transaction_dir or default_tx_dir)

        current_version = self.index.wiki_version if self.index is not None else ""
        tx = KnowledgeBuildTransaction.begin(
            snapshot=pending,
            parent_wiki_version=current_version,
            compiler_version=self.compiler_version,
            schema_version=self.schema_version,
        )
        # 幂等：同一 build_id 的 COMMITTED 事务跳过，避免重复发布
        existing = tx_store.load(tx.transaction_id)
        if existing is not None and existing.state == "COMMITTED":
            return {
                "transaction_id": tx.transaction_id,
                "build_id": tx.build_id,
                "state": "COMMITTED",
                "replayed": True,
                "rebuilt_page_ids": [],
                "published": True,
                "wiki_version": existing.wiki_version,
                "publish_errors": [],
            }
        tx_store.save(tx)

        try:
            tx.transition("COMPILING")
            tx_store.save(tx)
            page_ids = report.impacted_page_ids if report.needs_action else []
            rebuilt = await self.regenerate_pages(
                page_ids, status="staged" if auto_publish else "draft"
            )
            if report.needs_action:
                self._rebuild_index()

            published = False
            errors: list[str] = []
            if auto_publish and self.publisher is not None:
                tx.transition("PUBLISHING")
                tx_store.save(tx)
                result = self.publisher.publish()
                published = result.ok
                errors = result.errors
                if not published:
                    tx.transition("FAILED", reason="; ".join(errors) or "publish failed")
                else:
                    tx.transition("PUBLISHED")
                    tx.record(wiki_version=result.wiki_version)
                    tx_store.save(tx)
            elif not auto_publish:
                tx.transition("COMPILED")

            if published:
                # 唯一允许更新 active Registry 的时点（G-01 commit 点）
                self.registry.commit_snapshot(pending)
                tx.transition("COMMITTED")
                tx_store.save(tx)

            return {
                "transaction_id": tx.transaction_id,
                "build_id": tx.build_id,
                "state": tx.state,
                "replayed": False,
                "pending_snapshot_id": pending.snapshot_id,
                "source_snapshot_hash": pending.snapshot_hash,
                "rebuilt_page_ids": rebuilt,
                "published": published,
                "wiki_version": tx.wiki_version,
                "publish_errors": errors,
            }
        except Exception as exc:
            logger.exception("事务构建失败")
            try:
                tx.transition("FAILED", reason=str(exc))
                tx_store.save(tx)
            except Exception:
                pass
            return {
                "transaction_id": tx.transaction_id,
                "build_id": tx.build_id,
                "state": "FAILED",
                "replayed": False,
                "rebuilt_page_ids": [],
                "published": False,
                "wiki_version": tx.wiki_version,
                "publish_errors": [str(exc)],
                "failure_reason": str(exc),
            }

    def _impacted_from_snapshot(self, pending, diff) -> list[str]:
        """基于 pending 快照的受影响源，用 index 反向映射页面的简化版。"""
        from agent.wiki.source_registry import stable_source_id

        affected = {stable_source_id(rel) for rel in diff.new + diff.changed}
        impacted: set[str] = set()
        if self.index is not None and self.index.manifest is not None:
            for page in self.index.manifest.pages:
                if set(page.source_ids) & affected:
                    impacted.add(page.page_id)
        else:
            impacted.update(self._scan_source_references(affected))
        return sorted(impacted)

    # ── 内部工具 ──────────────────────────────────────────

    def _scan_source_references(self, source_ids: set[str]) -> list[str]:
        """无 index 时，遍历 store 页面找引用受影响源的页面。"""
        impacted: set[str] = set()
        for page_id in self.store.list_page_ids():
            try:
                meta = self.store.open_page_meta(page_id)
            except Exception:
                continue
            if any(r.source_id in source_ids for r in meta.source_refs):
                impacted.add(page_id)
        return sorted(impacted)

    def _rebuild_index(self) -> None:
        if self.index is None:
            return
        from agent.wiki.index import WikiIndexStore, build_manifest

        meta_dir = getattr(self.store, "root", None)
        if meta_dir is None:
            return
        index_store = WikiIndexStore(meta_dir / "_meta")
        manifest = build_manifest(self.store)
        index_store.write(manifest)
        self.index.load(manifest)


def _read_text(path: Any) -> str:
    if not hasattr(path, "read_text"):
        path = __import__("pathlib").Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk")
