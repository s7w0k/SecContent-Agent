"""Wiki 测试共享构造工具（可被各 test_*.py 直接 import）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent.wiki.contracts import SourceRef, WikiPage, WikiPageMeta, WikiRelation, WikiSection


def sha256_hex(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_source_file(root: Path, rel: str, text: str) -> tuple[str, str]:
    """创建 Raw Source 文件，返回 (relative_path, content_hash)。"""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel, sha256_hex(text)


def make_page(
    page_id: str,
    *,
    title: str | None = None,
    page_type: str | None = None,
    product_id: str | None = None,
    aliases: list[str] | None = None,
    relations: list[WikiRelation] | None = None,
    source_refs: list[SourceRef] | None = None,
    body_extra: str = "",
) -> WikiPage:
    """构造带 frontmatter 的 WikiPage 对象。

    page_type 缺省时从 page_id 的已知类型段推断（如
    product.a.capability.x → "capability"）。
    仅当提供了 source_refs 时才在证据章节写入 [来源: ...] 标记，
    避免让无 source_refs 的页面被误判为已 grounding。
    """
    ns = page_id.split(".", 1)[0]
    if page_type is None:
        known = {
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
        types = [seg for seg in page_id.split(".") if seg in known]
        page_type = types[-1] if types else (ns if ns != "product" else "product")
    meta = WikiPageMeta(
        page_id=page_id,
        title=title or page_id,
        page_type=page_type,
        product_id=product_id,
        aliases=aliases or [],
        relations=relations or [],
        source_refs=source_refs or [],
        status="published",
        content_hash=sha256_hex(page_id),
    )
    if source_refs:
        evidence_body = (
            "- 支持智能体身份认证 [来源: overview.md]\n- 提供 MCP 协议防护 [来源: overview.md]"
        )
    else:
        evidence_body = "支持智能体身份认证统合能力\n提供 MCP 协议防护能力"
    sections = [
        WikiSection(title="summary", heading_level=2, body=f"{meta.title} 核心能力概述。"),
        WikiSection(
            title="Evidence & Sources",
            heading_level=2,
            body=evidence_body,
        ),
    ]
    return WikiPage(meta=meta, body=body_extra or f"# {meta.title}", sections=sections)
