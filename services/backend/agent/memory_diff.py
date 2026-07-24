"""草稿修订差异提取。

使用 difflib 计算确定性差异，对变化较大的段落可调用 LLM 归纳修改方向。
只保存差异段落和摘要，不保存完整草稿。
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("backend.agent.memory_diff")


@dataclass
class RevisionDiffResult:
    """修订差异结果。"""

    added_blocks: list[str] = field(default_factory=list)
    removed_blocks: list[str] = field(default_factory=list)
    changed_blocks: list[dict[str, str]] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    char_edit_ratio: float = 0.0
    has_changes: bool = False


def extract_diff(original: str, revised: str) -> RevisionDiffResult:
    """计算原稿与修订稿的差异。

    使用 Markdown 块拆分 + difflib.SequenceMatcher。

    Args:
        original: 原始草稿文本
        revised: 修订后草稿文本

    Returns:
        RevisionDiffResult
    """
    if not original or not revised:
        return RevisionDiffResult()

    orig_blocks = [b.strip() for b in original.split("\n\n") if b.strip()]
    rev_blocks = [b.strip() for b in revised.split("\n\n") if b.strip()]

    matcher = difflib.SequenceMatcher(None, orig_blocks, rev_blocks)
    result = RevisionDiffResult()

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        elif tag == "replace":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                orig_text = orig_blocks[i] if i < len(orig_blocks) else ""
                rev_text = rev_blocks[j] if j < len(rev_blocks) else ""
                result.changed_blocks.append({"old": orig_text[:500], "new": rev_text[:500]})
            # Handle unequal length
            for i in range(i1 + (j2 - j1), i2):
                if i < len(orig_blocks):
                    result.removed_blocks.append(orig_blocks[i][:500])
            for j in range(j1 + (i2 - i1), j2):
                if j < len(rev_blocks):
                    result.added_blocks.append(rev_blocks[j][:500])
        elif tag == "delete":
            for i in range(i1, i2):
                if i < len(orig_blocks):
                    result.removed_blocks.append(orig_blocks[i][:500])
        elif tag == "insert":
            for j in range(j1, j2):
                if j < len(rev_blocks):
                    result.added_blocks.append(rev_blocks[j][:500])

    result.has_changes = bool(result.added_blocks or result.removed_blocks or result.changed_blocks)

    # 计算编辑比率
    total_chars = len(original) + len(revised)
    if total_chars > 0:
        ratio = difflib.SequenceMatcher(None, original, revised).ratio()
        result.char_edit_ratio = round(1.0 - ratio, 4)

    # 生成简要摘要
    if result.changed_blocks:
        result.summary.append(f"修改了 {len(result.changed_blocks)} 个段落")
    if result.added_blocks:
        result.summary.append(f"新增了 {len(result.added_blocks)} 个段落")
    if result.removed_blocks:
        result.summary.append(f"删除了 {len(result.removed_blocks)} 个段落")

    logger.info(
        "diff extracted: changed=%d added=%d removed=%d edit_ratio=%.4f",
        len(result.changed_blocks),
        len(result.added_blocks),
        len(result.removed_blocks),
        result.char_edit_ratio,
    )

    return result
