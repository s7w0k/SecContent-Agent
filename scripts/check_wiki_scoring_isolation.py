"""CI Hard Gate：严格 Wiki 评分隔离（PR-2）。

确保 Wiki 评分模式与 Legacy 子系统完全隔离，防止回归：
  1. 所有 `ScoringAgentV2(` 构造点必须显式传 `knowledge_provider=`（不允许省略/Nones）。
  2. 禁止任何晚注入 `scorer.xxx.knowledge_provider = ...` 或 `xxx.knowledge_provider = Legacy...`。
  3. `_score_with_wiki` 内绝不出现构造 Legacy Prompt 的 `_build_system_prompt_for_product`。

用法:
    python scripts/check_wiki_scoring_isolation.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 后端需扫描的 .py 源码目录/文件
SCAN_SOURCES = [
    ROOT / "services" / "backend" / "main.py",
    ROOT / "services" / "backend" / "worker.py",
    ROOT / "services" / "backend" / "agent" / "scorer_v2.py",
    ROOT / "services" / "backend" / "knowledge_admin",
]

# 允许不带 knowledge_provider 的合法边界：测试夹具/桩、文档注释、以及本脚本自身
ALLOW_WITHOUT_PROVIDER = [
    "test_",
    "tests/",
    "check_wiki_scoring_isolation.py",
]

# 1) `ScoringAgentV2(` 调用：要求同一调用内含 knowledge_provider=
#    采用多行感知：从 "ScoringAgentV2(" 起匹配到配对的 ")"，粗粒度以最近括号闭合为准。
_LATE_INJECT_RE = re.compile(
    r"\.knowledge_provider\s*=\s*(?:LegacyKnowledgeProvider|WikiKnowledgeProvider|"
    r"ShadowKnowledgeProvider)?\s*\("
)
_PROVIDER_KW_RE = re.compile(r"knowledge_provider\s*=\s*[^,\n)]")
# 方法体内对被调用方法的引用（用于排除"方法定义本身"这个词法命中）
_METHOD_CALL_RE = re.compile(r"\b_build_system_prompt_for_product\s*\(")


def _is_allowed_source(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return any(seg in rel for seg in ALLOW_WITHOUT_PROVIDER)


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for path in SCAN_SOURCES:
        if path.is_dir():
            out.extend(p for p in sorted(path.rglob("*.py")) if not _is_allowed_source(p))
        elif path.is_file() and not _is_allowed_source(path):
            out.append(path)
    return sorted(set(out))


def _check_no_late_injection(text: str, rel: str) -> list[str]:
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _LATE_INJECT_RE.search(line):
            hits.append(f"{rel}:{lineno}: late provider injection: {line.strip()}")
    return hits


def _check_explicit_provider(text: str, rel: str) -> list[str]:
    hits: list[str] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        idx = line.find("ScoringAgentV2(")
        if idx == -1:
            continue
        # 截取该构造调用的参数区直到配对右括号（或在同一行内有闭合）
        call_text = line[idx:]
        block = call_text
        if line.count("(") > line.count(")"):
            depth = call_text.count("(") - call_text.count(")")
            for j in range(lineno, len(lines)):
                block += "\n" + lines[j]
                depth += lines[j].count("(") - lines[j].count(")")
                if depth <= 0:
                    break
        if not _PROVIDER_KW_RE.search(block):
            hits.append(f"{rel}:{lineno}: ScoringAgentV2(...) 缺少显式 knowledge_provider=")
    return hits


def _check_no_legacy_prompt_in_wiki(text: str, rel: str) -> list[str]:
    """按缩进提取 `_score_with_wiki` 方法体，检查其中是否调用 Legacy Prompt Builder。"""
    lines = text.splitlines()
    body: list[str] = []
    capturing = False
    base_indent = 0
    for _lineno, line in enumerate(lines, start=1):
        if not capturing:
            if re.match(r"\s*async\s+def\s+_score_with_wiki\s*\(", line):
                capturing = True
                base_indent = len(line) - len(line.lstrip())
            continue
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        body.append(line)
    if any(_METHOD_CALL_RE.search(line) for line in body):
        return [
            f"{rel}: _score_with_wiki 方法体内不得调用 _build_system_prompt_for_product (Legacy Prompt)"
        ]
    return []


def main() -> int:
    violations: list[str] = []
    for path in _iter_py_files():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        violations.extend(_check_no_late_injection(text, rel))
        violations.extend(_check_explicit_provider(text, rel))
        violations.extend(_check_no_legacy_prompt_in_wiki(text, rel))

    if violations:
        print("❌ CI Hard Gate failed — Wiki 评分隔离被破坏：")
        for v in violations:
            print(f"  - {v}")
        return 1

    print("✅ CI Hard Gate passed — wiki scoring isolation intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
