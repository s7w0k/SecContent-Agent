"""Navigation Tools - Wiki Navigator 暴露给 Agent 的确定性工具集。

文档 14.2：
  resolve_entity / list_pages / get_page_summary / open_page /
  open_section / list_links / follow_link / search_pages / read_source

关键约束：
  - search_pages 只返回 `page_id / title / page_type / summary`，
    不直接返回一大段正文；Agent 必须再调用 open_page 才读页面。
"""

from __future__ import annotations

from agent.wiki.index import WikiIndex
from agent.wiki.store import WikiStore


class NavigationTools:
    """确定性 Wiki 导航工具。全部只读（Runtime Plane）。"""

    def __init__(self, store: WikiStore, index: WikiIndex | None = None):
        self.store = store
        self.index = index

    # ── 实体 / 页表 ──────────────────────────────────────

    def resolve_entity(self, name: str) -> list[dict]:
        if self.index is None:
            return [
                {"page_id": pid}
                for pid in self.store.list_page_ids()
                if name and name.lower() in pid.lower()
            ]
        return [
            {"page_id": p.page_id, "title": p.title, "page_type": p.page_type}
            for p in self.index.resolve(name)
        ]

    def list_pages(
        self,
        product_id: str | None = None,
        page_type: str | None = None,
        task_affinity: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        if self.index is None:
            metas = self.store.list_pages()
        else:
            entries = self.index.list_pages(product_id, page_type, task_affinity, limit)
            metas = [
                {"page_id": e.page_id, "title": e.title, "page_type": e.page_type} for e in entries
            ]
        if self.index is None:
            metas = [
                {"page_id": m.page_id, "title": m.title, "page_type": m.page_type} for m in metas
            ]
        return metas

    def search_pages(
        self, keyword: str, product_id: str | None = None, page_type: str | None = None
    ) -> list[dict]:
        """只返回摘要卡片，不返回正文。"""
        if self.index is None:
            return self._fallback_search(keyword, product_id, page_type)
        results = self.index.search(keyword, product_id, page_type)
        cards = []
        for p in results:
            cards.append(
                {
                    "page_id": p.page_id,
                    "title": p.title,
                    "page_type": p.page_type,
                    "product_id": p.product_id,
                    "summary": p.summary,
                }
            )
        return cards

    def _fallback_search(
        self, keyword: str, product_id: str | None, page_type: str | None
    ) -> list[dict]:
        cards = []
        kw = keyword.lower()
        page_ids = self.store.list_page_ids()
        for pid in page_ids:
            if kw and kw not in pid.lower():
                continue
            try:
                meta = self.store.open_page_meta(pid)
            except Exception:
                continue
            if product_id and meta.product_id != product_id:
                continue
            if page_type and meta.page_type != page_type:
                continue
            cards.append(
                {
                    "page_id": pid,
                    "title": meta.title,
                    "page_type": meta.page_type,
                    "product_id": meta.product_id,
                    "summary": self.store.open_page(pid).summary(),
                }
            )
        return cards

    # ── 读取页面 ─────────────────────────────────────────

    def get_page_summary(self, page_id: str) -> dict:
        page = self.store.open_page(page_id)
        return {
            "page_id": page_id,
            "title": page.meta.title,
            "page_type": page.meta.page_type,
            "product_id": page.meta.product_id,
            "summary": page.summary(),
        }

    def open_page(self, page_id: str) -> dict:
        page = self.store.open_page(page_id)
        sections_md = []
        for sec in page.sections:
            sections_md.append({"title": sec.title, "body": sec.body[:2000]})
        return {
            "page_id": page_id,
            "title": page.meta.title,
            "page_type": page.meta.page_type,
            "product_id": page.meta.product_id,
            "summary": page.summary(),
            "body": page.body[:4000],
            "sections": sections_md,
        }

    def open_section(self, page_id: str, section: str) -> dict:
        page = self.store.open_page(page_id)
        lower = section.lower()
        for sec in page.sections:
            if lower in sec.title.lower() or lower == sec.title.lower():
                return {"page_id": page_id, "section": sec.title, "body": sec.body[:3000]}
        if page.meta.title.lower() == lower:
            return {"page_id": page_id, "section": "body", "body": page.body[:3000]}
        return {"page_id": page_id, "section": section, "body": ""}

    # ── 链接 ─────────────────────────────────────────────

    def list_links(self, page_id: str) -> list[dict]:
        page = self.store.open_page(page_id)
        return [
            {"relation_type": r.relation_type, "target_page_id": r.target_page_id}
            for r in page.meta.relations
        ]

    def follow_link(self, page_id: str, relation_type: str | None = None) -> list[dict]:
        page = self.store.open_page(page_id)
        targets = []
        for r in page.meta.relations:
            if relation_type and r.relation_type != relation_type:
                continue
            targets.append(r.target_page_id)
        return [
            {"page_id": t, "title": self._title(t), "relation_type": relation_type or ""}
            for t in targets
            if t != page_id
        ]

    def read_source(self, source_ref: dict) -> dict:
        """读取 Raw Source 对应的正文（需 Source Registry 提供真实路径）。"""
        relative_path = source_ref.get("relative_path", "")
        from agent.wiki.contracts import is_path_safe

        if not is_path_safe(relative_path):
            return {"error": "UNSAFE_PATH"}
        return {"path": relative_path, "read_only": True}

    def _title(self, page_id: str) -> str:
        try:
            return self.store.open_page_meta(page_id).title
        except Exception:
            return page_id
