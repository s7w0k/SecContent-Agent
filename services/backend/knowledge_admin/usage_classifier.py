"""文件用途分类器 - 标注知识库文件的角色和评分参与状态。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent.knowledge import _is_scoring_relevant

# V2 打分直接拼接的 5 个核心文件（相对路径，正斜杠）
DIRECT_SCORING_FILES: frozenset[str] = frozenset(
    {
        "1-智能体身份安全/overview.md",
        "1-智能体身份安全/market-brief.md",
        "3-AI-BOM/overview.md",
        "3-AI-BOM/market-brief.md",
        "shared/hot-event-playbook.md",
    }
)

# 第一版只读路径（不允许编辑）
_READ_ONLY_DIRS: frozenset[str] = frozenset(
    {
        "_index",
        "skills",
        "原始文档",
    }
)

_READ_ONLY_FILES: frozenset[str] = frozenset(
    {
        "CLAUDE.md",
        "AGENTS.md",
        "qa-log.md",
        "README.md",
    }
)


class UsageClassifier:
    """标注知识库文件的用途、评分参与状态和编辑权限。"""

    @staticmethod
    def classify(relative_path: str) -> str:
        """返回文件用途分类标签。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        name = Path(normalized).name
        parts = normalized.split("/")

        # 入口与路由
        if name in ("CLAUDE.md", "AGENTS.md", "README.md"):
            return "entry_router"
        if normalized == "_index/folder-routing.md":
            return "folder_router"

        # 角色工作流
        if parts[0] == "skills":
            return "role_workflow"

        # 产品全景
        if parts[0] == "0-产品全景":
            return "product_map"

        # 产品目录下的标准 brief
        if name == "overview.md":
            return "product_fact"
        if name == "market-brief.md":
            return "market_brief"
        if name == "sales-brief.md":
            return "sales_brief"
        if name == "architecture-brief.md":
            return "architecture_brief"
        if name == "tasks.md":
            return "task_status"

        # 共享知识
        if parts[0] == "shared":
            return "shared_fact"

        # 原始文档
        if parts[0] == "原始文档":
            return "raw_source"

        # 海外版
        if parts[0] == "海外版":
            return "overseas"

        # 维护日志
        if name == "qa-log.md":
            return "maintenance_log"

        return "other"

    @staticmethod
    def is_loader_relevant(root_dir: Path, relative_path: str) -> bool:
        """判断文件是否被现有 KnowledgeLoader 发现。"""
        normalized = relative_path.replace("\\", "/")
        filepath = root_dir / normalized
        return _is_scoring_relevant(root_dir, filepath)

    @staticmethod
    def is_direct_scoring_prompt(relative_path: str) -> bool:
        """判断文件是否属于 V2 打分直接拼接的 5 个核心文件。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        return normalized in DIRECT_SCORING_FILES

    @staticmethod
    def is_editable(relative_path: str) -> bool:
        """判断文件是否允许管理员编辑。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        parts = normalized.split("/")

        if parts[0] in _READ_ONLY_DIRS:
            return False
        return Path(normalized).name not in _READ_ONLY_FILES

    @staticmethod
    def is_protected_path(relative_path: str) -> bool:
        """判断文件是否为受保护路径（允许编辑内容，禁止移动/改名/删除）。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        return normalized in DIRECT_SCORING_FILES

    @classmethod
    def get_file_metadata(
        cls,
        relative_path: str,
        content: str,
        *,
        root_dir: Path | None = None,
    ) -> dict:
        """返回文件的完整元数据。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        content_hash = f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"

        return {
            "document_id": cls.get_document_id(normalized),
            "name": Path(normalized).name,
            "relative_path": normalized,
            "knowledge_role": cls.classify(normalized),
            "loader_relevant": cls.is_loader_relevant(root_dir, normalized) if root_dir else False,
            "direct_scoring_prompt": cls.is_direct_scoring_prompt(normalized),
            "editable": cls.is_editable(normalized),
            "protected_path": cls.is_protected_path(normalized),
            "content_hash": content_hash,
        }

    @staticmethod
    def get_document_id(relative_path: str) -> str:
        """根据规范化相对路径生成文档 ID。"""
        normalized = relative_path.replace("\\", "/").strip("/")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def get_usage_legend() -> list[dict]:
        """返回用途分类说明，供前端绘制图例。"""
        return [
            {"role": "entry_router", "label": "入口路由", "description": "AI入口与身份路由"},
            {"role": "folder_router", "label": "目录路由", "description": "根据问题选择产品目录"},
            {
                "role": "role_workflow",
                "label": "角色工作流",
                "description": "定义回答方式，非产品事实",
            },
            {"role": "product_map", "label": "产品全景", "description": "产品矩阵、关系和术语"},
            {"role": "product_fact", "label": "产品事实", "description": "产品定位和能力事实"},
            {"role": "market_brief", "label": "市场简报", "description": "热点和传播角度"},
            {"role": "sales_brief", "label": "销售简报", "description": "售前卖点和FAQ"},
            {
                "role": "architecture_brief",
                "label": "架构简报",
                "description": "技术架构，当前评分排除",
            },
            {
                "role": "task_status",
                "label": "任务状态",
                "description": "产品状态和计划，当前评分排除",
            },
            {"role": "shared_fact", "label": "共享知识", "description": "竞品、热点和技术总图"},
            {"role": "raw_source", "label": "原始文档", "description": "brief不足时兜底"},
            {"role": "overseas", "label": "海外版", "description": "海外版知识，当前评分排除"},
            {"role": "maintenance_log", "label": "维护日志", "description": "未命中问题记录"},
            {"role": "other", "label": "其他", "description": "未分类文件"},
        ]
