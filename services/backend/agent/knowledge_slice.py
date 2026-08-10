"""KnowledgeSliceResolver - 按产品 ID 和用途构造知识上下文切片。

不同用途的默认文件优先级：
- score: overview.md, market-brief.md（required）
- draft: overview.md, market-brief.md（required）+ sales-brief.md（optional）
- chat: overview.md（required）+ market-brief/sales-brief/custom（optional）

统一排除：原始文档/、tasks.md、qa-log.md、CLAUDE.md、AGENTS.md、未发布草稿

阶段二 Step 3 变更：
  - PURPOSE_DOC_TYPES 按 purpose 分层（required/optional）
  - 用户级知识稳定排序：required → sort_order → updated_at → source_id
  - required 缺失记录 knowledge_missing，不推断产品能力
  - 预算在追加前计算，不允许先超额再标 truncated
  - 缺失/非法 doc_type 读取时映射 custom（不写回数据库）
  - 用户知识严格按 user_id 隔离
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

# 合法 doc_type（缺失/非法 → custom）
VALID_DOC_TYPES = ("overview", "market-brief", "sales-brief", "custom")

# purpose → doc_type 分层（阶段二 Step 3）
PURPOSE_DOC_TYPES: dict[Purpose, dict[str, list[str]]] = {
    "score": {"required": ["overview", "market-brief"], "optional": []},
    "draft": {
        "required": ["overview", "market-brief"],
        "optional": ["sales-brief", "custom"],
    },
    "chat": {
        "required": ["overview"],
        "optional": ["market-brief", "sales-brief", "custom"],
    },
}


@dataclass(frozen=True)
class KnowledgeSlice:
    """知识切片结果。"""

    content: str
    product_ids: list[str] = field(default_factory=list)
    source_document_ids: list[str] = field(default_factory=list)
    content_hash: str = ""
    char_count: int = 0
    truncated: bool = False
    knowledge_missing: list[str] = field(default_factory=list)


def _normalize_doc_type(doc_type: str | None) -> str:
    """缺失/非法 doc_type 映射为 custom（仅读取视图，不写回）。"""
    return doc_type if doc_type in VALID_DOC_TYPES else "custom"


def _doc_type_rank(doc_type: str, purpose: Purpose) -> tuple[int, int]:
    """计算 doc_type 排序键：required 优先（按 required 列表序），optional 次之。"""
    config = PURPOSE_DOC_TYPES.get(purpose, {"required": [], "optional": []})
    required = config.get("required", [])
    optional = config.get("optional", [])
    if doc_type in required:
        return (0, required.index(doc_type))
    if doc_type in optional:
        return (1, optional.index(doc_type))
    return (2, 0)


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
            user_id: 用户 ID（传入时合并用户级知识，严格按 user_id 隔离）

        Returns:
            KnowledgeSlice
        """
        budget = max_chars or self._max_chars
        parts: list[str] = []
        source_docs: list[str] = []
        knowledge_missing: list[str] = []
        truncated = False

        def _append(part: str, source: str) -> bool:
            """预算在追加前计算；返回 False 表示预算已满（调用方停止）。"""
            nonlocal truncated
            if sum(len(p) for p in parts) + len(part) > budget:
                truncated = True
                return False
            parts.append(part)
            source_docs.append(source)
            return True

        if product_ids:
            # 区分全局产品和用户产品
            global_product_ids: list[str] = []
            user_product_ids: list[str] = []
            user_product_docs: list[dict] = []

            if user_id and self._db is not None:
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

            # 解析全局产品知识（required 文件缺失时记录 knowledge_missing）
            if global_product_ids:
                validated = self._catalog.validate_product_ids(
                    global_product_ids, purpose=purpose
                )

                for product in validated:
                    file_paths = self._catalog.get_purpose_files(
                        product.product_id, purpose
                    )
                    required_files = self._required_files_for_purpose(purpose)
                    loaded_names = {fp.name for fp in file_paths}
                    for req in required_files:
                        if req not in loaded_names:
                            knowledge_missing.append(
                                f"global_product:{product.product_id}:missing:{req}"
                            )

                    for fp in file_paths:
                        content = self._read_file(fp)
                        if not content:
                            continue
                        rel_path = str(fp.relative_to(self._knowledge_base_dir)).replace("\\", "/")

                        if len(content) > MAX_FILE_CHARS:
                            content = content[:MAX_FILE_CHARS] + "\n\n... (truncated)"

                        if not _append(
                            f"### {product.name} - {fp.name}\n\n{content}",
                            rel_path,
                        ):
                            break

                    if truncated:
                        break

            # 解析用户产品知识（从 MongoDB 读取，按 purpose/doc_type 分层）
            if user_product_ids and not truncated:
                product_name_map: dict[str, str] = {}
                for doc in user_product_docs:
                    product_name_map[doc["product_id"]] = doc.get("name", doc["product_id"])

                entries = await self._db["user_knowledge_entries"].find({
                    "user_id": user_id,
                    "product_id": {"$in": user_product_ids},
                    "enabled": True,
                }).sort("sort_order", 1).to_list(length=200)

                # 按产品分组，每组按 doc_type 分层 + 稳定排序
                for pid in user_product_ids:
                    pid_entries = [
                        e for e in entries if e.get("product_id") == pid
                    ]
                    missing = await self._resolve_user_product(
                        pid,
                        pid_entries,
                        product_name_map,
                        purpose,
                        _append,
                    )
                    knowledge_missing.extend(missing)

        # 追加共享参考文件
        if include_shared and not truncated:
            shared_paths = self._catalog.get_shared_files(purpose)
            for fp in shared_paths:
                content = self._read_file(fp)
                if not content:
                    continue
                rel_path = str(fp.relative_to(self._knowledge_base_dir)).replace("\\", "/")

                if len(content) > MAX_FILE_CHARS:
                    content = content[:MAX_FILE_CHARS] + "\n\n... (truncated)"

                if not _append(f"### 共享参考 - {fp.name}\n\n{content}", rel_path):
                    break

        content = "\n\n---\n\n".join(parts) if parts else ""

        # 追加用户对全局产品的补充知识条目（product_scope=global 的 optional 条目）
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
            supplement_sources: list[str] = []
            # 稳定排序：sort_order → updated_at → entry_id
            def _sup_key(e: dict):
                return (
                    int(e.get("sort_order", 100)),
                    str(e.get("updated_at", "")),
                    str(e.get("entry_id", "")),
                )

            for entry in sorted(supplement_entries, key=_sup_key):
                title = entry.get("title", "")
                doc_type = _normalize_doc_type(entry.get("doc_type", "custom"))
                entry_content = entry.get("content", "")
                if not entry_content.strip():
                    continue
                if len(entry_content) > MAX_FILE_CHARS:
                    entry_content = entry_content[:MAX_FILE_CHARS] + "\n\n... (truncated)"
                part = f"### {title}（用户级补充·{doc_type}）\n\n{entry_content}"
                if sum(len(p) for p in parts) + sum(len(p) for p in supplement_parts) + len(part) > budget:
                    truncated = True
                    break
                supplement_parts.append(part)
                supplement_sources.append(f"user:supplement:{entry.get('entry_id', '')}")

            if supplement_parts:
                supplement_content = "\n\n---\n\n".join(supplement_parts)
                content = (
                    content + f"\n\n---\n\n## 用户级补充知识\n\n{supplement_content}"
                    if content
                    else supplement_content
                )
                source_docs.extend(supplement_sources)

        # 计算最终哈希（包含用户级内容）
        content_hash = self._compute_hash(content, product_ids or [])

        return KnowledgeSlice(
            content=content,
            product_ids=product_ids or [],
            source_document_ids=source_docs,
            content_hash=content_hash,
            char_count=len(content),
            truncated=truncated,
            knowledge_missing=knowledge_missing,
        )

    async def _resolve_user_product(
        self,
        pid: str,
        entries: list[dict],
        product_name_map: dict[str, str],
        purpose: Purpose,
        append,
    ) -> list[str]:
        """解析单个用户产品的知识条目：doc_type 分层 + 稳定排序 + 缺失语义。"""
        config = PURPOSE_DOC_TYPES.get(purpose, {"required": [], "optional": []})
        required_types = config.get("required", [])
        optional_types = config.get("optional", [])

        # 规范化 doc_type
        normed: list[tuple[str, dict]] = []
        for entry in entries:
            entry.pop("_id", None)
            doc_type = _normalize_doc_type(entry.get("doc_type", "custom"))
            content = entry.get("content", "")
            if not content.strip():
                continue
            normed.append((doc_type, entry))

        # 稳定排序：required（按 required 序）→ optional → sort_order → updated_at → source_id
        def _sort_key(item: tuple[str, dict]) -> tuple:
            doc_type, entry = item
            rank = _doc_type_rank(doc_type, purpose)
            return (
                rank[0],
                rank[1],
                int(entry.get("sort_order", 100)),
                str(entry.get("updated_at", "")),
                str(entry.get("entry_id", "")),
            )

        normed.sort(key=_sort_key)

        pname = product_name_map.get(pid, pid)
        for doc_type, entry in normed:
            # 仅注入该 purpose 允许的 doc_type（required + optional）
            if doc_type not in required_types and doc_type not in optional_types:
                continue
            title = entry.get("title", "")
            content = entry.get("content", "")
            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + "\n\n... (truncated)"
            part = f"### {pname} - {title}（用户级·{doc_type}）\n\n{content}"
            if not append(part, f"user:{pid}:{entry.get('entry_id', '')}"):
                break

        # required 缺失语义
        found = {dt for dt, _ in normed}
        missing = [
            f"user_product:{pid}:missing:{req}"
            for req in required_types
            if req not in found
        ]
        return missing

    def resolve_none(self) -> KnowledgeSlice:
        """none 模式：不注入任何产品知识。"""
        return KnowledgeSlice(
            content="",
            product_ids=[],
            source_document_ids=[],
            content_hash="sha256:none",
            char_count=0,
            truncated=False,
            knowledge_missing=[],
        )

    def _required_files_for_purpose(self, purpose: Purpose) -> list[str]:
        """purpose 下 required doc_type 对应的产品文件（全局产品）。"""
        required_types = PURPOSE_DOC_TYPES.get(purpose, {}).get("required", [])
        return [f"{dt}.md" for dt in required_types if dt in ("overview", "market-brief")]

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
