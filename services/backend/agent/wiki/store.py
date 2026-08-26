"""WikiStore - Wiki 页面持久化读写、frontmatter 解析与路径安全。

PR-01 产物：
  - Wiki 页面 YAML/JSON frontmatter 解析（安全，不执行任意表达式）
  - SourceRef / WikiPageMeta / WikiRelation 反序列化
  - Page ID <-> 文件路径映射（经过 safe_page_path 校验）
  - 页面不存在返回明确 WikiPageNotFound，路径穿越被拒绝
  - 重复 page_id 检测 / source ref 合法性校验（供 Linter 复用）

安全规则：
  - 只读/写 Wiki Root 下的页面文件
  - 禁止 path traversal、绝对路径、URL 编码穿越
  - frontmatter 只按白名单字段解析，忽略未知字段，禁止求值
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agent.wiki.contracts import (
    PageIdError,
    SourceRef,
    WikiPage,
    WikiPageMeta,
    WikiPageNotFound,
    WikiPathError,
    WikiSection,
    is_path_safe,
    safe_page_path,
    validate_page_id,
)

logger = logging.getLogger("backend.agent.wiki.store")

_META_DIRNAME = "_meta"

# 允许写入的 frontmatter 白名单（用于编译器输出 / 手写检查）
_META_KEYS = {
    "schema_version",
    "page_id",
    "title",
    "page_type",
    "product_id",
    "aliases",
    "task_affinity",
    "relations",
    "source_refs",
    "status",
    "content_hash",
    "updated_at",
}


# ═══════════════════════════════════════════════════════════════
# 轻量 YAML 子集解析器（不依赖 pyyaml）
# ═══════════════════════════════════════════════════════════════

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$")


def _strip_quote(t: str) -> str:
    t = t.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
        return t[1:-1]
    return t


def _parse_scalar(token: str) -> Any:
    token = token.strip()
    if not token:
        return None
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x) for x in inner.split(",") if x.strip()]
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    if token in ("null", "Null", "~"):
        return None
    if _INT.match(token):
        return int(token)
    if _FLOAT.match(token):
        return float(token)
    return _strip_quote(token)


def _tokenize(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            continue
        stripped = raw.rstrip()
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if content:
            lines.append((indent, content))
    return lines


def _parse_block(lines: list[tuple[int, str]], idx: int, indent: int):
    """解析从 idx 开始的块（map 或 seq），返回 (value, next_idx)。"""
    if not lines or idx >= len(lines):
        return {}, idx
    content = lines[idx][1]
    if content == "-" or content.startswith("- "):
        return _parse_seq(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_map(lines: list[tuple[int, str]], idx: int, indent: int):
    obj: dict[str, Any] = {}
    n = len(lines)
    i = idx
    while i < n:
        lind, content = lines[i]
        if lind < indent:
            break
        if lind != indent or content.startswith("-"):
            break
        key, sep, rest = content.partition(":")
        key = _strip_quote(key)
        if not sep:
            break
        if not rest.strip():
            if i + 1 < n and lines[i + 1][0] > indent:
                val, i = _parse_block(lines, i + 1, lines[i + 1][0])
            else:
                val = None
                i += 1
        else:
            val = _parse_scalar(rest)
            i += 1
        obj[key] = val
    return obj, i


def _parse_seq(lines: list[tuple[int, str]], idx: int, indent: int):
    items: list[Any] = []
    n = len(lines)
    i = idx
    while i < n:
        lind, content = lines[i]
        if lind < indent:
            break
        if lind != indent or not (content == "-" or content.startswith("- ")):
            break
        rest = content[1:].strip()
        if not rest:
            if i + 1 < n and lines[i + 1][0] > indent:
                val, i = _parse_block(lines, i + 1, lines[i + 1][0])
            else:
                val, i = None, i + 1
            items.append(val)
            continue
        # 单行内联 map：`- type: belongs_to`
        if ":" in rest and not rest.startswith(("[", "{", "'", '"')):
            k, _sep, vr = rest.partition(":")
            item: dict[str, Any] = {_strip_quote(k): (_parse_scalar(vr) if vr.strip() else None)}
            j = i + 1
            child_indent = None
            while j < n and lines[j][0] > indent:
                clind, ccontent = lines[j]
                if child_indent is None:
                    child_indent = clind
                elif clind < child_indent:
                    break
                if clind != child_indent or ccontent.startswith("-"):
                    break
                ck, _, cr = ccontent.partition(":")
                item[_strip_quote(ck)] = _parse_scalar(cr) if cr.strip() else None
                j += 1
            i = j
            items.append(item)
            continue
        items.append(_parse_scalar(rest))
        i += 1
    return items, i


def parse_frontmatter(text: str) -> dict[str, Any]:
    """解析 YAML/JSON frontmatter，返回 dict；非法返回空 dict（不抛错）。"""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    lines = _tokenize(text)
    if not lines:
        return {}
    try:
        value, _ = _parse_block(lines, 0, lines[0][0])
    except Exception:
        logger.warning("frontmatter 解析失败，回退为空元数据")
        return {}
    return value if isinstance(value, dict) else {}


def extract_frontmatter(md: str) -> tuple[str, str]:
    """从 Markdown 提取 frontmatter 与正文，返回 (frontmatter_text, body)。

    前端 '---' 与后端 '---' 之间的内容是 frontmatter；缺失则 frontmatter 为空。
    """
    stripped = md.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return "", stripped
    lines = stripped.split("\n", 1)
    if len(lines) < 2:
        return "", stripped
    head = lines[1]
    marker = "\n---"
    pos = head.find(marker)
    if pos == -1:
        return head.strip(), ""
    fm = head[:pos]
    body = head[pos + len(marker) :]
    return fm.strip(), body


def meta_from_frontmatter(fm_text: str, defaults: dict | None = None) -> WikiPageMeta:
    """将 frontmatter 文本转换为 WikiPageMeta。"""
    data = parse_frontmatter(fm_text)
    if defaults:
        merged = {**defaults, **{k: v for k, v in data.items() if v is not None}}
        data = merged
    return meta_from_dict(data)


def meta_from_dict(data: dict) -> WikiPageMeta:
    """将 dict 转换为 WikiPageMeta（白名单字段，忽略未知）。"""
    page_id = str(data.get("page_id") or "").strip()
    if not page_id:
        # 由 title 派生稳定性较弱的 fallback；调用方应校验
        title = str(data.get("title") or "untitled").strip()
        from agent.wiki.contracts import slugify

        page_id = f"concept.{slugify(title)}"
    validate_page_id(page_id)

    relations = []
    for rel in data.get("relations") or []:
        if isinstance(rel, dict):
            rtype = rel.get("type") or rel.get("relation_type") or ""
            target = rel.get("target") or rel.get("target_page_id") or ""
            if rtype and target:
                relations.append({"relation_type": rtype, "target_page_id": target})

    source_refs = []
    for src in data.get("source_refs") or []:
        if isinstance(src, dict):
            source_refs.append(src)

    plan = {
        "schema_version": int(data.get("schema_version") or 1),
        "page_id": page_id,
        "title": str(data.get("title") or page_id),
        "page_type": str(data.get("page_type") or data.get("page_type") or "synthesis"),
        "product_id": data.get("product_id"),
        "aliases": list(data.get("aliases") or []),
        "task_affinity": list(data.get("task_affinity") or []),
        "relations": relations,
        "source_refs": source_refs,
        "status": str(data.get("status") or "draft"),
        "content_hash": str(data.get("content_hash") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }
    # page_type 缺省：由 page_id 首段推断
    if not data.get("page_type"):
        page_type = page_id.split(".", 1)[0]
        plan["page_type"] = "product" if page_type == "product" else page_type
    return WikiPageMeta.model_validate(plan)


def parse_wiki_page(md: str, source_path: str = "") -> WikiPage:
    """将带 frontmatter 的 Markdown 解析为 WikiPage。"""
    fm_text, body = extract_frontmatter(md)
    meta = meta_from_frontmatter(fm_text)
    if source_path:
        meta = meta.model_copy(update={})
    sections = _split_body_sections(body, meta.page_id)
    return WikiPage(meta=meta, body=_strip_h1(body, meta.title), sections=sections)


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


def _strip_h1(body: str, title: str) -> str:
    """去掉正文顶部的 H1 标题（与 frontmatter title 对应）。"""
    lines = body.split("\n")
    if lines and lines[0].strip() == f"# {title}":
        return "\n".join(lines[1:]).strip()
    return body.strip()


def _split_body_sections(body: str, page_id: str) -> list[WikiSection]:
    sections: list[WikiSection] = []
    current_title = "_preamble_"
    current_level = 2
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        if buf:
            title = current_title if current_title != "_preamble_" else "概述"
            sections.append(
                WikiSection(title=title, heading_level=current_level, body="\n".join(buf).strip())
            )
            buf.clear()

    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(s)
            if m:
                flush()
                current_title = m.group(2).strip()
                current_level = len(m.group(1))
                continue
        buf.append(line)
    flush()
    return sections


# ═══════════════════════════════════════════════════════════════
# WikiStore
# ═══════════════════════════════════════════════════════════════


class WikiStore:
    """Wiki 页面持久化存储。Runtime Plane 只读；Knowledge Plane 可写。"""

    def __init__(self, wiki_root: str | Path):
        self.root = Path(wiki_root).resolve()
        self._page_index: dict[str, Path] | None = None
        self._index_dirty = True

    def invalidate(self) -> None:
        self._page_index = None
        self._index_dirty = True

    # ── 发现 ──────────────────────────────────────────────

    def _scan(self) -> dict[str, Path]:
        """扫描 Wiki Root 下所有 .md 页面（排除 _meta），返回 page_id -> path。"""
        index: dict[str, Path] = {}
        if not self.root.is_dir():
            self._page_index = index
            self._index_dirty = False
            return index
        for fp in self.root.rglob("*.md"):
            if fp.is_symlink():
                logger.warning("跳过符号链接: %s", fp)
                continue
            rel = str(fp.relative_to(self.root)).replace("\\", "/")
            if rel.startswith(_META_DIRNAME + "/") or not is_path_safe(rel):
                continue
            meta = self._read_meta(fp)
            if meta is None:
                continue
            index[meta.page_id] = fp
        self._page_index = index
        self._index_dirty = False
        return index

    def _read_meta(self, path: Path) -> WikiPageMeta | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            try:
                text = path.read_text(encoding="gbk")
            except Exception:
                return None
        fm_text, _ = extract_frontmatter(text)
        if not fm_text:
            return None
        try:
            return meta_from_frontmatter(fm_text)
        except Exception:
            return None

    def page_index(self) -> dict[str, Path]:
        if self._page_index is None or self._index_dirty:
            self._scan()
        return self._page_index

    # ── 页面访问（Runtime）─────────────────────────────────

    def list_page_ids(self) -> list[str]:
        return sorted(self.page_index().keys())

    def list_pages(self) -> list[WikiPageMeta]:
        metas = [self._read_meta(p) for p in self.page_index().values()]
        return [m for m in metas if m is not None]

    def page_exists(self, page_id: str) -> bool:
        return page_id in self.page_index()

    def _locate(self, page_id: str) -> Path | None:
        idx = self.page_index()
        if page_id in idx:
            return idx[page_id]
        try:
            path = safe_page_path(self.root, page_id)
        except (PageIdError, WikiPathError):
            return None
        return path if path.is_file() else None

    def open_page(self, page_id: str) -> WikiPage:
        """按 page_id 打开页面。不存在抛 WikiPageNotFound，路径非法拒绝。"""
        validate_page_id(page_id)
        path = self._locate(page_id)
        if path is None:
            raise WikiPageNotFound(f"页面不存在: {page_id}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gbk")
        page = parse_wiki_page(text, str(path))
        if page.meta.page_id != page_id:
            logger.warning(
                "page_id 与路径不一致: 期望 %s 实得 %s(%s)", page_id, page.meta.page_id, path
            )
        return page

    def open_page_meta(self, page_id: str) -> WikiPageMeta:
        """打开页面元数据（不读正文）。"""
        validate_page_id(page_id)
        path = self._locate(page_id)
        if path is None:
            raise WikiPageNotFound(f"页面不存在: {page_id}")
        meta = self._read_meta(path)
        if meta is None:
            raise WikiPageNotFound(f"页面无有效 frontmatter: {page_id}")
        return meta

    def page_summaries(self, page_ids: list[str]) -> list[dict]:
        return [{"page_id": pid, "title": self.open_page_meta(pid).title} for pid in page_ids]

    # ── 写入（Knowledge Plane / Compiler / Maintainer）─────

    def write_page(self, page: WikiPage) -> Path:
        """原子写入页面到其规范路径，并刷新索引。"""
        path = safe_page_path(self.root, page.meta.page_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        md = page.render_markdown() + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(path)
        self.invalidate()
        return path

    # ── 校验辅助（供 Linter 复用）──────────────────────────

    def check_source_ref(self, source_ref: SourceRef) -> list[str]:
        """校验单个 SourceRef 的合法性，返回错误列表（空=合法）。"""
        errors: list[str] = []
        if not source_ref.source_id:
            errors.append(f"EMPTY_SOURCE_ID[{source_ref.relative_path}]")
        if not source_ref.relative_path:
            errors.append(f"EMPTY_RELATIVE_PATH[{source_ref.source_id}]")
        if not is_path_safe(source_ref.relative_path):
            errors.append(f"UNSAFE_SOURCE_PATH[{source_ref.source_id}:{source_ref.relative_path}]")
        if not source_ref.content_hash:
            errors.append(f"EMPTY_CONTENT_HASH[{source_ref.source_id}]")
        return errors
