"""产品目录服务 - 稳定产品 ID 到知识库路径的映射。

职责：
- 列出已发布产品
- 校验产品 ID
- 将稳定 ID 映射到知识库根目录
- 返回产品别名、用途和摘要
- 生成目录版本/哈希

安全规则：
- 禁止使用客户端提交的相对路径直接读文件
- 禁止 ..、绝对路径或编码后的路径穿越
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger("backend.product_catalog")

Purpose = Literal["score", "draft", "chat"]


@dataclass(frozen=True)
class ProductEntry:
    """单个产品目录条目。"""

    product_id: str
    name: str
    description: str
    knowledge_root: str
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    allowed_purposes: tuple[Purpose, ...] = ("score", "draft", "chat")
    published: bool = True
    sort_order: int = 100


# ── 产品目录（与 agent-security-briefs/ 目录对应）──────────

_PRODUCTS: list[ProductEntry] = [
    ProductEntry(
        product_id="agent-identity-security",
        name="智能体身份安全",
        description="围绕智能体身份、认证、授权和运行时治理",
        knowledge_root="1-智能体身份安全",
        aliases=("智能体身份", "Agent Identity Security", "1-智能体身份安全"),
        keywords=(
            "身份认证", "身份治理", "授权", "最小权限", "权限边界",
            "凭证", "密钥", "单点登录", "委托授权", "反冒用", "SSO",
        ),
        allowed_purposes=("score", "draft", "chat"),
        published=True,
        sort_order=10,
    ),
    ProductEntry(
        product_id="agent-security",
        name="智能体安全",
        description="智能体安全平台解决方案，覆盖检测、防护和治理",
        knowledge_root="2-智能体安全",
        aliases=("智能体安全", "Agent Security", "2-智能体安全"),
        keywords=(
            "agent安全", "agent防护", "智能体防护", "智能体运行时",
            "agent runtime", "agent检测", "智能体检测", "运行时防护",
            "沙箱", "提示词注入", "数据泄露", "态势感知", "行为分析",
            "异常检测", "多智能体", "安全隔离", "智能体供应链",
            "智能体平台", "威胁检测", "威胁情报", "进程隔离",
        ),
        allowed_purposes=("score", "draft", "chat"),
        published=True,
        sort_order=20,
    ),
    ProductEntry(
        product_id="ai-bom",
        name="AI-BOM",
        description="AI 资产物料清单管理，覆盖 AI 组件供应链安全",
        knowledge_root="3-AI-BOM",
        aliases=("AI-BOM", "AI物料清单", "3-AI-BOM"),
        keywords=(
            "AI资产", "AI组件", "模型供应链", "AI供应链", "物料清单",
            "SBOM", "模型商店", "模型来源", "数据血缘", "资产台账",
            "依赖图谱", "供应链安全",
        ),
        allowed_purposes=("score", "draft", "chat"),
        published=True,
        sort_order=30,
    ),
    ProductEntry(
        product_id="agent-security-gateway",
        name="智能体安全网关",
        description="智能体安全网关，管控 Agent 流量和API",
        knowledge_root="4-智能体安全网关",
        aliases=("智能体安全网关", "Agent Security Gateway", "4-智能体安全网关"),
        keywords=("安全网关", "API网关", "流量管控", "agent网关"),
        allowed_purposes=("score", "draft", "chat"),
        published=False,  # 知识库文件不全，暂不发布
        sort_order=40,
    ),
    ProductEntry(
        product_id="ans",
        name="ANS",
        description="亚信安全网络服务",
        knowledge_root="5-ANS",
        aliases=("ANS", "亚信安全网络服务", "5-ANS"),
        keywords=("ANS", "亚信安全网络服务", "网络安全服务"),
        allowed_purposes=("score", "draft", "chat"),
        published=False,  # 知识库文件不全，暂不发布
        sort_order=50,
    ),
]


# ── 用途到文件的白名单规则 ────────────────────────────────

_PURPOSE_FILES: dict[Purpose, tuple[str, ...]] = {
    "score": (
        "overview.md",
        "market-brief.md",
    ),
    "draft": (
        "overview.md",
        "market-brief.md",
        "sales-brief.md",
    ),
    "chat": (
        "overview.md",
        "market-brief.md",
        "sales-brief.md",
    ),
}

# 全局排除的文件/目录
_EXCLUDED_PATHS = frozenset({
    "原始文档",
    "tasks.md",
    "qa-log.md",
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    ".git",
})

# 0-产品全景 中的全局参考文件
_SHARED_FILES = (
    "0-产品全景/overview.md",
    "0-产品全景/product-map.md",
    "0-产品全景/glossary.md",
)

# shared/ 下的共享参考文件
_SHARED_DIR_FILES = (
    "shared/hot-event-playbook.md",
    "shared/competitor-brief.md",
)


class ProductCatalogService:
    """产品目录服务。"""

    def __init__(self, knowledge_base_dir: str | Path = "/app/docs"):
        self._knowledge_base_dir = Path(knowledge_base_dir)
        self._products = {p.product_id: p for p in _PRODUCTS}

    def list_products(
        self,
        *,
        purpose: Purpose | None = None,
        published_only: bool = True,
    ) -> list[ProductEntry]:
        """列出产品。

        Args:
            purpose: 筛选用途（score/draft/chat），None 表示不限
            published_only: 是否只返回已发布产品
        """
        results = []
        for p in _PRODUCTS:
            if published_only and not p.published:
                continue
            if purpose is not None and purpose not in p.allowed_purposes:
                continue
            results.append(p)
        results.sort(key=lambda x: x.sort_order)
        return results

    def get_product(self, product_id: str) -> ProductEntry | None:
        """获取产品信息。"""
        return self._products.get(product_id)

    def validate_product_id(
        self,
        product_id: str,
        *,
        purpose: Purpose | None = None,
    ) -> ProductEntry:
        """校验产品 ID，返回产品信息。

        Raises:
            ValueError: 产品不存在、未发布或不支持当前用途
        """
        product = self._products.get(product_id)
        if product is None:
            raise ValueError(f"PRODUCT_UNAVAILABLE: 产品不存在: {product_id}")
        if not product.published:
            raise ValueError(f"PRODUCT_UNAVAILABLE: 产品未发布: {product_id}")
        if purpose is not None and purpose not in product.allowed_purposes:
            raise ValueError(
                f"PRODUCT_UNAVAILABLE: 产品 {product_id} 不支持用途: {purpose}"
            )
        return product

    def validate_product_ids(
        self,
        product_ids: list[str],
        *,
        purpose: Purpose | None = None,
        max_count: int = 5,
    ) -> list[ProductEntry]:
        """批量校验产品 ID。"""
        if len(product_ids) > max_count:
            raise ValueError(
                f"INVALID_PRODUCT_SELECTION: 最多选择 {max_count} 个产品"
            )
        if not product_ids:
            raise ValueError("INVALID_PRODUCT_SELECTION: 未选择产品")
        return [
            self.validate_product_id(pid, purpose=purpose) for pid in product_ids
        ]

    def get_knowledge_path(self, product_id: str) -> Path:
        """获取产品知识库根目录的绝对路径。"""
        product = self.validate_product_id(product_id)
        return self._knowledge_base_dir / product.knowledge_root

    def get_purpose_files(
        self,
        product_id: str,
        purpose: Purpose,
    ) -> list[Path]:
        """获取产品在指定用途下的知识文件路径列表。"""
        self.validate_product_id(product_id, purpose=purpose)
        product_dir = self.get_knowledge_path(product_id)
        file_names = _PURPOSE_FILES.get(purpose, ())

        paths: list[Path] = []
        for fname in file_names:
            fp = product_dir / fname
            if fp.exists():
                paths.append(fp)

        return paths

    def get_shared_files(self, purpose: Purpose) -> list[Path]:
        """获取全局共享参考文件。"""
        paths: list[Path] = []
        all_shared = list(_SHARED_FILES) + list(_SHARED_DIR_FILES)
        for rel_path in all_shared:
            fp = self._knowledge_base_dir / rel_path
            if fp.exists():
                paths.append(fp)
        return paths

    @staticmethod
    def is_path_safe(path_str: str) -> bool:
        """检查路径是否安全（拒绝 ..、绝对路径等）。"""
        if not path_str:
            return False
        if ".." in path_str:
            return False
        if os.path.isabs(path_str) or path_str.startswith("/"):
            return False
        # 检查 URL 编码的路径穿越
        if "%2e" in path_str.lower() or "%2f" in path_str.lower():
            return False
        # 检查排除的路径
        return all(excluded not in path_str for excluded in _EXCLUDED_PATHS)

    def catalog_hash(self) -> str:
        """生成产品目录的版本哈希。"""
        parts = []
        for p in sorted(_PRODUCTS, key=lambda x: x.product_id):
            parts.append(f"{p.product_id}:{p.published}:{p.sort_order}")
        content = "|".join(parts)
        return "sha256:" + hashlib.sha256(content.encode()).hexdigest()

    def to_api_response(
        self,
        *,
        purpose: Purpose | None = None,
    ) -> dict:
        """生成 API 响应。"""
        products = self.list_products(purpose=purpose, published_only=True)
        return {
            "items": [
                {
                    "product_id": p.product_id,
                    "name": p.name,
                    "description": p.description,
                    "published": p.published,
                    "available_for": list(p.allowed_purposes),
                }
                for p in products
            ],
            "knowledge_hash": self.catalog_hash(),
        }
