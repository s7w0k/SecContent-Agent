"""Wiki Linter - 编译后发布前的静态校验。

PR-03 第四步，至少检查：
  schema_valid / source_ref_valid / broken_link / duplicate_page_id /
  orphan_page / empty_page / ungrounded_claim / stale_source / conflict

只有 LintResult 通过（无致命错误）才允许进入发布。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.wiki.contracts import WikiPageMeta
from agent.wiki.store import WikiStore, extract_frontmatter, meta_from_frontmatter

logger = logging.getLogger("backend.agent.wiki.linter")

FATAL_CODES = frozenset({"schema_valid", "source_ref_valid", "broken_link", "duplicate_page_id"})


@dataclass
class LintResult:
    """Lint 结果。ok = 无致命错误。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(_is_fatal(e) for e in self.errors)

    def __bool__(self) -> bool:
        return self.ok

    def add(self, error: str) -> None:
        self.errors.append(error)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def __str__(self) -> str:
        if self.ok and not self.errors:
            return f"LINT PASS ({len(self.warnings)} warnings)"
        return f"LINT FAIL: {len(self.errors)} errors, {len(self.warnings)} warnings"


def _is_fatal(error: str) -> bool:
    return any(error.startswith(code) or error.startswith(code + "[") for code in FATAL_CODES)


class WikiLinter:
    """Wiki Linter。registry 可选：提供则为 stale_source 校验提供源哈希。"""

    def __init__(self, store: WikiStore, registry: Any | None = None):
        self.store = store
        self.registry = registry

    # ── 主入口 ────────────────────────────────────────────

    def lint(self, page_ids: list[str] | None = None) -> LintResult:
        result = LintResult()
        target_ids = page_ids or self.store.list_page_ids()
        if not target_ids:
            return result

        self._lint_duplicates(result)

        # 建立外链映射用于 orphan / broken 检测
        outbound: dict[str, set[str]] = {}
        page_types: dict[str, str] = {}

        for page_id in target_ids:
            page = self._open(page_id)
            if page is None:
                result.add(f"schema_valid[{page_id}] 页面无法解析")
                continue

            page_types[page_id] = page.meta.page_type
            outbound[page_id] = {r.target_page_id for r in page.meta.relations}
            self._lint_page(result, page)

        self._lint_orphans(result, target_ids, outbound, page_types)

        if self.registry is None:
            pass
        return result

    # ── 页面级检查 ────────────────────────────────────────

    def _open(self, page_id: str):
        try:
            return self.store.open_page(page_id)
        except Exception as exc:
            logger.debug("open_page failed %s: %s", page_id, exc)
            return None

    def _lint_page(self, result: LintResult, page) -> None:
        page_id = page.meta.page_id

        # empty_page
        if not page.body.strip() and not page.sections:
            result.add(f"empty_page[{page_id}]")

        # source_ref_valid
        for ref in page.meta.source_refs:
            for err in self.store.check_source_ref(ref):
                result.add(f"source_ref_valid[{page_id}] {err}")
            self._lint_stale_source(result, page_id, ref)

        # broken_link
        for rel in page.meta.relations:
            if not self.store.page_exists(rel.target_page_id):
                result.add(f"broken_link[{page_id}] 目标不存在: {rel.target_page_id}")

        # ungrounded_claim（fact 类页面必须有 source_refs）
        if (
            page.meta.page_type in {"capability", "limitation", "scenario"}
            and not page.meta.source_refs
            and not _page_mentions_source(page)
        ):
            result.add(f"ungrounded_claim[{page_id}]")

    def _lint_stale_source(self, result: LintResult, page_id: str, ref) -> None:
        if self.registry is None:
            return
        entry = self.registry.get(ref.source_id)
        if entry is None:
            result.add(f"stale_source[{page_id}] 源不存在: {ref.source_id}")
        elif entry.sha256 != ref.content_hash:
            result.add(f"stale_source[{page_id}] 源哈希变化: {ref.relative_path}")

    # ── 重复 page_id ──────────────────────────────────────

    def _lint_duplicates(self, result: LintResult) -> None:
        counts: dict[str, list[str]] = {}
        if not self.store.root.is_dir():
            return
        for fp in self.store.root.rglob("*.md"):
            if "_meta" in fp.parts:
                continue
            meta = self._read_page_meta(fp)
            if meta is None:
                continue
            counts.setdefault(meta.page_id, []).append(str(fp))
        for page_id, paths in counts.items():
            if len(paths) > 1:
                result.add(f"duplicate_page_id[{page_id}] {len(paths)} 个文件: {paths}")

    def _read_page_meta(self, path: Path) -> WikiPageMeta | None:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gbk")
        fm_text, _ = extract_frontmatter(text)
        if not fm_text:
            return None
        try:
            return meta_from_frontmatter(fm_text)
        except Exception:
            return None

    # ── orphan ────────────────────────────────────────────

    def _lint_orphans(
        self,
        result: LintResult,
        target_ids: list[str],
        outbound: dict[str, set[str]],
        page_types: dict[str, str],
    ) -> None:
        inbound: dict[str, int] = {}
        for targets in outbound.values():
            for t in targets:
                inbound[t] = inbound.get(t, 0) + 1
        for page_id in target_ids:
            if page_types.get(page_id) in {"product", "concept", "competitor", "synthesis"}:
                continue
            if inbound.get(page_id, 0) == 0:
                result.warn(f"orphan_page[{page_id}] 无入链")


def _page_mentions_source(page) -> bool:
    """页面是否至少在证据章节透露真实来源（[来源: ...] 标记）。

    仅标题叫 Source 但无来源标记的页面不能算作 grounded（无 provenance）。
    """
    for sec in page.sections:
        if (
            ("来源" in sec.title or "Source" in sec.title)
            and "[来源:" in sec.body
            and "未 grounding" not in sec.body
        ):
            return True
    return False
