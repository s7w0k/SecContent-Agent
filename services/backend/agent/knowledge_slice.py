"""KnowledgeSliceResolver - 按产品 ID 和用途构造知识上下文切片。

不同用途的默认文件优先级：
- score: overview.md, market-brief.md, 产品全景必要片段
- draft: overview.md, market-brief.md, sales-brief.md
- chat: 与草稿快照一致，必要时追加产品摘要

统一排除：原始文档/、tasks.md、qa-log.md、CLAUDE.md、AGENTS.md、未发布草稿
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agent.product_catalog import ProductCatalogService

logger = logging.getLogger("backend.knowledge_slice")

Purpose = Literal["score", "draft", "chat"]

# 每个文件的最大字符数
MAX_FILE_CHARS = 2500

# 总字符预算
DEFAULT_MAX_CHARS = 8000


@dataclass(frozen=True)
class KnowledgeSlice:
    """知识切片结果。"""

    content: str
    product_ids: list[str] = field(default_factory=list)
    source_document_ids: list[str] = field(default_factory=list)
    content_hash: str = ""
    char_count: int = 0
    truncated: bool = False


class KnowledgeSliceResolver:
    """按产品 ID 和用途解析知识切片。"""

    def __init__(
        self,
        knowledge_base_dir: str | Path = "/app/docs",
        max_chars: int = DEFAULT_MAX_CHARS,
        db=None,
    ):
        self._catalog = ProductCatalogService(knowledge_base_dir)
        self._knowledge_base_dir = Path(knowledge_base_dir)
        self._max_chars = max_chars
        self._db = db

    async def resolve(
        self,
        *,
        purpose: Purpose,
        product_ids: list[str] | None = None,
        include_shared: bool = True,
        max_chars: int | None = None,
        user_id: str | None = None,
    ) -> KnowledgeSlice:
        """解析知识切片。

        Args:
            purpose: 用途（score/draft/chat）
            product_ids: 产品 ID 列表，空列表或 None 表示不关联产品
            include_shared: 是否包含全局共享参考文件
            max_chars: 总字符预算
            user_id: 用户 ID（传入时合并用户级知识）

        Returns:
            KnowledgeSlice
        """
        budget = max_chars or self._max_chars
        parts: list[str] = []
        source_docs: list[str] = []
        truncated = False

        if product_ids:
            # 区分全局产品和用户产品
            global_product_ids: list[str] = []
            user_product_ids: list[str] = []

            if user_id and self._db is not None:
                # 查询用户产品，区分全局和用户级
                user_product_docs = await self._db["user_products"].find(
                    {"user_id": user_id, "product_id": {"$in": product_ids}, "enabled": True}
                ).to_list(length=50)
                user_pids_set = {doc["product_id"] for doc in user_product_docs}
                for pid in product_ids:
                    if pid in user_pids_set:
                        user_product_ids.append(pid)
                    else:
                        global_product_ids.append(pid)
            else:
                global_product_ids = list(product_ids)

            # 解析全局产品知识
            if global_product_ids:
                validated = self._catalog.validate_product_ids(
                    global_product_ids, purpose=purpose
                )

                for product in validated:
                    file_paths = self._catalog.get_purpose_files(
                        product.product_id, purpose
                    )
                    for fp in file_paths:
                        content = self._read_file(fp)
                        if not content:
                            continue
                        rel_path = str(fp.relative_to(self._knowledge_base_dir)).replace("\\", "/")
                        source_docs.append(rel_path)

                        if len(content) > MAX_FILE_CHARS:
                            content = content[:MAX_FILE_CHARS] + "\n\n... (truncated)"

                        parts.append(f"### {product.name} - {fp.name}\n\n{content}")

                        if sum(len(p) for p in parts) > budget:
                            truncated = True
                            break

                    if truncated:
                        break

            # 解析用户产品知识（从 MongoDB 读取）
            if user_product_ids and not truncated:
                # 获取产品名称
                product_name_map: dict[str, str] = {}
                for doc in user_product_docs:
                    product_name_map[doc["product_id"]] = doc.get("name", doc["product_id"])

                # 获取用户知识条目
                entries = await self._db["user_knowledge_entries"].find({
                    "user_id": user_id,
                    "product_id": {"$in": user_product_ids},
                    "enabled": True,
                }).sort("sort_order", 1).to_list(length=100)

                # doc_type 优先级

                for entry in entries:
                    entry.pop("_id", None)
                    pid = entry.get("product_id", "")
                    pname = product_name_map.get(pid, pid)
                    title = entry.get("title", "")
                    doc_type = entry.get("doc_type", "custom")
                    content = entry.get("content", "")
                    if not content.strip():
                        continue

                    if len(content) > MAX_FILE_CHARS:
                        content = content[:MAX_FILE_CHARS] + "\n\n... (truncated)"

                    parts.append(f"### {pname} - {title}（用户级·{doc_type}）\n\n{content}")
                    source_docs.append(f"user:{pid}:{entry.get('entry_id', '')}")

                    if sum(len(p) for p in parts) > budget:
                        truncated = True
                        break

        # 追加共享参考文件
        if include_shared and not truncated:
            shared_paths = self._catalog.get_shared_files(purpose)
            for fp in shared_paths:
                content = self._read_file(fp)
                if not content:
                    continue
                rel_path = str(fp.relative_to(self._knowledge_base_dir)).replace("\\", "/")
                source_docs.append(rel_path)

                if len(content) > MAX_FILE_CHARS:
                    content = content[:MAX_FILE_CHARS] + "\n\n... (truncated)"

                parts.append(f"### 共享参考 - {fp.name}\n\n{content}")

                if sum(len(p) for p in parts) > budget:
                    truncated = True
                    break

        content = "\n\n---\n\n".join(parts) if parts else ""

        # 追加用户对全局产品的补充知识条目（product_scope=global 的用户条目）
        if user_id and self._db is not None and not truncated:
            supplement_query: dict = {
                "user_id": user_id,
                "enabled": True,
                "product_scope": "global",
            }
            if global_product_ids:
                supplement_query["product_id"] = {"$in": global_product_ids}

            supplement_entries = await self._db["user_knowledge_entries"].find(
                supplement_query
            ).sort("sort_order", 1).to_list(length=100)

            supplement_parts: list[str] = []
            for entry in supplement_entries:
                entry.pop("_id", None)
                title = entry.get("title", "")
                doc_type = entry.get("doc_type", "custom")
                entry_content = entry.get("content", "")
                if not entry_content.strip():
                    continue
                if len(entry_content) > MAX_FILE_CHARS:
                    entry_content = entry_content[:MAX_FILE_CHARS] + "\n\n... (truncated)"
                supplement_parts.append(
                    f"### {title}（用户级补充·{doc_type}）\n\n{entry_content}"
                )
                source_docs.append(f"user:supplement:{entry.get('entry_id', '')}")

            if supplement_parts:
                supplement_content = "\n\n---\n\n".join(supplement_parts)
                content = (
                    content + f"\n\n---\n\n## 用户级补充知识\n\n{supplement_content}"
                    if content
                    else supplement_content
                )

        # 计算最终哈希（包含用户级内容）
        content_hash = self._compute_hash(content, product_ids or [])

        return KnowledgeSlice(
            content=content,
            product_ids=product_ids or [],
            source_document_ids=source_docs,
            content_hash=content_hash,
            char_count=len(content),
            truncated=truncated,
        )

    def resolve_none(self) -> KnowledgeSlice:
        """none 模式：不注入任何产品知识。"""
        return KnowledgeSlice(
            content="",
            product_ids=[],
            source_document_ids=[],
            content_hash="sha256:none",
            char_count=0,
            truncated=False,
        )

    def _read_file(self, filepath: Path) -> str:
        """读取文件，自动检测编码。"""
        try:
            content = filepath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = filepath.read_text(encoding="gbk")
            except Exception:
                logger.warning("Failed to read file: %s", filepath)
                return ""
        except Exception:
            logger.warning("Failed to read file: %s", filepath)
            return ""
        return content.strip()

    @staticmethod
    def _compute_hash(content: str, product_ids: list[str]) -> str:
        """计算知识切片的哈希。"""
        h = hashlib.sha256()
        h.update(content.encode("utf-8"))
        h.update(b"|")
        h.update(",".join(sorted(product_ids)).encode("utf-8"))
        return "sha256:" + h.hexdigest()
