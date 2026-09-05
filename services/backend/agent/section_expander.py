"""章节级按需展开与检索（阶段六 S6-2 / S6-3）。

职责：
  - S6-2 展开决策：确定性触发（query 命中 raw 章节精确术语、summary 标记需展开、
          参数/案例/部署/版本类诉求），不依赖 LLM 工具调用作为首期唯一决策者
  - S6-3 章节正文获取：get_document_outline / get_section，校验 doc_id 必须属于
          冻结产品、当前用户与冻结索引版本
  - 章节正文受 token/字符预算限制，超长章节按预算截断

安全约束：
  - 只读，不写任何文件
  - 产品硬过滤：doc.product_id 必须为空（shared）或属于请求产品集合
  - 用户隔离：只允许访问全局已发布索引内容，不读取用户私有文档
  - 索引版本一致性：传入 index_version 时必须与索引一致，否则拒绝展开
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent.knowledge_index import (
    DocIndex,
    KnowledgeIndexer,
    SectionIndex,
    _read_text,
    extract_section_body,
)

logger = logging.getLogger("backend.agent.section_expander")

# 单章节展开的字符预算上限（token 限制的近似）
SECTION_BODY_CHARS = 1500

# query 分词：空白 + 中英文常见标点
_TERM_SPLIT = re.compile(r"[\s，。；、,.!?；：:（）()\[\]【】“”\"'\-]+")

# 确定性展开触发：query 命中这些"需细节"意图词时倾向展开
_EXPAND_HINT_TERMS = frozenset(
    {
        "参数",
        "指标",
        "版本",
        "部署",
        "上线",
        "案例",
        "客户",
        "竞品",
        "规格",
        "配置",
        "阈值",
        "错误码",
        "接口",
        "步骤",
        "方案",
    }
)


@dataclass(frozen=True)
class OutlineSection:
    """文档大纲中的单个章节（不含正文）。"""

    doc_id: str
    section_id: str
    title: str
    heading_level: int
    char_count: int
    occurrence: int
    summary: str = ""


@dataclass(frozen=True)
class DocOutline:
    """文档大纲（供生成模型选择要展开的章节）。"""

    doc_id: str
    relative_path: str
    title: str
    doc_type: str
    sections: list[OutlineSection] = field(default_factory=list)


@dataclass(frozen=True)
class SectionBody:
    """展开后的章节正文（含引用元数据所需字段）。"""

    doc_id: str
    section_id: str
    title: str
    content: str
    char_count: int
    needs_confirmation: bool = False


def _tokenize(query: str) -> list[str]:
    return [t for t in _TERM_SPLIT.split((query or "").lower()) if t]


def _section_score(query_terms: list[str], section: SectionIndex) -> float:
    """确定性相关度打分：标题命中 > summary 命中；用于展开决策。"""
    if not query_terms:
        return 0.0
    title = section.title.lower()
    summary = (section.summary or "").lower()
    score = 0.0
    for term in query_terms:
        if term in title:
            score += 3.0
        elif term in summary:
            score += 1.0
    return score


def _has_expand_hint(query: str) -> bool:
    """query 是否包含确定性展开意图词（S6-2）。"""
    return any(t in (query or "") for t in _EXPAND_HINT_TERMS)


def select_sections_for_expansion(
    doc: DocIndex,
    query: str,
    max_expanded: int = 2,
) -> list[SectionIndex]:
    """确定性选择要展开的章节（S6-2）。

    规则：
      - 优先按 query 术语对章节标题/summary 打分，取分数 > 0 且最高的 max_expanded 个
      - 无任何命中且 query 含展开意图词时，取文档头部 max_expanded 个章节（降级为概貌）
      - 返回的章节正文会由 get_section 按 SECTION_BODY_CHARS 截断
    """
    if not doc.sections:
        return []
    terms = _tokenize(query)
    scored = [(s, _section_score(terms, s)) for s in doc.sections]
    hits = sorted(
        [s for s, sc in scored if sc > 0],
        key=lambda s: (-_section_score(terms, s), s.char_offset),
    )
    if hits:
        return hits[:max_expanded]
    if _has_expand_hint(query):
        return sorted(doc.sections, key=lambda s: s.char_offset)[:max_expanded]
    return []


class SectionExpander:
    """基于索引 + 知识库文件提供章节按需展开。"""

    def __init__(
        self,
        knowledge_base_dir: str | Path,
        indexer: KnowledgeIndexer | None = None,
        index_path: str | Path | None = None,
        section_body_chars: int = SECTION_BODY_CHARS,
    ):
        self._knowledge_base_dir = Path(knowledge_base_dir)
        self._indexer = indexer or KnowledgeIndexer(index_path)
        self._section_body_chars = section_body_chars

    # ── 加载与校验（S6-3）─────────────────────────────────

    def _manifest(self):
        manifest = self._indexer.manifest
        if manifest is None:
            manifest = self._indexer.load()
        return manifest

    def _validate_access(
        self,
        doc: DocIndex,
        *,
        product_ids: list[str] | None,
        user_id: str | None,
        index_version: str,
    ) -> bool:
        """校验 doc 可被当前用户在当前产品/索引版本下访问。"""
        # 索引版本一致性：传入时必须匹配
        if index_version:
            manifest = self._manifest()
            if manifest is None or manifest.index_version != index_version:
                logger.warning(
                    "SectionExpander index version mismatch: expected=%s got=%s",
                    index_version,
                    self._indexer.index_version,
                )
                return False
        # 产品硬过滤：shared 文档（product_id=None）对所有请求可见；否则必须属于请求产品
        if product_ids and doc.product_id is not None and doc.product_id not in set(product_ids):
            return False
        # 用户隔离：仅全局已发布内容可展开；索引不承载用户私有文档，
        # 故此处仅记录 user_id 契约，不读取私有数据
        return doc.published

    def _get_doc(self, doc_id: str) -> DocIndex | None:
        return self._indexer.get(doc_id)

    # ── 大纲（S6-3 get_document_outline）─────────────────

    def get_document_outline(
        self,
        doc_id: str,
        *,
        product_ids: list[str] | None = None,
        user_id: str | None = None,
        index_version: str = "",
    ) -> DocOutline | None:
        """返回文档章节大纲；访问不被允许或文档不存在时返回 None。"""
        doc = self._get_doc(doc_id)
        if doc is None:
            return None
        if not self._validate_access(
            doc, product_ids=product_ids, user_id=user_id, index_version=index_version
        ):
            return None
        return DocOutline(
            doc_id=doc.doc_id,
            relative_path=doc.relative_path,
            title=doc.title,
            doc_type=doc.doc_type,
            sections=[
                OutlineSection(
                    doc_id=doc.doc_id,
                    section_id=s.section_id,
                    title=s.title,
                    heading_level=s.heading_level,
                    char_count=s.char_count,
                    occurrence=s.occurrence,
                    summary=s.summary,
                )
                for s in doc.sections
            ],
        )

    # ── 正文（S6-3 get_section）──────────────────────────

    def get_section(
        self,
        doc_id: str,
        section_id: str,
        *,
        product_ids: list[str] | None = None,
        user_id: str | None = None,
        index_version: str = "",
        needs_confirmation: bool = False,
    ) -> SectionBody | None:
        """展开指定章节正文；校验产品/用户/索引版本后返回，超长按预算截断。"""
        doc = self._get_doc(doc_id)
        if doc is None:
            return None
        if not self._validate_access(
            doc, product_ids=product_ids, user_id=user_id, index_version=index_version
        ):
            return None
        if not any(s.section_id == section_id for s in doc.sections):
            return None

        try:
            content = _read_text(self._knowledge_base_dir / doc.relative_path)
        except Exception as exc:
            logger.warning("SectionExpander read failed %s: %s", doc.relative_path, exc)
            return None

        body = extract_section_body(content, doc.doc_id, section_id)
        if not body:
            logger.warning("SectionExpander body not found: %s:%s", doc_id, section_id)
            return None

        title = next((s.title for s in doc.sections if s.section_id == section_id), section_id)
        if len(body) > self._section_body_chars:
            body = body[: self._section_body_chars].rstrip() + "…"

        return SectionBody(
            doc_id=doc.doc_id,
            section_id=section_id,
            title=title,
            content=body,
            char_count=len(body),
            needs_confirmation=needs_confirmation,
        )

    def expand_sections(
        self,
        doc_id: str,
        section_ids: list[str],
        *,
        product_ids: list[str] | None = None,
        user_id: str | None = None,
        index_version: str = "",
    ) -> list[SectionBody]:
        """批量展开多个章节；每个章节独立校验，越权/缺失的跳过。"""
        out: list[SectionBody] = []
        for sid in section_ids:
            body = self.get_section(
                doc_id,
                sid,
                product_ids=product_ids,
                user_id=user_id,
                index_version=index_version,
                needs_confirmation=True,
            )
            if body is not None:
                out.append(body)
        return out
