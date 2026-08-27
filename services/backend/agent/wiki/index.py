"""Wiki Index - 以 Wiki 页面为粒度的导航索引。

PR-04 产物：
  - 保留 JSON Index 工程方式，但索引单位是 WikiPageIndex（非 DocIndex）
  - wiki_version = hash( sorted(page_id + content_hash) )
  - 这是 Navigation Index，不是 Top-K Chunk Retrieval Engine
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from agent.wiki.store import WikiStore
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.wiki.index")

SCHEMA_VERSION = 1
DEFAULT_INDEX_FILENAME = "wiki-index.json"


class WikiPageIndex(BaseModel):
    """单个页面在 Wiki 导航索引中的条目。"""

    page_id: str
    title: str
    page_type: str
    product_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    task_affinity: list[str] = Field(default_factory=list)
    summary: str = Field(default="")
    relations: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    search_terms: list[str] = Field(default_factory=list)


class WikiIndexManifest(BaseModel):
    """Wiki 索引清单。"""

    schema_version: int = Field(default=SCHEMA_VERSION)
    wiki_version: str = Field(description="Wiki 内容版本哈希（确定性）")
    built_at: str = Field(default="")
    page_count: int = Field(default=0, ge=0)
    pages: list[WikiPageIndex] = Field(default_factory=list)


def compute_wiki_version(pages: list[WikiPageIndex]) -> str:
    """wiki_version = hash(sorted(page_id + content_hash))。"""
    payload = sorted(f"{p.page_id};{p.content_hash}" for p in pages if p.content_hash)
    blob = "\n".join(payload)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_manifest(store: WikiStore, built_at: str = "") -> WikiIndexManifest:
    """从 WikiStore 扫描所有页面，构建 WikiIndexManifest。"""
    from datetime import UTC, datetime

    if not built_at:
        built_at = datetime.now(UTC).isoformat()
    page_ids = store.list_page_ids()
    entries: list[WikiPageIndex] = []

    for page_id in page_ids:
        try:
            page = store.open_page(page_id)
        except Exception:
            continue
        meta = page.meta
        content_hash = meta.content_hash or _page_content_hash(page)
        entries.append(
            WikiPageIndex(
                page_id=meta.page_id,
                title=meta.title,
                page_type=meta.page_type,
                product_id=meta.product_id,
                aliases=meta.aliases,
                task_affinity=meta.task_affinity,
                summary=page.summary(300),
                relations=[f"{r.relation_type}->{r.target_page_id}" for r in meta.relations],
                source_ids=[r.source_id for r in meta.source_refs],
                content_hash=content_hash,
                search_terms=build_search_terms(meta.title, meta.aliases, page.summary(300)),
            )
        )

    entries.sort(key=lambda e: e.page_id)
    return WikiIndexManifest(
        wiki_version=compute_wiki_version(entries),
        built_at=built_at,
        page_count=len(entries),
        pages=entries,
    )


def _page_content_hash(page) -> str:
    blob = page.render_markdown()
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_search_terms(
    title: str,
    aliases: list[str],
    summary: str = "",
    ngram: int = 2,
) -> list[str]:
    """为页面构建检索词（含中文预处理，§9.4）。

    中文 FTS unicode61 对自然语言粒度不足，这里预生成 search_terms：
      - 英文/数字词条直接保留小写形式
      - 连续 CJK 文本按 ngram 切分为 w={2} 窗口（覆盖"单点登录"→"单点/点登/登录"）
      - 同时保留整段 CJK 短语与标题/别名
    仅用于 `search_pages` 召回，不参与 wiki_version 计算，避免影响版本确定性。
    """
    terms: set[str] = set()
    sources = [title, *aliases, summary]
    for raw in sources:
        text = (raw or "").strip().lower()
        if not text:
            continue
        # 拉丁/数字词条按空白拆分
        for part in text.split():
            if _is_cjk(part):
                terms.add(part)
                _add_ngrams(terms, part, ngram)
            else:
                terms.add(part)
    # 合并后的排序列表
    return sorted(t for t in terms if t)


def _is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _add_ngrams(terms: set[str], text: str, n: int) -> None:
    if len(text) < n:
        terms.add(text)
        return
    for i in range(len(text) - n + 1):
        terms.add(text[i : i + n])


class WikiIndex:
    """运行时加载并查询 Wiki 索引（实体/别名/页面解析）。"""

    def __init__(self, manifest: WikiIndexManifest | None = None):
        self._manifest = manifest
        self._by_id: dict[str, WikiPageIndex] = {}
        self._by_alias: dict[str, WikiPageIndex] = {}
        if manifest:
            self._reindex(manifest)

    @property
    def wiki_version(self) -> str:
        return self._manifest.wiki_version if self._manifest else ""

    @property
    def manifest(self) -> WikiIndexManifest | None:
        return self._manifest

    def _reindex(self, manifest: WikiIndexManifest) -> None:
        self._by_id = {p.page_id: p for p in manifest.pages}
        self._by_alias = {}
        for p in manifest.pages:
            for alias in p.aliases:
                self._by_alias[alias] = p

    def load(self, manifest: WikiIndexManifest) -> None:
        self._manifest = manifest
        self._reindex(manifest)

    # ── 查询 ──────────────────────────────────────────────

    def get_page(self, page_id: str) -> WikiPageIndex | None:
        return self._by_id.get(page_id)

    def resolve(self, name: str) -> list[WikiPageIndex]:
        """按 page_id 精确 / 别名 / 标题模糊解析实体。"""
        name = name.strip()
        if not name:
            return []
        results: list[WikiPageIndex] = []
        if name in self._by_id:
            results.append(self._by_id[name])
        if name in self._by_alias:
            results.append(self._by_alias[name])
        if not results:
            lower = name.lower()
            for p in self._manifest.pages:
                if lower in p.page_id.lower() or lower in p.title.lower():
                    results.append(p)
        return _dedupe_by_id(results)

    def search(
        self,
        keyword: str,
        product_id: str | None = None,
        page_type: str | None = None,
        task_affinity: str | None = None,
        limit: int = 50,
    ) -> list[WikiPageIndex]:
        kw = keyword.strip().lower()
        out: list[WikiPageIndex] = []
        for p in self._manifest.pages:
            if product_id and p.product_id != product_id:
                continue
            if page_type and p.page_type != page_type:
                continue
            if task_affinity and task_affinity not in p.task_affinity:
                continue
            if kw and not _matches_kw(p, kw):
                continue
            out.append(p)
        return out[:limit]

    def list_pages(
        self,
        product_id: str | None = None,
        page_type: str | None = None,
        task_affinity: str | None = None,
        limit: int = 50,
    ) -> list[WikiPageIndex]:
        out = [
            p
            for p in self._manifest.pages
            if (not product_id or p.product_id == product_id)
            and (not page_type or p.page_type == page_type)
            and (not task_affinity or task_affinity in p.task_affinity)
        ]
        return out[:limit]


def _dedupe_by_id(items: list[WikiPageIndex]) -> list[WikiPageIndex]:
    seen: set[str] = set()
    out: list[WikiPageIndex] = []
    for p in items:
        if p.page_id in seen:
            continue
        seen.add(p.page_id)
        out.append(p)
    return out


def _matches_kw(p: WikiPageIndex, kw: str) -> bool:
    """关键字匹配：page_id / title / aliases 子串，或命中预生成 search_terms。

    中文检索优先走 search_terms（含 ngram），改善 unicode61 粒度不足问题（§9.4）。
    """
    if kw in p.page_id.lower() or kw in p.title.lower():
        return True
    if any(kw in a.lower() for a in p.aliases):
        return True
    # search_terms：命中（含 词条是查询子串 / 词条包含查询）→ 中文 ngram 召回
    return any(kw == t or (t and (kw in t or t in kw)) for t in p.search_terms)


class WikiIndexStore:
    """Index 的持久化读写（原子写）。"""

    def __init__(self, meta_dir: str | Path):
        self.meta_dir = Path(meta_dir)
        self.index_path = self.meta_dir / DEFAULT_INDEX_FILENAME

    def write(self, manifest: WikiIndexManifest) -> str:
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(self.index_path)
        return manifest.wiki_version

    def load(self) -> WikiIndexManifest | None:
        if not self.index_path.exists():
            return None
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return WikiIndexManifest.model_validate(data)
        except Exception as exc:
            logger.error("WikiIndex 加载失败: %s", exc)
            return None
