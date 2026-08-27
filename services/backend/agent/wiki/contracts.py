"""Wiki 核心契约（Pydantic）与 Page ID 规范。

PR-01 产物：
  - SourceRef：Raw Source 的稳定引用（source_id + 路径 + 哈希 + heading/section/行区间）
  - WikiRelation：页面间关系
  - WikiPageMeta：Wiki 页面 YAML frontmatter 元数据
  - WikiPage：页面完整对象（meta + 正文 + 章节）
  - PagePlanner / Compiler 的中转契约（PagePlan、WikiPageDraft、ClaimDraft）
  - Page ID 校验 / 路径安全

设计要求：
  - source_refs 不能只停留在页面级；关键 Claim 应保留 claim-level provenance（见 ClaimDraft）
  - 页面 frontmatter 禁止任意表达式求值，仅按白名单字段解析
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# 允许的页面类型
PageType = Literal[
    "product",
    "capability",
    "scenario",
    "integration",
    "limitation",
    "positioning",
    "concept",
    "competitor",
    "synthesis",
    "overview",
]

PAGE_TYPES: frozenset[str] = frozenset(
    {
        "product",
        "capability",
        "scenario",
        "integration",
        "limitation",
        "positioning",
        "concept",
        "competitor",
        "synthesis",
        "overview",
    }
)

PAGE_ID_TYPE_RE = re.compile(
    r"^(product|capability|scenario|integration|limitation|positioning|concept|competitor|synthesis|overview)\."
)
_PAGE_ID_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")

SCHEMA_VERSION = 1

# 页面状态
STATUS_DRAFT = "draft"
STATUS_STAGED = "staged"
STATUS_PUBLISHED = "published"
STATUS_ARCHIVED = "archived"


# ═══════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════


class WikiError(Exception):
    """Wiki 通用错误基类。"""


class WikiPageNotFound(WikiError):
    """页面不存在。"""


class WikiPathError(WikiError):
    """路径穿越 / 非法路径。"""


class PageIdError(WikiError):
    """page_id 不合法或重复。"""


class WikiDuplicatePageId(PageIdError):
    """重复 page_id。"""


# ═══════════════════════════════════════════════════════════════
# 契约
# ═══════════════════════════════════════════════════════════════


class SourceRef(BaseModel):
    """Raw Source 稳定引用，用于 Wiki Claim 到源文件的可追溯。"""

    source_id: str = Field(description="Source Registry 中稳定 source_id")
    relative_path: str = Field(description="源文件相对知识库根的路径")
    content_hash: str = Field(description="源文件内容 SHA-256 哈希")
    heading: str = Field(default="", description="所在章节标题")
    section_id: str = Field(default="", description="章节稳定 ID")
    line_start: int | None = Field(default=None, ge=0, description="起始行号（0 起）")
    line_end: int | None = Field(default=None, ge=0, description="结束行号")


class WikiRelation(BaseModel):
    """页面间关系。"""

    relation_type: str = Field(description="关系类型，如 belongs_to / mitigates / related_to")
    target_page_id: str = Field(description="目标 page_id")


class WikiClaim(BaseModel):
    """Claim-level Provenance 一等对象（Phase 3 / PR-10，G-12）。

    生产级 Evidence 应保留 claim 粒度溯源：一条事实来自一个或多个 SourceRef，
    可通过 claim_id 在跨版本间稳定追踪（不随机 UUID，见 compute_claim_id）。
    """

    claim_id: str = Field(default="", description="稳定 claim_id；空则按 text 计算")
    text: str = Field(description="事实陈述")
    claim_type: str = Field(default="capability")
    source_refs: list[SourceRef] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    effective_from: str | None = Field(default=None)
    effective_to: str | None = Field(default=None)
    status: str = Field(default="active")

    def ensure_id(self, *, product_id: str = "", claim_type: str = "") -> str:
        """补全稳定 claim_id（缺省时按语义内容确定性生成）。"""
        if self.claim_id:
            return self.claim_id
        self.claim_id = compute_claim_id(
            product_id=product_id or "",
            claim_type=claim_type or self.claim_type,
            semantic_key=self.text,
        )
        return self.claim_id


def compute_claim_id(*, product_id: str, claim_type: str, semantic_key: str) -> str:
    """稳定 Claim ID：sha256(normalized(product_id) + normalized(type) + normalized(key))。

    不使用随机 UUID，保证同一事实跨版本/跨构建幂等（§6.1）。
    """

    def _norm(s: str) -> str:
        return (s or "").strip().lower()

    blob = "|".join([_norm(product_id), _norm(claim_type), _norm(semantic_key)])
    return "claim_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class WikiPageMeta(BaseModel):
    """Wiki 页面 YAML frontmatter 元数据。"""

    schema_version: int = Field(default=SCHEMA_VERSION)
    page_id: str = Field(description="稳定 page_id")
    title: str = Field(description="页面标题")
    page_type: str = Field(description="页面类型（page_type）")
    product_id: str | None = Field(default=None)
    aliases: list[str] = Field(default_factory=list)
    task_affinity: list[str] = Field(default_factory=list)
    relations: list[WikiRelation] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    claims: list[WikiClaim] = Field(default_factory=list)
    status: str = Field(default=STATUS_DRAFT)
    content_hash: str = Field(default="")
    updated_at: str = Field(default="")


class WikiSection(BaseModel):
    """页面内章节。"""

    title: str
    heading_level: int = Field(default=2, ge=1, le=6)
    body: str = Field(default="")


class WikiPage(BaseModel):
    """一个完整的 Wiki 页面（meta + 正文 + 章节）。"""

    meta: WikiPageMeta
    body: str = Field(default="")
    sections: list[WikiSection] = Field(default_factory=list)

    def summary(self, max_chars: int = 300) -> str:
        """取页面摘要：优先 Overview/Summary 章节，否则取正文前几行。"""
        for sec in self.sections:
            if sec.title.strip().lower() in {"summary", "摘要", "overview", "概览"}:
                text = " ".join(sec.body.split())[:max_chars]
                return text
        text = " ".join(self.body.split())[:max_chars]
        return text

    def render_markdown(self) -> str:
        """渲染成带 frontmatter 的可读 Markdown。"""
        import json

        meta = self.meta
        fm = {
            "schema_version": meta.schema_version,
            "page_id": meta.page_id,
            "title": meta.title,
            "page_type": meta.page_type,
            "product_id": meta.product_id,
            "aliases": meta.aliases,
            "task_affinity": meta.task_affinity,
            "relations": [
                {"type": r.relation_type, "target": r.target_page_id} for r in meta.relations
            ],
            "source_refs": [
                {
                    "source_id": r.source_id,
                    "relative_path": r.relative_path,
                    "content_hash": r.content_hash,
                    "heading": r.heading,
                    "section_id": r.section_id,
                    "line_start": r.line_start,
                    "line_end": r.line_end,
                }
                for r in meta.source_refs
            ],
            "claims": [c.model_dump() for c in meta.claims],
            "status": meta.status,
            "content_hash": meta.content_hash,
            "updated_at": meta.updated_at,
        }
        self._validate()
        head = "---\n" + json.dumps(fm, ensure_ascii=False, indent=2) + "\n---\n"
        body_md = f"# {meta.title}\n" if meta.title else ""
        if self.body.strip():
            body_md += "\n" + self.body.strip() + "\n"
        for sec in self.sections:
            sec_body = sec.body.strip()
            if sec_body:
                body_md += f"\n{'#' * sec.heading_level} {sec.title}\n\n{sec_body}\n"
        return head + body_md

    def _validate(self) -> None:
        """结构校验（解析期间也已校验）；重复调用无害。"""
        validate_page_id(self.meta.page_id)


# ═══════════════════════════════════════════════════════════════
# Compiler 中转契约
# ═══════════════════════════════════════════════════════════════


class PlannedPage(BaseModel):
    """Page Planner 输出的单页计划（决定应有哪些页面，不写正文）。"""

    page_id: str
    page_type: str
    product_id: str | None = None
    title: str = ""


class PagePlan(BaseModel):
    """Page Planner 输出：一个产品 / 一组页面的计划。"""

    product_id: str | None = None
    pages: list[PlannedPage] = Field(default_factory=list)

    @property
    def page_ids(self) -> list[str]:
        return [p.page_id for p in self.pages]


class ClaimDraft:
    """编译器结构化输出的单条事实声明（含 claim-level provenance）。"""

    __slots__ = ("fact", "heading", "line_end", "line_start", "section_id", "source_id")

    def __init__(
        self,
        fact: str,
        source_id: str = "",
        section_id: str = "",
        heading: str = "",
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> None:
        self.fact = fact
        self.source_id = source_id
        self.section_id = section_id
        self.heading = heading
        self.line_start = line_start
        self.line_end = line_end

    def to_dict(self) -> dict:
        return {
            "fact": self.fact,
            "source_id": self.source_id,
            "section_id": self.section_id,
            "heading": self.heading,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ClaimDraft:
        return cls(
            fact=str(data.get("fact", "")),
            source_id=str(data.get("source_id", "")),
            section_id=str(data.get("section_id", "")),
            heading=str(data.get("heading", "")),
            line_start=data.get("line_start"),
            line_end=data.get("line_end"),
        )


class WikiPageDraft(BaseModel):
    """Page Compiler 输出的单页草稿。"""

    page_id: str
    title: str
    page_type: str
    product_id: str | None = None
    summary: str = Field(default="", description="Summary 章节内容")
    claims: list[dict] = Field(
        default_factory=list,
        description="结构化声明列表（dict 形式，含 fact/source_id/section_id）",
    )
    relations: list[dict] = Field(
        default_factory=list,
        description="建议链接（LLM Suggested Link Pass 2 输入）",
    )
    sections: list[dict] = Field(default_factory=list, description="可选的分章节内容")

    def claim_objects(self) -> list[ClaimDraft]:
        return [ClaimDraft.from_dict(c) for c in self.claims]


# ═══════════════════════════════════════════════════════════════
# Page ID 规范与路径安全
# ═══════════════════════════════════════════════════════════════

# 各 page_type 允许的命名空间前缀
_TYPE_NAMESPACE = {
    "product": "product",
    "capability": "capability",
    "scenario": "scenario",
    "integration": "integration",
    "limitation": "limitation",
    "positioning": "positioning",
    "concept": "concept",
    "competitor": "competitor",
    "synthesis": "synthesis",
    "overview": "product",
}

# 推荐的 page_id 形态（文档 4.2）：
#   product.<product_id>
#   product.<product_id>.capability.<slug>
#   product.<product_id>.scenario.<slug>
#   product.<product_id>.integration.<slug>
#   product.<product_id>.limitation.<slug>
#   concept.<slug>
#   competitor.<slug>
#   scenario.<slug>
#   synthesis.<slug>


def slugify(value: str) -> str:
    """轻量 slug：小写、非字母数字转下划线。"""
    v = value.strip().lower()
    v = re.sub(r"[^a-z0-9]+", "_", v).strip("_")
    return v or "untitled"


def is_valid_page_id(page_id: str) -> bool:
    """校验 page_id 是否合法。"""
    if not page_id or len(page_id) > 200:
        return False
    segments = page_id.split(".")
    if not PAGE_ID_TYPE_RE.match(page_id):
        return False
    return all(_PAGE_ID_SEGMENT_RE.match(seg) for seg in segments)


def validate_page_id(page_id: str) -> str:
    """校验 page_id，非法则抛 PageIdError。

    规则：
      - product.<product_id>：product_id 后不允许再跟额外裸段（必须带类型段）
      - 通用：段必须为 [a-zA-Z0-9_.-]
    """
    if not is_valid_page_id(page_id):
        raise PageIdError(f"非法 page_id: {page_id!r}")
    segments = page_id.split(".")
    ns = segments[0]
    expected = _TYPE_NAMESPACE.get(ns, ns)
    if ns != expected:
        raise PageIdError(f"page_type 命名空间不匹配: {ns!r} != {expected!r}")
    # 产品页不能是 product.product_id.capability；只有带类型段的后缀才承载类型
    return page_id


def page_type_of(page_id: str) -> str | None:
    """从 page_id 推断页面类型命名空间（首段）。"""
    if not page_id or "." not in page_id:
        return None
    return page_id.split(".", 1)[0]


def hash_page_id_text(text: str) -> str:
    """文本的稳定 hash（用于 page_id / 版本）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_path_safe(rel: str) -> bool:
    """Wiki 路径安全检查：拒绝空、绝对路径、..、URL 编码穿越。"""
    if not rel:
        return False
    rel = rel.replace("\\", "/")
    if rel.startswith("/"):
        return False
    if path_traversal(rel):
        return False
    return not ("%2e" in rel.lower() or "%2f" in rel.lower())


def path_traversal(rel: str) -> bool:
    """检测 rel 是否包含路径穿越片段。"""
    norm = rel.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    return ".." in parts


def safe_page_path(root: Path, page_id: str) -> Path:
    """将 page_id 映射到 Wiki Root 下的安全文件路径。

    page_id 的 '.' 分隔映射为子目录，最后一段为文件名。
    例如 product.agent_identity_security.capability.identity_auth
     → products/agent_identity_security/capabilities/identity_auth.md
    """
    validate_page_id(page_id)
    root_resolved = root.resolve()
    segs = page_id.split(".")
    rel_parts: list[str] = []

    ns = segs[0]  # product / concept / competitor / scenario / synthesis
    if ns == "product" and len(segs) >= 2:
        product_seg = segs[1]
        rel_parts.append("products")
        rel_parts.append(product_seg)
        type_part = None
        slug_part = None
        if len(segs) == 3:
            type_part = segs[2]
            slug_part = segs[2]
        elif len(segs) == 4:
            type_part = segs[2]
            slug_part = segs[3]
        if type_part:
            dir_map = {
                "capability": "capabilities",
                "scenario": "scenarios",
                "integration": "integrations",
                "limitation": "limitations",
                "positioning": "positioning",
                "overview": "overview",
            }
            rel_parts.append(dir_map.get(type_part, type_part + "s"))
            rel_parts.append(slug_part + ".md")
        else:
            rel_parts.append("index.md")
    else:
        rel_parts.append(ns + "s")
        rel_parts.append(segs[-1] + ".md")

    rel = "/".join(rel_parts)
    if not is_path_safe(rel):
        raise WikiPathError(f"非安全路径: {rel!r}")
    target = root_resolved.joinpath(*rel_parts)
    if root_resolved not in [target, *target.parents]:
        raise WikiPathError(f"路径越界: {rel!r}")
    return target
