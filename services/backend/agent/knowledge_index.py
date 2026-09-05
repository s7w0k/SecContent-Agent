"""轻量文档索引 - 可版本化、可增量更新、可追溯的 JSON 索引。

阶段四（S4-1 ~ S4-5）产物：
  - S4-1 索引 schema：KnowledgeIndexManifest / DocIndex / SectionIndex、schema 版本、兼容读取、确定性序列化
  - S4-2 文档发现和分类：产品目录映射来自 ProductCatalogService，raw/shared 分类，排除管理文档
  - S4-3 description/summary 生成：规则摘要 + 可选离线 LLM 章节摘要 + schema 与事实约束校验
  - S4-4 增量构建：基于 content_hash 只重建变化文档，临时文件 + os.replace 原子写入
  - S4-5 发布流程对接：构建新索引 → 校验 → 原子发布（由 publication.py 调用）

本阶段不改变 active 检索行为，仅建立可版本化、可增量更新、可追溯的索引。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agent.product_catalog import ProductCatalogService
from pydantic import BaseModel, Field

logger = logging.getLogger("backend.agent.knowledge_index")

# ── 常量 ──────────────────────────────────────────────────────

SCHEMA_VERSION = 1
FORMAT_NAME = "kb-index"
DEFAULT_INDEX_FILENAME = "kb-index.json"

# 长文档阈值（字符数）：超过则强制携带章节清单
LONG_DOC_CHARS = 1500

# 摘要长度上限
DESCRIPTION_CHARS = 300
SUMMARY_CHARS = 300
SECTION_SUMMARY_CHARS = 200

# 超长章节二级切片阈值（字符数，阶段六 S6-1）
SECTION_SPLIT_CHARS = 2000

# 层级
Tier = Literal["required", "optional", "fallback", "shared"]
DocType = Literal[
    "overview",
    "market-brief",
    "sales-brief",
    "architecture-brief",
    "tasks",
    "raw",
    "shared",
]

# 产品目录下显式映射：文件名 → (doc_type, tier)
EXPLICIT_MAPPING: dict[str, tuple[DocType, Tier]] = {
    "overview.md": ("overview", "required"),
    "market-brief.md": ("market-brief", "required"),
    "sales-brief.md": ("sales-brief", "optional"),
    "architecture-brief.md": ("architecture-brief", "optional"),
    "tasks.md": ("tasks", "optional"),
}

# doc_type → 可用用途
DOC_PURPOSES: dict[DocType, tuple[str, ...]] = {
    "overview": ("score", "draft", "chat"),
    "market-brief": ("score", "draft", "chat"),
    "sales-brief": ("draft", "chat"),
    "architecture-brief": (),
    "tasks": (),
    "raw": (),
    "shared": ("score", "draft", "chat"),
}

# 原始文档目录名（映射 raw/fallback）
RAW_DIR = "原始文档"

# 全局排除目录（不进入产品事实索引）
# `_wiki` 为 LLM Wiki 的编译产物目录，必须排除，避免 Legacy 把 Wiki 当原始文档重复读取产生自我污染。
EXCLUDED_DIRS = frozenset({"skills", "_index", "海外版", ".git", "__pycache__", "_wiki"})

# 全局排除的根级管理文档（不作为产品事实文档）
EXCLUDED_ROOT_FILES = frozenset({"AGENTS.md", "CLAUDE.md", "README.md", "qa-log.md"})

# 共享参考目录（映射 shared/shared）
SHARED_DIRS = frozenset({"shared", "0-产品全景"})

_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_CAPITALIZED_TERM = re.compile(r"[A-Z][A-Z0-9a-z\-]{1,30}(?:[A-Z0-9][A-Z0-9a-z\-]*)+")


# ── Schema ────────────────────────────────────────────────────


class SectionIndex(BaseModel):
    """索引中的单个章节条目。"""

    section_id: str = Field(description="稳定章节 ID，格式 doc_id:index")
    title: str = Field(description="章节标题")
    heading_level: int = Field(ge=0, le=6, description="标题层级，0 表示文档前言")
    char_offset: int = Field(ge=0, description="章节起始字符偏移")
    char_count: int = Field(ge=0, description="章节字符数")
    line_offset: int = Field(default=0, ge=0, description="章节起始行号（0 起，阶段六 S6-1）")
    occurrence: int = Field(default=1, ge=1, description="同名标题出现序号（阶段六 S6-1 消歧）")
    summary: str = Field(default="", description="章节摘要")


class DocIndex(BaseModel):
    """索引中的单个文档条目。"""

    doc_id: str = Field(description="稳定文档 ID（基于相对路径哈希）")
    relative_path: str = Field(description="相对知识库根目录的路径")
    title: str = Field(description="文档标题")
    doc_type: DocType = Field(description="文档类型")
    tier: Tier = Field(description="检索层级：required/optional/fallback/shared")
    product_id: str | None = Field(default=None, description="所属产品 ID")
    purposes: list[str] = Field(
        default_factory=list, description="可参与检索的用途（score/draft/chat）"
    )
    published: bool = Field(default=True, description="所属产品是否已发布")
    content_hash: str = Field(description="文档内容 SHA-256 哈希")
    char_count: int = Field(ge=0, description="文档字符数")
    description: str = Field(default="", description="长描述（供检索/摘要注入）")
    summary: str = Field(default="", description="短摘要")
    keywords: list[str] = Field(default_factory=list, description="关键术语")
    sections: list[SectionIndex] = Field(default_factory=list, description="章节清单")
    updated_at: str = Field(default="", description="索引构建时间（ISO）")


class KnowledgeIndexManifest(BaseModel):
    """索引清单 - 可版本化、可追溯的顶层结构。"""

    schema_version: int = Field(default=SCHEMA_VERSION, description="索引 schema 版本")
    format: str = Field(default=FORMAT_NAME, description="索引格式名")
    index_version: str = Field(description="索引内容版本哈希（确定性）")
    catalog_hash: str = Field(description="产品目录哈希")
    built_at: str = Field(description="构建时间（ISO）")
    doc_count: int = Field(ge=0, description="文档数量")
    docs: list[DocIndex] = Field(default_factory=list, description="文档列表")
    hash: str = Field(default="", description="索引内容哈希（与 index_version 一致）")


# ── 发现结果 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class DiscoveredDoc:
    """文档发现阶段产出的待索引条目（尚未读取内容）。"""

    relative_path: str
    product_id: str | None
    doc_type: DocType
    tier: Tier
    published: bool
    purposes: tuple[str, ...]


# ── 工具函数 ──────────────────────────────────────────────────


def _read_text(filepath: Path) -> str:
    """读取文件，自动检测编码（utf-8 → gbk）。"""
    try:
        return filepath.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return filepath.read_text(encoding="gbk")


def _rule_summary(text: str, max_chars: int) -> str:
    """规则摘要：取前几个有意义的非标题段落。"""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    meaningful = [
        line
        for line in lines
        if not line.startswith("#")
        and not line.startswith("```")
        and not line.startswith("![")
        and len(line) > 8
    ]
    snippet = " ".join(meaningful[:3])
    if not snippet:
        snippet = " ".join(lines[:2])
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rstrip() + "…"
    return snippet


def _extract_title(content: str, fallback: str) -> str:
    """从文档前两个标题层级提取标题，否则用文件名回退。"""
    for line in content.split("\n"):
        m = _MARKDOWN_HEADING.match(line.strip())
        if m and len(m.group(1)) <= 2:
            return m.group(2).strip()
    return fallback


def _split_sections(content: str, doc_id: str) -> list[SectionIndex]:
    """Markdown 章节切分（阶段六 S6-1），仅保留章节元数据。"""
    return [
        SectionIndex(**{k: v for k, v in item.items() if k != "text"})
        for item in _split_sections_raw(content, doc_id)
    ]


def extract_section_body(content: str, doc_id: str, section_id: str) -> str:
    """按 section_id 返回章节正文（阶段六 S6-3）。

    与索引切分共用同一逻辑，保证正文与索引章节一致；找不到返回空串。
    """
    for item in _split_sections_raw(content, doc_id):
        if item.get("section_id") == section_id:
            return (item.get("text") or "").strip()
    return ""


def _split_sections_raw(content: str, doc_id: str) -> list[dict[str, Any]]:
    """Markdown 章节切分核心（阶段六 S6-1），返回含正文文本的章节条目。

    特性：
      - 支持 h1~h6 标题层级，稳定 section_id（doc_id:index）
      - 同名标题消歧：occurrence 记录同名出现序号，section_id 仍按索引唯一
      - 行号与字符 offset 双定位（char_offset/line_offset）
      - 代码块完整性：``` 围栏内的标题不切分
      - 超长章节二级切片：超过 SECTION_SPLIT_CHARS 拆为「标题（续N）」子章节
    """
    raw: list[dict[str, Any]] = []
    current_title = ""
    current_level = 0
    buf: list[str] = []
    start_offset = 0
    start_line = 0
    offset = 0
    line_no = 0
    in_fence = False

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        text = "\n".join(buf)
        raw.append(
            {
                "title": current_title or "_preamble_",
                "heading_level": current_level,
                "char_offset": start_offset,
                "char_count": max(0, offset - start_offset - 1),
                "line_offset": start_line,
                "summary": _rule_summary(text, SECTION_SUMMARY_CHARS),
                "text": text,
            }
        )
        buf = []

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            offset += len(line) + 1
            line_no += 1
            continue
        if not in_fence:
            m = _MARKDOWN_HEADING.match(stripped)
            if m:
                flush()
                current_title = m.group(2).strip()
                current_level = len(m.group(1))
                start_offset = offset
                start_line = line_no
                offset += len(line) + 1
                line_no += 1
                continue
        buf.append(line)
        offset += len(line) + 1
        line_no += 1

    flush()

    # 展开为最终章节条目：超长章节二级切片 + 同名序号消歧 + 稳定 section_id
    sections: list[dict[str, Any]] = []
    index = 0
    seen_title: dict[str, int] = {}
    for item in raw:
        title = item["title"]
        occurrence = seen_title.get(title, 0) + 1
        seen_title[title] = occurrence
        for chunk in _split_overlong_section(item, SECTION_SPLIT_CHARS):
            chunk = dict(chunk)
            chunk["section_id"] = f"{doc_id}:{index}"
            chunk["occurrence"] = occurrence
            index += 1
            sections.append(chunk)
    return sections


def _split_overlong_section(item: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """超长章节二级切片：按字符预算把章节拆为多个子条目。

    每个子条目保有正确的 char_offset/char_count，标题追加「（续N）」。
    """
    text = item.get("text") or ""
    if item["char_count"] <= limit or not text:
        return [item]
    chunks: list[dict[str, Any]] = []
    pos = 0
    counter = 1
    while pos < len(text):
        seg = text[pos : pos + limit]
        chunks.append(
            {
                "title": f"{item['title']}（续{counter}）",
                "heading_level": item["heading_level"],
                "char_offset": item["char_offset"] + pos,
                "char_count": len(seg),
                "line_offset": item["line_offset"],
                "summary": _rule_summary(seg, SECTION_SUMMARY_CHARS),
                "text": seg,
            }
        )
        pos += limit
        counter += 1
    return chunks


def _compute_index_version(docs: list[DocIndex]) -> str:
    """对文档内容做确定性序列化并哈希，作为索引版本。

    排除时间戳字段（updated_at），保证相同输入产出稳定哈希。
    """
    payload = []
    for d in sorted(docs, key=lambda x: x.relative_path):
        dd = d.model_dump()
        dd.pop("updated_at", None)
        payload.append(dd)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── 摘要生成 ──────────────────────────────────────────────────

# 可选离线 LLM 摘要器签名：
#   Callable[[str, str, list[str]], dict]
#   input : (content, title, section_titles)
#   output: {"description": str, "summary": str, "sections": {title: str}}
LLMSummarizer = Callable[[str, str, list[str]], dict]


class SummaryGenerator:
    """description/summary 生成。

    规则模式：短 brief 与长文档均使用规则摘要，确定性、可重复。
    可选 LLM 模式：仅对长 raw 文档离线生成章节摘要，失败回退规则摘要。
    """

    def __init__(self, llm_summarizer: LLMSummarizer | None = None):
        self._llm = llm_summarizer

    def generate(
        self,
        content: str,
        title: str,
        sections: list[SectionIndex],
        discovered: DiscoveredDoc,
    ) -> tuple[str, str, dict[str, str]]:
        """返回 (description, summary, section_summaries_by_title)。"""
        section_summaries: dict[str, str] = {}

        if (
            self._llm is not None
            and discovered.tier == "fallback"
            and len(content) >= LONG_DOC_CHARS
        ):
            try:
                result = (
                    self._llm(
                        content=content,
                        title=title,
                        section_titles=[s.title for s in sections],
                    )
                    or {}
                )
                description = str(result.get("description", "")).strip()
                summary = str(result.get("summary", "")).strip() or description
                sections_out = result.get("sections") or {}
                for s_title, s_summary in sections_out.items():
                    if isinstance(s_summary, str) and s_summary.strip():
                        section_summaries[str(s_title)] = s_summary.strip()
            except Exception:
                logger.warning("LLM 摘要生成失败，回退规则摘要")
                description = summary = _rule_summary(content, DESCRIPTION_CHARS)
                section_summaries = {}
        else:
            description = summary = _rule_summary(content, DESCRIPTION_CHARS)

        if not description:
            description = _rule_summary(content, DESCRIPTION_CHARS)
        if not summary:
            summary = description

        return description, summary, section_summaries


# ── 索引构建器 ────────────────────────────────────────────────


class KnowledgeIndexBuilder:
    """发现并构建知识库 JSON 索引，支持增量与原子写入。"""

    def __init__(
        self,
        knowledge_base_dir: str | Path,
        index_dir: str | Path | None = None,
        catalog: ProductCatalogService | None = None,
        llm_summarizer: LLMSummarizer | None = None,
    ):
        self.root = Path(knowledge_base_dir).resolve()
        self.index_dir = Path(index_dir) if index_dir else self.root / "_index"
        self._catalog = catalog or ProductCatalogService(self.root)
        self._summ = SummaryGenerator(llm_summarizer)

    # ── 安全 ──────────────────────────────────────────────────

    @staticmethod
    def _is_safe_rel(rel: str) -> bool:
        """拒绝路径穿越、绝对路径与 URL 编码穿越。"""
        if not rel:
            return False
        if ".." in rel.split("/"):
            return False
        if os.path.isabs(rel) or rel.startswith("/"):
            return False
        return not ("%2e" in rel.lower() or "%2f" in rel.lower())

    # ── 发现和分类（S4-2）────────────────────────────────────

    def discover(self) -> list[DiscoveredDoc]:
        """扫描知识库并分类文档。

        分类规则：
        - 产品目录：overview/market-brief/sales-brief/architecture-brief/tasks 显式映射
        - 原始文档/ → raw/fallback
        - shared/、0-产品全景/ → shared/shared
        - skills/_index/海外版/.git 及根级 AGENTS/CLAUDE/README/qa-log 排除
        - 未发布产品文档入索引但 published=False
        """
        discovered: list[DiscoveredDoc] = []

        for product in self._catalog.list_products(published_only=False):
            product_dir = self.root / product.knowledge_root
            if not product_dir.is_dir():
                logger.warning("产品目录缺失，跳过: %s", product.knowledge_root)
                continue

            # 显式映射文件
            for fname, (doc_type, tier) in EXPLICIT_MAPPING.items():
                abs_path = product_dir / fname
                if abs_path.is_file() and not abs_path.is_symlink():
                    discovered.append(
                        DiscoveredDoc(
                            relative_path=f"{product.knowledge_root}/{fname}",
                            product_id=product.product_id,
                            doc_type=doc_type,
                            tier=tier,
                            published=product.published,
                            purposes=DOC_PURPOSES[doc_type],
                        )
                    )

            # 原始文档 → raw/fallback
            raw_dir = product_dir / RAW_DIR
            if raw_dir.is_dir():
                for fp in sorted(raw_dir.rglob("*.md")):
                    if fp.is_symlink():
                        logger.warning("跳过符号链接: %s", fp)
                        continue
                    rel = str(fp.relative_to(self.root)).replace("\\", "/")
                    if not self._is_safe_rel(rel):
                        logger.warning("跳过不安全路径: %s", rel)
                        continue
                    discovered.append(
                        DiscoveredDoc(
                            relative_path=rel,
                            product_id=product.product_id,
                            doc_type="raw",
                            tier="fallback",
                            published=product.published,
                            purposes=(),
                        )
                    )

        # 共享参考目录
        for shared_dir in SHARED_DIRS:
            sd = self.root / shared_dir
            if not sd.is_dir():
                continue
            for fp in sorted(sd.rglob("*.md")):
                if fp.is_symlink():
                    logger.warning("跳过符号链接: %s", fp)
                    continue
                rel = str(fp.relative_to(self.root)).replace("\\", "/")
                if not self._is_safe_rel(rel):
                    logger.warning("跳过不安全路径: %s", rel)
                    continue
                discovered.append(
                    DiscoveredDoc(
                        relative_path=rel,
                        product_id=None,
                        doc_type="shared",
                        tier="shared",
                        published=True,
                        purposes=DOC_PURPOSES["shared"],
                    )
                )

        discovered.sort(key=lambda d: d.relative_path)
        return discovered

    def _read(self, rel: str, overrides: dict[str, str] | None) -> str:
        """读取文档内容，支持发布预览覆盖。"""
        if overrides and rel in overrides:
            return overrides[rel]
        return _read_text(self.root / rel)

    # ── 构建（S4-3 / S4-4）───────────────────────────────────

    def build_manifest(
        self,
        previous: KnowledgeIndexManifest | None = None,
        content_overrides: dict[str, str] | None = None,
    ) -> KnowledgeIndexManifest:
        """构建索引清单。

        - 增量：content_hash 未变化的文档复用上一版元数据
        - 删除/下线文档从前一 manifest 移除
        - 临时内容覆盖（content_overrides）用于发布前预览
        """
        discovered = self.discover()
        prev_by_id: dict[str, DocIndex] = {}
        if previous is not None:
            prev_by_id = {d.doc_id: d for d in previous.docs}

        now = datetime.now(UTC).isoformat()
        docs: list[DocIndex] = []

        for d in discovered:
            try:
                content = self._read(d.relative_path, content_overrides)
            except Exception as exc:
                logger.warning("读取失败，跳过文档: %s (%s)", d.relative_path, exc)
                continue

            content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            doc_id = "doc:" + hashlib.sha256(d.relative_path.encode("utf-8")).hexdigest()[:16]

            prev = prev_by_id.get(doc_id)
            if prev is not None and prev.content_hash == content_hash:
                # 内容未变化，复用上一版元数据（含摘要、章节清单）
                docs.append(prev)
                continue

            title = _extract_title(content, Path(d.relative_path).stem)
            sections = _split_sections(content, doc_id)
            description, summary, section_summaries = self._summ.generate(
                content, title, sections, d
            )
            for s in sections:
                if s.title in section_summaries:
                    s.summary = section_summaries[s.title][:SECTION_SUMMARY_CHARS]

            docs.append(
                DocIndex(
                    doc_id=doc_id,
                    relative_path=d.relative_path,
                    title=title,
                    doc_type=d.doc_type,
                    tier=d.tier,
                    product_id=d.product_id,
                    purposes=list(d.purposes),
                    published=d.published,
                    content_hash=content_hash,
                    char_count=len(content),
                    description=description,
                    summary=summary,
                    keywords=self._extract_keywords(content, d),
                    sections=sections,
                    updated_at=now,
                )
            )

        docs.sort(key=lambda x: x.relative_path)
        index_version = _compute_index_version(docs)
        return KnowledgeIndexManifest(
            index_version=index_version,
            catalog_hash=self._catalog.catalog_hash(),
            built_at=now,
            doc_count=len(docs),
            docs=docs,
            hash=index_version,
        )

    def _extract_keywords(self, content: str, d: DiscoveredDoc) -> list[str]:
        """提取关键术语：产品关键词 + 大写专业术语。"""
        kws: list[str] = []
        if d.product_id:
            product = self._catalog.get_product(d.product_id)
            if product:
                kws.extend(product.keywords)
        for term in _CAPITALIZED_TERM.findall(content):
            if term not in kws:
                kws.append(term)
        seen: set[str] = set()
        out: list[str] = []
        for kw in kws:
            if kw not in seen:
                seen.add(kw)
                out.append(kw)
        return out[:20]

    # ── 校验（S4-3 事实约束）────────────────────────────────

    def validate(self, manifest: KnowledgeIndexManifest) -> list[str]:
        """schema 与事实约束校验，返回错误列表（空 = 通过）。"""
        errors: list[str] = []
        seen_ids: set[str] = set()
        for d in manifest.docs:
            if d.doc_id in seen_ids:
                errors.append(f"DUP_DOC_ID: {d.doc_id}")
            seen_ids.add(d.doc_id)
            if not self._is_safe_rel(d.relative_path):
                errors.append(f"UNSAFE_PATH: {d.relative_path}")
            if not d.content_hash:
                errors.append(f"EMPTY_CONTENT_HASH: {d.relative_path}")

        # 100% 已发布产品的必需 brief 必须在索引中可定位（仅校验已部署的产品目录）
        for product in self._catalog.list_products(published_only=True):
            product_dir = self.root / product.knowledge_root
            if not product_dir.is_dir():
                # 产品目录未部署，不强制要求 brief（避免空目录构建被误判失败）
                continue
            for fname in ("overview.md", "market-brief.md"):
                rel = f"{product.knowledge_root}/{fname}"
                if not any(d.relative_path == rel for d in manifest.docs):
                    errors.append(f"MISSING_REQUIRED_BRIEF: {product.product_id}:{fname}")

        return errors

    # ── 原子写入（S4-4）──────────────────────────────────────

    def write(
        self,
        manifest: KnowledgeIndexManifest,
        index_dir: str | Path | None = None,
    ) -> str:
        """确定性序列化并原子写入 kb-index.json，返回 index_version。"""
        target_dir = Path(index_dir) if index_dir else self.index_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / DEFAULT_INDEX_FILENAME

        payload = manifest.model_dump()
        blob = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

        fd, tmp_path = tempfile.mkstemp(dir=str(target_dir), prefix=".kb-index.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp_path)
            raise

        # 写入后校验
        written = target.read_text(encoding="utf-8")
        if json.loads(written) != json.loads(blob):
            raise RuntimeError("索引写入校验失败: 内容不一致")
        logger.info("索引写入成功: %s (docs=%d)", target, manifest.doc_count)
        return manifest.index_version


# ── 运行时加载器 ───────────────────────────────────────────────


class KnowledgeIndexer:
    """运行时加载并查询索引，支持 schema 版本兼容读取。"""

    def __init__(self, index_path: str | Path | None = None):
        self.index_path = Path(index_path) if index_path else None
        self._manifest: KnowledgeIndexManifest | None = None

    def load(self, index_path: str | Path | None = None) -> KnowledgeIndexManifest | None:
        """读取索引（兼容 schema_version 变化）。"""
        path = Path(index_path) if index_path else self.index_path
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("索引读取失败: %s (%s)", path, exc)
            return None
        if not isinstance(data, dict) or "docs" not in data:
            logger.error("索引结构非法: %s", path)
            return None
        try:
            manifest = KnowledgeIndexManifest.model_validate(data)
        except Exception as exc:
            logger.error("索引 schema 校验失败: %s (%s)", path, exc)
            return None
        self._manifest = manifest
        return manifest

    def get(self, doc_id: str) -> DocIndex | None:
        """按 doc_id 取文档条目。"""
        if self._manifest is None:
            return None
        for d in self._manifest.docs:
            if d.doc_id == doc_id:
                return d
        return None

    def for_product(self, product_id: str) -> list[DocIndex]:
        """取某产品的全部文档条目。"""
        if self._manifest is None:
            return []
        return [d for d in self._manifest.docs if d.product_id == product_id]

    @property
    def manifest(self) -> KnowledgeIndexManifest | None:
        return self._manifest

    @property
    def index_version(self) -> str:
        return self._manifest.index_version if self._manifest else ""
