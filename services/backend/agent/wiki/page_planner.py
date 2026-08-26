"""Page Planner - 决定 Wiki 应该有哪些页面（不写正文）。

PR-03 第一步：
  输入：Source Docs + Product Catalog
  输出：PagePlan[]（每个产品一组页面计划）

设计约束：
  - 只决定“应该有哪些页面”，不生成正文
  - capability / scenario / limitation 等子页由源文档标题启发式 + 产品关键词派生
  - 稳定 page_id，遵循 contract 规范（product.<id>.capability.<slug> 等）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.wiki.contracts import PagePlan, PlannedPage, slugify

logger = logging.getLogger("backend.agent.wiki.page_planner")

# 标题 → 子页类型的启发式关键词
_CAPABILITY_KEYWORDS = ("能力", "功能", "认证", "授权", "防护", "检测", "治理", "管控", "分析")
_SCENARIO_KEYWORDS = ("场景", "用例", "应用场景", "典型场景", "落地")
_LIMITATION_KEYWORDS = ("限制", "局限", "不支持", "边界", "约束", "注意事项", "前提条件")
_INTEGRATION_KEYWORDS = ("集成", "对接", "适配", "生态", "第三方")

_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$")
_FENCE = "```"


@dataclass(frozen=True)
class SourceDoc:
    """Planner 输入的一篇源文档。"""

    relative_path: str
    title: str = ""
    text: str = ""


def _split_headings(text: str) -> list[tuple[int, str]]:
    """提取正文标题（含层级、去重），跳过代码块。"""
    out: list[tuple[int, str]] = []
    in_fence = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith(_FENCE):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(s)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if 2 <= level <= 3:
                out.append((level, title))
    return out


def _classify_heading(title: str) -> str | None:
    """把标题归类到子页类型；无法归类返回 None。"""
    for kw in _CAPABILITY_KEYWORDS:
        if kw in title:
            return "capability"
    for kw in _SCENARIO_KEYWORDS:
        if kw in title:
            return "scenario"
    for kw in _INTEGRATION_KEYWORDS:
        if kw in title:
            return "integration"
    return None


def _dedupe_slugs(candidates: list[tuple[str, str]]) -> list[str]:
    """(title, base_slug) 列表去重，返回稳定 slug 列表。"""
    seen: set[str] = set()
    result: list[str] = []
    seen_titles: set[str] = set()
    counter = 0
    for title, base in candidates:
        if title in seen_titles:
            continue
        seen_titles.add(title)
        slug = base
        while slug in seen:
            counter += 1
            slug = f"{base}_{counter}"
        seen.add(slug)
        result.append(slug)
    return result


class PagePlanner:
    """根据源文档与产品目录生成页面计划。"""

    def __init__(self, catalog: Any | None = None):
        self._catalog = catalog

    def dimensions_for(self, product_id: str) -> list[str]:
        """返回某产品应生成的子页维度（不含 product/overview/positioning）。"""
        return ["capability", "scenario", "integration", "limitation"]

    def plan_for_product(
        self,
        product_id: str,
        product_name: str = "",
        sources: list[SourceDoc] | list[str] = (),
    ) -> PagePlan:
        """为一个产品生成页面计划。

        sources: 可传入绝对路径字符串或 SourceDoc；自动读取标题与正文。
        """
        pages: list[PlannedPage] = []
        base = "product." + product_id

        pages.append(PlannedPage(page_id=base, page_type="product", product_id=product_id))
        pages.append(
            PlannedPage(page_id=f"{base}.overview", page_type="overview", product_id=product_id)
        )
        pages.append(
            PlannedPage(
                page_id=f"{base}.positioning", page_type="positioning", product_id=product_id
            )
        )

        capability_heads: list[tuple[str, str]] = []
        scenario_heads: list[tuple[str, str]] = []
        integration_heads: list[tuple[str, str]] = []

        docs = self._resolve_sources(sources)
        for doc in docs:
            for _level, title in _split_headings(doc.text):
                cls = _classify_heading(title)
                if cls == "capability":
                    capability_heads.append((title, slugify(title)))
                elif cls == "scenario":
                    scenario_heads.append((title, slugify(title)))
                elif cls == "integration":
                    integration_heads.append((title, slugify(title)))

        capability_heads = (capability_heads or _default_capabilities(product_name))[:12]
        scenario_heads = scenario_heads[:8]
        integration_heads = integration_heads[:8]

        for slug in _dedupe_slugs(capability_heads):
            pages.append(
                PlannedPage(
                    page_id=f"{base}.capability.{slug}",
                    page_type="capability",
                    product_id=product_id,
                    title=slug,
                )
            )

        for slug in _dedupe_slugs(scenario_heads):
            pages.append(
                PlannedPage(
                    page_id=f"{base}.scenario.{slug}",
                    page_type="scenario",
                    product_id=product_id,
                    title=slug,
                )
            )

        for slug in _dedupe_slugs(integration_heads):
            pages.append(
                PlannedPage(
                    page_id=f"{base}.integration.{slug}",
                    page_type="integration",
                    product_id=product_id,
                    title=slug,
                )
            )

        return PagePlan(product_id=product_id, pages=pages)

    def _resolve_sources(self, sources: list[SourceDoc] | list[str]) -> list[SourceDoc]:
        out: list[SourceDoc] = []
        for item in sources:
            if isinstance(item, str):
                path = Path(item)
                try:
                    text = _read_text(path)
                except Exception:
                    text = ""
                out.append(SourceDoc(relative_path=str(path), title=path.stem, text=text))
            else:
                out.append(item)
        return out

    def plan_all(self, products: list[Any]) -> list[PagePlan]:
        return [self.plan_for_product(p.product_id, getattr(p, "name", "")) for p in products]


def _default_capabilities(product_name: str) -> list[tuple[str, str]]:
    """无结构化源时提供 1 个通用 capability 占位（面向增量测试确定性）。"""
    return [("核心能力", "core_ability")]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk")
