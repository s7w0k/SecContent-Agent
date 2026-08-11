"""validate_agent_skills - 校验 agent-security-briefs/skills/ 下 Skill 包合规性。

校验维度（与阶段二 Step 1 约束一致）：
  - name 与目录名一致，只含小写字母、数字、连字符
  - description 非空，写明做什么/何时用/何时不用
  - SKILL.md frontmatter 合法（--- 包裹的 YAML：name + description）
  - SKILL.md 主体 ≤500 行 / 5000 token（1 token ≈ 4 字符估算）
  - 文件引用仅相对路径；拒绝绝对路径、.. 越界、循环引用、符号链接逃逸
  - references/、assets/ 存在性校验

用法：
  python scripts/validate_agent_skills.py [skills_root] [--strict]

退出码：0 = 全部通过；1 = 存在不合规项。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_LINES = 500
MAX_TOKENS = 5000
CHARS_PER_TOKEN = 4

NAME_RE = re.compile(r"^[a-z0-9-]+$")


class SkillValidationError(Exception):
    """Skill 校验失败。"""


def _validate_name(name: str, dir_name: str) -> list[str]:
    errors: list[str] = []
    if not NAME_RE.match(name):
        errors.append(f"name '{name}' 非法：只允许小写字母、数字、连字符")
    if name != dir_name:
        errors.append(f"name '{name}' 与目录名 '{dir_name}' 不一致")
    return errors


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md frontmatter，返回 (meta, body)。"""
    if not text.startswith("---"):
        raise SkillValidationError("缺少 frontmatter（必须以 --- 开头）")
    lines = text.splitlines()
    if len(lines) < 3:
        raise SkillValidationError("frontmatter 不完整")
    if lines[1].strip() != "---":
        # 找到第二个 ---
        end = None
        for i in range(2, min(len(lines), 20)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is None:
            raise SkillValidationError("frontmatter 缺少结束标记 ---")
    else:
        end = 1
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key:
            meta[key] = value
    body = "\n".join(lines[end + 1 :])
    return meta, body


def _resolve_refs(text: str, root: Path) -> list[str]:
    """提取 markdown 文本中的相对文件引用（反引号包裹的相对路径 + markdown 链接路径）。"""
    refs: list[str] = []
    for m in re.finditer(r"`([^`\n]+\.md)`", text):
        refs.append(m.group(1))
    for m in re.finditer(r"\]\(([^)#]+)\)", text):
        refs.append(m.group(1))
    for m in re.finditer(r"^(?:必读|加读|读取|参考)：?(.*)$", text, flags=re.MULTILINE):
        cleaned = m.group(1).strip()
        if cleaned.endswith(".md") or "templates/" in cleaned:
            refs.append(cleaned)
    return refs


def _validate_skill_dir(skill_dir: Path, strict: bool) -> list[str]:
    """校验单个 Skill 目录。返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return [f"{skill_dir.name}: 缺少 SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")

    try:
        meta, body = _parse_frontmatter(text)
    except SkillValidationError as exc:
        return [f"{skill_dir.name}: {exc}"]

    name = meta.get("name", "")
    description = meta.get("description", "")
    errors.extend(_validate_name(name, skill_dir.name))

    if not description:
        errors.append(f"{skill_dir.name}: description 为空")
    if "何时不用" not in description and "不用于" not in description and "不适用" not in description:
        errors.append(f"{skill_dir.name}: description 未写明何时不用（负向边界）")

    body_lines = len(body.splitlines())
    body_tokens = len(body) // CHARS_PER_TOKEN
    if body_lines > MAX_LINES:
        errors.append(f"{skill_dir.name}: 主体 {body_lines} 行 > {MAX_LINES} 行")
    if body_tokens > MAX_TOKENS:
        errors.append(f"{skill_dir.name}: 主体约 {body_tokens} token > {MAX_TOKENS} token")

    # 路径引用校验：相对路径、不越界、不绝对、不符号链接逃逸
    refs = _resolve_refs(text, skill_dir)
    seen_dirs: set[Path] = set()
    for ref in refs:
        if ref.startswith("/") or re.match(r"^[A-Za-z]:", ref):
            errors.append(f"{skill_dir.name}: 绝对路径引用被拒绝: {ref}")
            continue
        if ".." in ref:
            errors.append(f"{skill_dir.name}: 越界路径引用被拒绝: {ref}")
            continue
        # 校验引用文件存在
        candidate = (skill_dir / ref).resolve()
        if not candidate.exists():
            # references/ 与 assets/ 内的引用
            ref_rel = (skill_dir.parent / ref).resolve()
            if not ref_rel.exists() and not candidate.exists():
                errors.append(f"{skill_dir.name}: 引用文件不存在: {ref}")
                continue
            candidate = ref_rel if ref_rel.exists() else candidate
        try:
            candidate.relative_to(skill_dir.parent.resolve())
        except ValueError:
            errors.append(f"{skill_dir.name}: 引用越出 skills root 被拒绝: {ref}")
            continue
        if candidate.is_symlink():
            errors.append(f"{skill_dir.name}: 符号链接引用被拒绝: {ref}")
        seen_dirs.add(candidate.parent)

    # references/ 与 assets/ 目录存在性（若 SKILL.md 有引用则必须存在）
    for sub in ("references", "assets"):
        if (skill_dir / sub).exists() and not (skill_dir / sub).is_dir():
            errors.append(f"{skill_dir.name}: {sub} 存在但不是目录")

    # 循环引用检测：SKILL.md / references / assets 之间的引用图不允许成环
    errors.extend(_detect_cycles(skill_dir))

    if strict and skill_dir.name not in ("scoring-knowledge", "draft-writing", "compliance-review"):
        errors.append(f"{skill_dir.name}: strict 模式仅允许三个规范包")

    return errors


def _detect_cycles(skill_dir: Path) -> list[str]:
    """检测 skill 包内部文件的循环引用（A→B→…→A）。"""
    errors: list[str] = []
    md_files = list(skill_dir.rglob("*.md"))
    if len(md_files) < 2:
        return errors

    # 构建引用图：文件相对路径 -> 其引用的文件相对路径集合
    graph: dict[str, set[str]] = {}
    for fp in md_files:
        try:
            rel = str(fp.relative_to(skill_dir)).replace("\\", "/")
        except ValueError:
            continue
        refs: set[str] = set()
        try:
            text = fp.read_text(encoding="utf-8")
        except Exception:
            continue
        for ref in _resolve_refs(text, skill_dir):
            if not ref.endswith(".md") or ref.startswith("/") or ".." in ref:
                continue
            candidate = (skill_dir / ref).resolve()
            try:
                candidate.relative_to(skill_dir.resolve())
            except ValueError:
                continue
            if candidate in md_files:
                refs.add(str(candidate.relative_to(skill_dir)).replace("\\", "/"))
        graph[rel] = refs

    # DFS 找环
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in color:
                continue
            if color[nxt] == GRAY:
                cycle = [*stack[stack.index(nxt):], nxt]
                errors.append(f"{skill_dir.name}: 循环引用被拒绝: {' → '.join(cycle)}")
            elif color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for node in sorted(graph):
        if color[node] == WHITE:
            visit(node)
    return errors


def validate_skills_root(root: Path, strict: bool = False) -> list[str]:
    """校验整个 skills root，返回错误列表。"""
    errors: list[str] = []
    if not root.exists():
        return [f"skills root 不存在: {root}"]
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        errors.extend(_validate_skill_dir(skill_dir, strict))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Agent Skills 包合规性")
    parser.add_argument("skills_root", nargs="?", default=None,
                        help="skills 根目录（默认 agent-security-briefs/skills）")
    parser.add_argument("--strict", action="store_true",
                        help="严格模式：仅允许三个规范包")
    args = parser.parse_args()

    if args.skills_root:
        root = Path(args.skills_root)
    else:
        repo_root = Path(__file__).resolve().parent.parent
        root = repo_root / "agent-security-briefs" / "skills"

    errors = validate_skills_root(root, strict=args.strict)
    if errors:
        print("Skill 校验失败：")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"Skill 校验通过：{root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
