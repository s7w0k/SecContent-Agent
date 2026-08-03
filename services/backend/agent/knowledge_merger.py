"""知识分层合并服务 - 全局知识 + 用户级知识合并。

从 user_knowledge_entries 集合读取用户的知识条目，
按 product_id 分组，按 doc_type 排序，追加到全局知识之后。

合并规则：
1. 用户知识条目按 product_id 过滤（只包含选中产品的条目）
2. 按 doc_type 分组：overview > market-brief > sales-brief > custom
3. 只包含 enabled=True 的条目
4. 追加到全局知识之后，以"用户级补充知识"标题分隔
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("backend.knowledge_merger")

# doc_type 排序优先级
_DOC_TYPE_ORDER = {"overview": 0, "market-brief": 1, "sales-brief": 2, "custom": 3}


@dataclass(frozen=True)
class MergedKnowledge:
    """合并后的知识。"""

    content: str
    global_content: str = ""
    user_content: str = ""
    source_document_ids: list[str] = field(default_factory=list)
    content_hash: str = ""
    char_count: int = 0
    user_entry_count: int = 0


class KnowledgeMerger:
    """知识分层合并服务。"""

    def __init__(self, db, catalog=None):
        self._db = db

    async def merge_for_user(
        self,
        user_id: str,
        product_ids: list[str],
        global_content: str,
        global_source_ids: list[str],
    ) -> MergedKnowledge:
        """为用户合并全局知识 + 用户级知识。

        Args:
            user_id: 用户 ID
            product_ids: 选中的产品 ID 列表（全局 + 用户级）
            global_content: 全局知识内容
            global_source_ids: 全局知识来源文件列表

        Returns:
            MergedKnowledge
        """
        # 获取用户启用的知识条目
        query: dict = {"user_id": user_id, "enabled": True}
        if product_ids:
            query["product_id"] = {"$in": product_ids}

        docs = await self._db["user_knowledge_entries"].find(query).to_list(length=200)

        # 获取用户产品信息（用于标题展示）
        product_names: dict[str, str] = {}
        if product_ids:
            user_product_docs = await self._db["user_products"].find({
                "user_id": user_id,
                "product_id": {"$in": product_ids},
            }).to_list(length=50)
            for doc in user_product_docs:
                doc.pop("_id", None)
                product_names[doc["product_id"]] = doc.get("name", doc["product_id"])

        # 按产品分组，每个产品内按 doc_type 排序
        entries_by_product: dict[str, list[dict]] = {}
        for doc in docs:
            doc.pop("_id", None)
            pid = doc.get("product_id", "")
            entries_by_product.setdefault(pid, []).append(doc)

        # 构建用户级内容
        user_parts: list[str] = []
        user_source_ids: list[str] = []

        for pid, entries in entries_by_product.items():
            # 按doc_type排序
            entries.sort(key=lambda e: _DOC_TYPE_ORDER.get(e.get("doc_type", "custom"), 99))
            product_name = product_names.get(pid, pid)

            for entry in entries:
                title = entry.get("title", "")
                doc_type = entry.get("doc_type", "custom")
                content = entry.get("content", "")
                if not content.strip():
                    continue
                user_parts.append(
                    f"### {product_name} - {title}（用户级·{doc_type}）\n\n{content}"
                )
                user_source_ids.append(f"user:{pid}:{entry.get('entry_id', '')}")

        user_content = "\n\n---\n\n".join(user_parts) if user_parts else ""

        # 合并：全局 + 用户级追加
        if user_content:
            merged = (
                global_content + f"\n\n---\n\n## 用户级补充知识\n\n{user_content}"
                if global_content
                else user_content
            )
        else:
            merged = global_content

        # 计算合并哈希
        h = hashlib.sha256()
        h.update(global_content.encode("utf-8"))
        h.update(b"|user|")
        h.update(user_content.encode("utf-8"))
        merged_hash = "sha256:" + h.hexdigest()

        return MergedKnowledge(
            content=merged,
            global_content=global_content,
            user_content=user_content,
            source_document_ids=global_source_ids + user_source_ids,
            content_hash=merged_hash,
            char_count=len(merged),
            user_entry_count=len(docs),
        )

    async def get_user_products_for_matching(self, user_id: str) -> list[dict]:
        """获取用户级产品信息（用于产品自动匹配）。

        Returns:
            产品列表，每项包含 product_id, name, aliases, keywords
        """
        docs = await self._db["user_products"].find({
            "user_id": user_id,
            "enabled": True,
        }).to_list(length=50)
        results = []
        for doc in docs:
            doc.pop("_id", None)
            results.append({
                "product_id": doc["product_id"],
                "name": doc.get("name", ""),
                "aliases": doc.get("aliases", []),
                "keywords": doc.get("keywords", []),
            })
        return results
