"""
产品知识库加载器 (V2)

从 docs/ 目录递归扫描所有 Markdown 文档，提取结构化知识信息，
合并后作为打分 Agent 和报道 Agent 的 System Prompt 上下文。

特性:
  - 启动时异步加载，内存缓存
  - 多文件遍历（递归扫描 docs/ 下所有 .md）
  - 多文档知识合并（去重）
  - 文件变更检测 + 热重载（联合 MD5 校验）
  - 支持纯文本解析（默认）和 LLM 增强提取（可选）
  - as_system_prompt() 输出可直接注入 Prompt

V2 变更:
  - 默认 filename=None，递归扫描 docs_dir 下所有 .md
  - 传入 filename 时仅加载指定文件（向后兼容）
  - as_system_prompt() 新增竞品信息 + 产品名称 + 来源标注
  - MarkdownKnowledgeParser 新增关键术语自动提取

使用:
    loader = KnowledgeLoader(docs_dir="/app/docs")
    knowledge = await loader.load()
    prompt_prefix = knowledge.as_system_prompt()
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("backend.agent.knowledge")


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════


@dataclass
class ProductKnowledge:
    """产品知识库结构化数据"""

    product_name: str = "智能体身份安全产品"
    product_positioning: str = ""
    core_features: list[str] = field(default_factory=list)
    tech_barriers: list[str] = field(default_factory=list)
    control_points: list[str] = field(default_factory=list)
    customer_cases: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    target_industries: list[str] = field(default_factory=list)

    # 关键术语（用于打分时的关键词匹配）
    key_terms: list[str] = field(default_factory=lambda: [
        "智能体安全",
        "Agent安全",
        "MCP协议",
        "身份认证",
        "权限管控",
        "意图识别",
        "提示注入",
        "模型攻击",
        "供应链安全",
    ])

    # 元信息（V2: source_files 替代 source_file）
    source_files: list[str] = field(default_factory=list)
    loaded_at: str = ""
    content_hash: str = ""

    # V1 兼容属性
    @property
    def source_file(self) -> str:
        return self.source_files[0] if self.source_files else ""

    @source_file.setter
    def source_file(self, value: str) -> None:
        if value and value not in self.source_files:
            self.source_files.append(value)

    def as_system_prompt(self) -> str:
        """将知识库转换为 System Prompt 片段（可注入打分/报道 Agent）。

        输出经过优化，直接可嵌入 LLM System Prompt 中作为产品上下文。
        """
        parts: list[str] = []

        if self.product_name:
            parts.append(f"## 产品\n{self.product_name}")

        if self.product_positioning:
            parts.append(f"## 产品定位与市场研判\n{self.product_positioning}")

        if self.core_features:
            features = "\n".join(f"- {f}" for f in self.core_features)
            parts.append(f"## 核心功能\n{features}")

        if self.tech_barriers:
            barriers = "\n".join(f"- {b}" for b in self.tech_barriers)
            parts.append(f"## 技术壁垒\n{barriers}")

        if self.control_points:
            points = "\n".join(f"- {p}" for p in self.control_points)
            parts.append(f"## 控标点\n{points}")

        if self.customer_cases:
            cases = "\n".join(f"- {c}" for c in self.customer_cases)
            parts.append(f"## 客户案例\n{cases}")

        if self.competitors:
            comps = "\n".join(f"- {c}" for c in self.competitors)
            parts.append(f"## 竞品信息\n{comps}")

        if self.target_industries:
            industries = "\n".join(f"- {i}" for i in self.target_industries)
            parts.append(f"## 目标行业\n{industries}")

        if self.key_terms:
            terms = ", ".join(self.key_terms)
            parts.append(f"## 关键术语\n{terms}")

        # V2: 标注知识来源
        if self.source_files:
            sources = ", ".join(Path(s).name for s in self.source_files)
            parts.append(f"\n> 知识来源: {sources}")

        return "\n\n".join(parts)

    def as_keywords(self) -> list[str]:
        """返回所有可用于匹配的关键词列表"""
        keywords: list[str] = list(self.key_terms)
        for field_items in [
            self.core_features,
            self.tech_barriers,
            self.control_points,
            self.competitors,
        ]:
            for item in field_items:
                words = item.replace("、", ",").replace("，", ",").split(",")
                for w in words:
                    w = w.strip()
                    if 2 <= len(w) <= 12:
                        keywords.append(w)
        return list(set(keywords))


# ═══════════════════════════════════════════════════════════════
# 文档解析器 — 基于 Markdown 结构的规则提取
# ═══════════════════════════════════════════════════════════════


class MarkdownKnowledgeParser:
    """从产品 Markdown 文档中提取结构化知识（规则 + 启发式）"""

    @staticmethod
    def parse(content: str, source_path: str = "") -> ProductKnowledge:
        """解析 Markdown 文档，提取 ProductKnowledge 结构。

        Args:
            content: Markdown 文本内容
            source_path: 文件路径（用于元信息追踪）
        """
        knowledge = ProductKnowledge()
        if source_path:
            knowledge.source_files.append(source_path)

        # ── 定位关键章节 ──
        sections = MarkdownKnowledgeParser._split_sections(content)

        # ── 产品名称（从一级标题）──
        for line in content.split("\n"):
            if line.startswith("# ") and "智能体" in line:
                knowledge.product_name = line[2:].strip()
                break

        # ── 产品定位 ──
        positioning_keywords = ["愿景", "定位", "业务架构", "产品规划", "形势研判"]
        for kw in positioning_keywords:
            for title, text in sections.items():
                if kw in title:
                    positioning = MarkdownKnowledgeParser._extract_summary(text)
                    if positioning:
                        if knowledge.product_positioning:
                            knowledge.product_positioning += "\n" + positioning
                        else:
                            knowledge.product_positioning = positioning

        # ── 核心功能 / 技术壁垒 ──
        for title, text in sections.items():
            title_lower = title.lower()
            if any(w in title_lower for w in ["功能", "独创", "壁垒", "架构"]):
                items = MarkdownKnowledgeParser._extract_list_items(text)
                if items:
                    if any(w in title_lower for w in ["功能", "架构"]):
                        knowledge.core_features.extend(items)
                    else:
                        knowledge.tech_barriers.extend(items)

        # ── 控标点 / 竞争优势 ──
        for title, text in sections.items():
            if any(w in title for w in ["控标", "竞争", "优势"]):
                items = MarkdownKnowledgeParser._extract_list_items(text)
                knowledge.control_points.extend(items)

        # ── 客户案例 ──
        for title, text in sections.items():
            if any(w in title for w in ["客户", "案例", "痛点"]):
                items = MarkdownKnowledgeParser._extract_list_items(text)
                knowledge.customer_cases.extend(items)

        # ── 目标行业 ──
        for title, text in sections.items():
            if any(w in title for w in ["行业", "市场", "运营商"]):
                industries = MarkdownKnowledgeParser._extract_industry_keywords(text)
                knowledge.target_industries.extend(industries)

        # ── 竞品 ──
        for title, text in sections.items():
            if any(w in title for w in ["竞品", "对手", "友商"]):
                items = MarkdownKnowledgeParser._extract_list_items(text)
                knowledge.competitors.extend(items)

        # ── 关键术语自动提取 ──
        extra_terms = MarkdownKnowledgeParser._extract_key_terms(content)
        for term in extra_terms:
            if term not in knowledge.key_terms:
                knowledge.key_terms.append(term)

        # ── 兜底：全文摘要作为定位 ──
        if not knowledge.product_positioning:
            knowledge.product_positioning = MarkdownKnowledgeParser._extract_summary(content)

        return knowledge

    @staticmethod
    def _extract_key_terms(content: str) -> list[str]:
        """从正文中自动提取安全领域关键术语"""
        security_terms = [
            "MCP协议", "A2A协议", "Agent安全", "智能体安全",
            "身份认证", "权限管控", "意图识别", "提示注入",
            "模型攻击", "供应链安全", "数据隐私", "零信任",
            "API安全", "工具调用安全", "自主行为风险",
            "CUA", "OpenClaw", "Physical AI", "具身智能",
        ]
        return [t for t in security_terms if t.lower() in content.lower()]

    @staticmethod
    def _split_sections(content: str) -> dict[str, str]:
        """按 ## 标题拆分文档"""
        sections: dict[str, str] = {}
        current_title = "__preamble__"
        current_text: list[str] = []

        for line in content.split("\n"):
            if line.startswith("## "):
                if current_text:
                    sections[current_title] = "\n".join(current_text)
                current_title = line[3:].strip()
                current_text = []
            else:
                current_text.append(line)

        if current_text:
            sections[current_title] = "\n".join(current_text)

        return sections

    @staticmethod
    def _extract_summary(text: str) -> str:
        """提取文本摘要（取非空非标题的有效段落）"""
        lines = [line.strip() for line in text.split("\n")
                 if line.strip() and not line.startswith("#")]
        meaningful = [line for line in lines
                      if len(line) > 10 and not line.startswith("![")]
        return " ".join(meaningful[:5])

    @staticmethod
    def _extract_list_items(text: str) -> list[str]:
        """提取列表项（- 开头或数字. 开头）"""
        items: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                item = stripped[2:].strip()
                if len(item) > 3:
                    items.append(item)
            elif stripped and stripped[0].isdigit() and ". " in stripped[:4]:
                item = stripped.split(". ", 1)[1].strip()
                if len(item) > 3:
                    items.append(item)
        return items

    @staticmethod
    def _extract_industry_keywords(text: str) -> list[str]:
        """从文本中提取行业关键词"""
        industry_keywords = [
            "运营商", "金融", "政务", "能源", "制造", "医疗",
            "教育", "互联网", "交通", "电力", "电信",
        ]
        return [kw for kw in industry_keywords if kw in text]


# ═══════════════════════════════════════════════════════════════
# KnowledgeLoader — 主类（V2：多文件支持）
# ═══════════════════════════════════════════════════════════════


# 评分相关文件白名单（相对于知识库根目录）
_SCORING_FILE_PATTERNS = [
    "0-产品全景/*.md",
    "1-智能体身份安全/overview.md",
    "1-智能体身份安全/market-brief.md",
    "1-智能体身份安全/sales-brief.md",
    "3-AI-BOM/overview.md",
    "3-AI-BOM/market-brief.md",
    "3-AI-BOM/sales-brief.md",
    "shared/*.md",
]


def _is_scoring_relevant(root_dir: Path, filepath: Path) -> bool:
    """判断文件是否与产品相关度评分有关。排除 tasks/architecture/原始文档/海外版。"""
    for pattern in _SCORING_FILE_PATTERNS:
        if filepath.match(pattern.replace("/", os.sep)):
            return True
    rel = str(filepath.relative_to(root_dir)).replace("\\", "/")
    return not any(
        p in rel for p in ("原始文档", "海外版", "tasks.md", "architecture-brief.md",
                           "CLAUDE.md", "AGENTS.md", "README.md", "qa-log.md",
                           ".git", "项目核心逻辑.md", "工作日报.md")
    )


class KnowledgeLoader:
    """产品知识库加载器（V2）。

    特性:
      - 启动时异步加载（await loader.load()）
      - 多文件扫描：递归遍历 docs/ 下所有 .md 文件
      - 多文档知识合并（自动去重）
      - 内存缓存（避免重复读取）
      - 文件联合 MD5 校验（检测变更自动热重载）
      - 纯规则解析（默认），可选 LLM 增强

    向后兼容:
      - 传入 filename 时仅加载指定文件（V1 行为）
      - 不传 filename 时递归扫描所有 .md（V2 行为）
    """

    DEFAULT_DOC = "智能体身份安全产品计划和目标.md"

    def __init__(
        self,
        docs_dir: str = "/app/docs",
        filename: str | None = None,
    ):
        """
        Args:
            docs_dir: 文档目录路径
            filename: 指定文件名（None = 递归扫描所有 .md，向后兼容）
        """
        self.docs_dir = Path(docs_dir)
        self.filename = filename  # None → multi-file mode

        # 缓存
        self._cache: ProductKnowledge | None = None
        self._last_hash: str = ""
        self._last_loaded: datetime | None = None

    @property
    def is_loaded(self) -> bool:
        """是否已成功加载知识库"""
        return self._cache is not None

    @property
    def loaded_at(self) -> datetime | None:
        return self._last_loaded

    # ── 文件发现 ──────────────────────────────────────────────

    def _discover_files(self) -> list[Path]:
        """递归扫描 docs_dir 下评分相关的 .md 文件。

        如果构造时指定了 filename，则只返回该文件（向后兼容）。
        """
        if self.filename:
            filepath = self.docs_dir / self.filename
            if filepath.exists():
                return [filepath]
            logger.warning("Specified file not found: %s", filepath)
            return []

        # V2: 递归扫描所有 .md，按评分相关性过滤
        all_files = sorted(self.docs_dir.rglob("*.md"))
        files = [f for f in all_files if _is_scoring_relevant(self.docs_dir, f)]
        skipped = len(all_files) - len(files)
        logger.info("Discovered %d .md files (%d scoring-relevant, %d skipped)",
                     len(all_files), len(files), skipped)
        return files

    # ── 文件读取与哈希 ────────────────────────────────────────

    @staticmethod
    def _read_file(filepath: Path) -> str:
        """读取单个文档文件（自动检测编码）"""
        try:
            return filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return filepath.read_text(encoding="gbk")

    def _compute_hash(self, files: list[Path]) -> str:
        """计算所有文件的联合 MD5 哈希（用于变更检测）。"""
        hasher = hashlib.md5(usedforsecurity=False)
        for fp in files:
            try:
                content = self._read_file(fp)
                hasher.update(content.encode("utf-8"))
            except Exception:
                logger.warning("Cannot hash file: %s", fp)
        return hasher.hexdigest()

    # ── 知识合并 ──────────────────────────────────────────────

    @staticmethod
    def _merge_knowledge(base: ProductKnowledge, add: ProductKnowledge) -> ProductKnowledge:
        """合并两个 ProductKnowledge 对象（去重 + 保留顺序）。

        Args:
            base: 基础知识（将被原地修改）
            add: 要合并的知识

        Returns:
            合并后的 base
        """
        # 产品名称：取第一个非默认名
        if base.product_name == "智能体身份安全产品" and add.product_name != "智能体身份安全产品":
            base.product_name = add.product_name

        # 产品定位：拼接非重复段落
        if add.product_positioning and add.product_positioning not in base.product_positioning:
            if base.product_positioning:
                base.product_positioning += "\n" + add.product_positioning
            else:
                base.product_positioning = add.product_positioning

        # 列表字段：合并去重
        list_fields = [
            "core_features", "tech_barriers", "control_points",
            "customer_cases", "competitors", "target_industries",
        ]
        for field_name in list_fields:
            base_list = getattr(base, field_name)
            add_list = getattr(add, field_name)
            for item in add_list:
                if item not in base_list:
                    base_list.append(item)

        # 关键术语：合并去重
        for term in add.key_terms:
            if term not in base.key_terms:
                base.key_terms.append(term)

        # 来源文件
        for src in add.source_files:
            if src not in base.source_files:
                base.source_files.append(src)

        return base

    # ── 主加载逻辑 ────────────────────────────────────────────

    async def load(self, force: bool = False) -> ProductKnowledge:
        """加载知识库（首次调用时读取并解析所有 .md，后续命中缓存）。

        Args:
            force: 强制重新加载（忽略缓存和 hash 校验）

        Returns:
            ProductKnowledge 结构化知识对象（至少包含默认 key_terms）
        """
        files = self._discover_files()

        if not files:
            logger.warning("No .md files found in: %s", self.docs_dir)
            return ProductKnowledge()

        # 计算联合 hash
        content_hash = self._compute_hash(files)

        # 缓存命中
        if not force and self._cache is not None and content_hash == self._last_hash:
            logger.debug("Knowledge cache hit (hash=%s, %d files)", content_hash[:8], len(files))
            return self._cache

        # 逐个文件解析并合并
        logger.info("Loading knowledge from %d .md files in: %s", len(files), self.docs_dir)
        merged = ProductKnowledge()
        for fp in files:
            try:
                content = self._read_file(fp)
                knowledge = MarkdownKnowledgeParser.parse(content, str(fp))
                merged = self._merge_knowledge(merged, knowledge)
                logger.debug("  Parsed: %s", fp.name)
            except Exception as e:
                logger.warning("Failed to parse %s: %s", fp.name, e)

        merged.loaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        merged.content_hash = content_hash

        # 更新缓存
        self._cache = merged
        self._last_hash = content_hash
        self._last_loaded = datetime.now()

        logger.info(
            "Knowledge loaded: %d files, %d features, %d barriers, "
            "%d control_points, %d cases, %d terms",
            len(merged.source_files),
            len(merged.core_features),
            len(merged.tech_barriers),
            len(merged.control_points),
            len(merged.customer_cases),
            len(merged.key_terms),
        )
        return merged

    async def reload_if_changed(self) -> bool:
        """检测文件变更并自动热重载。

        Returns:
            True 如果检测到变更并已重载，否则 False
        """
        files = self._discover_files()
        if not files:
            return False

        new_hash = self._compute_hash(files)

        if new_hash != self._last_hash and self._last_hash:
            logger.info("Knowledge docs changed (%d files), reloading...", len(files))
            await self.load(force=True)
            return True

        return False

    def as_system_prompt(self) -> str:
        """快捷方法：返回知识库的 System Prompt 格式。

        在首次 load() 之前调用时返回空字符串。
        """
        if self._cache is None:
            logger.warning("Knowledge not loaded — returning empty prompt")
            return ""
        return self._cache.as_system_prompt()

    def as_scoring_prompt(self, article: dict[str, str] | None = None) -> str:
        """按 CLAUDE.md 市场部指引，拼接 PR/打分所需的文件原文。

        CLAUDE.md 市场部操作步骤:
          1. 先读 X-产品名/overview.md
          2. 再读 market-brief.md（热点类型、金句、传播角度）
          3. 匹配热点时加读 shared/hot-event-playbook.md
        """
        key_files = [
            "1-智能体身份安全/overview.md",
            "1-智能体身份安全/market-brief.md",
            "3-AI-BOM/overview.md",
            "3-AI-BOM/market-brief.md",
            "shared/hot-event-playbook.md",
        ]

        parts: list[str] = []
        for rel_path in key_files:
            fp = self.docs_dir / rel_path
            if fp.exists():
                try:
                    content = self._read_file(fp)
                    if len(content) > 2500:
                        content = content[:2500] + "\n\n... (truncated)"
                    parts.append(content)
                except Exception:
                    pass
        if not parts:
            return self.as_system_prompt()
        logger.info("Scoring prompt built from %d files (market role)", len(parts))
        return "\n\n---\n\n".join(parts)

    def as_keywords(self) -> list[str]:
        """返回关键词列表（用于快速匹配判断是否需要打分）。"""
        if self._cache is None:
            return []
        return self._cache.as_keywords()
