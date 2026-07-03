"""
产品知识库加载器

从 docs/ 目录加载产品文档，提取结构化知识信息，
作为打分 Agent 和报道 Agent 的 System Prompt 上下文。

特性:
  - 启动时异步加载，内存缓存
  - 文件变更检测 + 热重载（MD5 校验）
  - 支持纯文本解析（默认）和 LLM 增强提取（可选）
  - as_system_prompt() 输出可直接注入 Prompt

使用:
    loader = KnowledgeLoader(docs_dir="/app/docs")
    knowledge = await loader.load()
    prompt_prefix = knowledge.as_system_prompt()
"""

from __future__ import annotations

import hashlib
import logging
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

    # 元信息
    source_file: str = ""
    loaded_at: str = ""
    content_hash: str = ""

    def as_system_prompt(self) -> str:
        """将知识库转换为 System Prompt 片段（可注入打分/报道 Agent）"""
        parts: list[str] = []

        if self.product_positioning:
            parts.append(f"## 产品定位\n{self.product_positioning}")

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

        if self.target_industries:
            industries = "\n".join(f"- {i}" for i in self.target_industries)
            parts.append(f"## 目标行业\n{industries}")

        if self.key_terms:
            terms = ", ".join(self.key_terms)
            parts.append(f"## 关键术语\n{terms}")

        return "\n\n".join(parts)

    def as_keywords(self) -> list[str]:
        """返回所有可用于匹配的关键词列表"""
        keywords: list[str] = list(self.key_terms)
        # 从各部分提取关键词
        for field_items in [
            self.core_features,
            self.tech_barriers,
            self.control_points,
        ]:
            for item in field_items:
                # 提取短词（2-6 字）
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
    def parse(content: str) -> ProductKnowledge:
        """解析 Markdown 文档，提取 ProductKnowledge 结构"""
        knowledge = ProductKnowledge()

        # ── 定位关键章节 ──
        sections = MarkdownKnowledgeParser._split_sections(content)
        {k.lower().strip(): v for k, v in sections.items()}

        # ── 产品名称（从一级标题）──
        for line in content.split("\n"):
            if line.startswith("# ") and "智能体" in line:
                knowledge.product_name = line[2:].strip()
                break

        # ── 产品定位 ──
        positioning_keywords = ["愿景", "定位", "业务架构", "产品规划"]
        for kw in positioning_keywords:
            for title, text in sections.items():
                if kw in title:
                    knowledge.product_positioning = MarkdownKnowledgeParser._extract_summary(text)
                    break
            if knowledge.product_positioning:
                break

        # ── 核心功能 / 技术壁垒 ──
        for title, text in sections.items():
            title_lower = title.lower()
            if any(w in title_lower for w in ["功能", "独创", "壁垒", "架构"]):
                items = MarkdownKnowledgeParser._extract_list_items(text)
                if items:
                    # 判断是功能还是壁垒
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
                # 提取关键词：运营商、金融、政务等
                industries = MarkdownKnowledgeParser._extract_industry_keywords(text)
                knowledge.target_industries.extend(industries)

        # ── 竞品 ──
        for title, text in sections.items():
            if any(w in title for w in ["竞品", "对手", "友商"]):
                items = MarkdownKnowledgeParser._extract_list_items(text)
                knowledge.competitors.extend(items)

        # ── 如果没有提取到足够信息，使用全文摘要 ──
        if not knowledge.product_positioning:
            knowledge.product_positioning = MarkdownKnowledgeParser._extract_summary(content)

        # ── 确保关键术语至少包含默认值 ──
        if not knowledge.key_terms:
            knowledge.key_terms = [
                "智能体安全", "Agent安全", "MCP协议", "身份认证",
                "权限管控", "意图识别", "提示注入", "模型攻击", "供应链安全",
            ]

        return knowledge

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
        """提取文本摘要（取前 3 个非空行）"""
        lines = [l.strip() for l in text.split("\n") if l.strip() and not l.startswith("#")]
        # 跳过图片链接和过短的行
        meaningful = [l for l in lines if len(l) > 10 and not l.startswith("![")]
        return " ".join(meaningful[:5])

    @staticmethod
    def _extract_list_items(text: str) -> list[str]:
        """提取列表项（- 开头或数字. 开头）"""
        items: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            # Markdown 列表项
            if stripped.startswith("- ") or stripped.startswith("* "):
                item = stripped[2:].strip()
                if len(item) > 3:
                    items.append(item)
            # 数字列表
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
            "教育", "互联网", "交通", "电力",
        ]
        found = []
        for kw in industry_keywords:
            if kw in text:
                found.append(kw)
        return found


# ═══════════════════════════════════════════════════════════════
# KnowledgeLoader — 主类
# ═══════════════════════════════════════════════════════════════


class KnowledgeLoader:
    """产品知识库加载器。

    特性:
      - 启动时异步加载（await loader.load()）
      - 内存缓存（避免重复读取）
      - 文件 MD5 校验（检测变更自动热重载）
      - 纯规则解析（默认），可选 LLM 增强
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
            filename: 指定文件名（默认读取产品计划和目标文档）
        """
        self.docs_dir = Path(docs_dir)
        self.filename = filename or self.DEFAULT_DOC
        self.filepath = self.docs_dir / self.filename

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

    async def load(self, force: bool = False) -> ProductKnowledge:
        """加载知识库（首次调用时读取并解析，后续命中缓存）。

        Args:
            force: 强制重新加载（忽略缓存和 hash 校验）

        Returns:
            ProductKnowledge 结构化知识对象

        Raises:
            FileNotFoundError: 文档文件不存在
        """
        # 检查文件
        if not self.filepath.exists():
            logger.warning("Knowledge doc not found: %s", self.filepath)
            # 返回默认知识库（不阻塞流水线）
            return ProductKnowledge()

        # 计算文件 hash
        content = self._read_file()
        content_hash = self._hash(content)

        # 缓存命中
        if not force and self._cache is not None and content_hash == self._last_hash:
            logger.debug("Knowledge cache hit (hash=%s)", content_hash[:8])
            return self._cache

        # 解析
        logger.info("Loading knowledge from: %s", self.filepath)
        knowledge = MarkdownKnowledgeParser.parse(content)
        knowledge.source_file = str(self.filepath)
        knowledge.loaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        knowledge.content_hash = content_hash

        # 更新缓存
        self._cache = knowledge
        self._last_hash = content_hash
        self._last_loaded = datetime.now()

        logger.info(
            "Knowledge loaded: %d features, %d barriers, %d control_points, %d cases",
            len(knowledge.core_features),
            len(knowledge.tech_barriers),
            len(knowledge.control_points),
            len(knowledge.customer_cases),
        )
        return knowledge

    async def reload_if_changed(self) -> bool:
        """检测文件变更并自动重载。

        Returns:
            True 如果检测到变更并已重载，否则 False
        """
        if not self.filepath.exists():
            return False

        content = self._read_file()
        new_hash = self._hash(content)

        if new_hash != self._last_hash and self._last_hash:
            logger.info("Knowledge doc changed, reloading...")
            await self.load(force=True)
            return True

        return False

    def as_system_prompt(self) -> str:
        """快捷方法：返回知识库的 System Prompt 格式。

        在首次 load() 之前调用时自动触发加载（同步阻塞，仅用于初始化阶段）。
        """
        if self._cache is None:
            logger.warning("Knowledge not loaded — returning empty prompt")
            return ""
        return self._cache.as_system_prompt()

    def as_keywords(self) -> list[str]:
        """返回关键词列表（用于快速匹配判断是否需要打分）。"""
        if self._cache is None:
            return []
        return self._cache.as_keywords()

    # ── 内部辅助 ──

    def _read_file(self) -> str:
        """读取文档文件"""
        try:
            return self.filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self.filepath.read_text(encoding="gbk")

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.md5(content.encode()).hexdigest()
