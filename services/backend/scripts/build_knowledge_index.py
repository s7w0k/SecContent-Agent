"""知识库轻量文档索引构建脚本（阶段四 S4-5）。

从产品知识库目录扫描所有文档，全量/增量构建 JSON 索引，
写入 `<knowledge_base>/_index/kb-index.json`。

用法：
    # 全量重建（忽略旧索引）
    python -m scripts.build_knowledge_index --base-dir /app/docs

    # 增量构建（仅重建变化文档，复用旧元数据）
    python -m scripts.build_knowledge_index --base-dir /app/docs --incremental

    # 指定索引输出目录
    python -m scripts.build_knowledge_index --index-dir /path/to/_index

退出码：
    0 成功；1 校验失败；2 构建异常
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agent.knowledge_index import KnowledgeIndexBuilder, KnowledgeIndexer

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scripts.build_knowledge_index")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建知识库轻量文档索引")
    parser.add_argument(
        "--base-dir",
        default="/app/docs",
        help="产品知识库根目录",
    )
    parser.add_argument(
        "--index-dir",
        default=None,
        help="索引输出目录（默认 <base-dir>/_index）",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="增量构建：内容未变化的文档复用旧索引元数据",
    )
    args = parser.parse_args()

    base_dir = Path(args.base_dir)
    index_file = (
        Path(args.index_dir) / "kb-index.json"
        if args.index_dir
        else base_dir / "_index" / "kb-index.json"
    )

    # 加载旧索引（增量复用）
    previous = None
    if args.incremental:
        previous = KnowledgeIndexer(index_file).load()

    builder = KnowledgeIndexBuilder(base_dir, index_dir=args.index_dir)
    manifest = builder.build_manifest(previous=previous)

    errors = builder.validate(manifest)
    if errors:
        logger.error("索引校验失败，共 %d 项：", len(errors))
        for err in errors:
            logger.error("  - %s", err)
        return 1

    index_version = builder.write(manifest)
    logger.info(
        "知识索引构建完成：docs=%d, index_version=%s, size=%d",
        manifest.doc_count,
        index_version[:16],
        index_file.stat().st_size if index_file.exists() else 0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
