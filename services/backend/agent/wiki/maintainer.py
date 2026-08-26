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
    ):
        self.store = store
        self.registry = registry
        self.index = index
        self.compiler = compiler or PageCompiler()
        self.publisher = publisher
        self._raw_reader = raw_reader or _read_text

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
