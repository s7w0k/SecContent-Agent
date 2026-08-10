"""SkillRegistry - Agent Skills 注册与加载（阶段二 Step 2）。

Skills 只承载流程、规则和资料选择指引；产品/用户知识继续作为
有版本、有权限的事实源（知识库文件 / user_knowledge_entries）。

职责：
  - PURPOSE_SKILLS 确定性映射（score/draft/chat → Skill 名称列表）
  - scan(root)：扫描 skills 目录构建 SkillSnapshot（只读 manifest）
  - get_required(purpose)：score/draft 确定性加载必需 Skill
  - match_chat(query, authorized_candidates)：对话场景规则召回 + 语义选择（仅返回 Skill name）
  - load_instructions(name)：读取 SKILL.md 全文
  - load_reference(name, relative_path)：安全读取 references/ 或 assets/ 下文件
  - snapshot_hash / version：快照指纹，用于缓存 key 与变更检测

安全规则：
  - 只读操作；拒绝绝对路径、.. 越界、符号链接逃逸、skills root 越界
  - allowed-tools 不作为服务端权限边界（本模块不返回任何工具权限）
  - 必需 Skill 解析失败时抛出 SkillResolutionError，上层应关闭新 Context
    路径并回退旧 resolver，禁止静默丢失 scoring/compliance
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from config import get_settings

logger = logging.getLogger("backend.agent.skill_registry")

Purpose = Literal["score", "draft", "chat"]

# 确定性 purpose → Skill 映射（固定 pipeline 使用）
PURPOSE_SKILLS: dict[Purpose, list[str]] = {
    "score": ["scoring-knowledge"],
    "draft": ["draft-writing", "compliance-review"],
    "chat": [],
}

# score/draft 必需 Skill（解析失败必须显式报错，不得静默降级）
_REQUIRED_FOR_PURPOSE: dict[Purpose, tuple[str, ...]] = {
    "score": ("scoring-knowledge",),
    "draft": ("draft-writing", "compliance-review"),
    "chat": (),
}

# 规则召回触发词（match_chat 用；语义选择降级仅返回 Skill name）
_CHAT_TRIGGER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "scoring-knowledge": ("评分", "相关度", "打分", "relevance"),
    "draft-writing": ("写稿", "稿件", "PR稿", "初稿", "draft", "改写", "标题"),
    "compliance-review": ("合规", "红线", "审查", "检查", "审核"),
}

_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class SkillError(Exception):
    """Skill 基础异常。"""


class SkillResolutionError(SkillError):
    """必需 Skill 解析失败（应触发回退旧 resolver）。"""


class SkillSecurityError(SkillError):
    """Skill 路径/内容安全异常。"""


@dataclass(frozen=True)
class SkillManifest:
    """只读 Skill 清单。"""

    name: str
    version: str
    description: str
    required: bool
    content_hash: str
    reference_hashes: dict[str, str] = field(default_factory=dict)
    skill_dir: str = ""  # 相对 skills root 的目录名（日志用，不参与 hash）


@dataclass(frozen=True)
class SkillSnapshot:
    """不可变快照：name → SkillManifest + 整体指纹。"""

    skills: dict[str, SkillManifest] = field(default_factory=dict)
    version: str = ""
    built_at: str = ""

    @property
    def snapshot_hash(self) -> str:
        parts = []
        for name in sorted(self.skills):
            m = self.skills[name]
            refs = ",".join(f"{k}={v}" for k, v in sorted(m.reference_hashes.items()))
            parts.append(f"{m.name}:{m.version}:{m.content_hash}:{refs}")
        content = "|".join(parts)
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def __len__(self) -> int:
        return len(self.skills)


def _sha256(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析 SKILL.md frontmatter，返回 (meta, body)。"""
    if not text.startswith("---"):
        raise SkillError("缺少 frontmatter")
    lines = text.splitlines()
    end = None
    for i in range(1, min(len(lines), 20)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise SkillError("frontmatter 缺少结束标记")
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


def _safe_resolve(root: Path, rel_path: str) -> Path:
    """在 root 内安全解析相对路径，拒绝越界/绝对/符号链接逃逸。"""
    if not rel_path:
        raise SkillSecurityError("空路径引用")
    if rel_path.startswith("/") or re.match(r"^[A-Za-z]:", rel_path):
        raise SkillSecurityError(f"绝对路径被拒绝: {rel_path}")
    if ".." in rel_path:
        raise SkillSecurityError(f"越界路径被拒绝: {rel_path}")
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise SkillSecurityError(f"路径越出 skill root: {rel_path}") from exc
    if candidate.is_symlink():
        raise SkillSecurityError(f"符号链接被拒绝: {rel_path}")
    return candidate


def _scan_skill_dir(skill_dir: Path, required_names: frozenset[str]) -> SkillManifest:
    """扫描单个 Skill 目录并构建 manifest。"""
    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise SkillResolutionError(f"Skill '{name}' 缺少 SKILL.md")

    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as exc:
        raise SkillResolutionError(f"Skill '{name}' SKILL.md 读取失败: {exc}") from exc

    try:
        meta, _body = _parse_frontmatter(text)
    except SkillError as exc:
        raise SkillResolutionError(f"Skill '{name}' frontmatter 非法: {exc}") from exc

    front_name = meta.get("name", "")
    if not _NAME_RE.match(front_name) or front_name != name:
        raise SkillResolutionError(f"Skill '{name}' name 非法或与目录不一致: {front_name!r}")

    description = meta.get("description", "")
    if not description:
        raise SkillResolutionError(f"Skill '{name}' description 为空")

    # 引用文件 hash（references/ assets/ 下，相对引用）
    reference_hashes: dict[str, str] = {}
    for rel in sorted(p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*.md")):
        if rel == "SKILL.md":
            continue
        fp = skill_dir / rel
        if fp.is_symlink():
            raise SkillSecurityError(f"Skill '{name}' 引用为符号链接: {rel}")
        try:
            reference_hashes[rel] = _sha256(fp.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SkillResolutionError(
                f"Skill '{name}' 引用文件读取失败: {rel}: {exc}"
            ) from exc

    version = meta.get("version", "1.0.0")
    return SkillManifest(
        name=name,
        version=version,
        description=description,
        required=name in required_names,
        content_hash=_sha256(text),
        reference_hashes=reference_hashes,
        skill_dir=skill_dir.name,
    )


class SkillRegistry:
    """Agent Skills 注册表。

    - 启动时调用 load() 构建快照（先扫描校验，完整通过后原子替换）
    - reload() 用于变更检测
    - 必需 Skill 解析失败时 get_required() 抛 SkillResolutionError
    """

    def __init__(self, skills_root: str | Path | None = None):
        settings = get_settings()
        if skills_root is None:
            kb = Path(settings.KNOWLEDGE_BASE_DIR)
            skills_root = kb / "skills"
        self._skills_root = Path(skills_root)
        self._snapshot: SkillSnapshot | None = None

    # ── 生命周期 ──────────────────────────────────────────

    def load(self, force: bool = False) -> SkillSnapshot:
        """扫描并替换当前快照。默认缓存（幂等）。"""
        if not force and self._snapshot is not None:
            return self._snapshot
        snapshot = self.scan(self._skills_root)
        self._snapshot = snapshot
        logger.info(
            "SkillRegistry loaded %d skills (version=%s, hash=%s)",
            len(snapshot),
            snapshot.version,
            snapshot.snapshot_hash[:16],
        )
        return snapshot

    def reload(self) -> SkillSnapshot:
        """强制重扫并原子替换快照。"""
        return self.load(force=True)

    # ── 扫描 / 快照 ───────────────────────────────────────

    def scan(self, root: str | Path) -> SkillSnapshot:
        """扫描 skills 目录，构建并完整校验快照（不替换当前快照）。

        校验失败时不产生部分快照；调用方决定是否回退旧快照。
        """
        root_path = Path(root)
        required_names = frozenset(
            name for names in _REQUIRED_FOR_PURPOSE.values() for name in names
        )
        manifests: dict[str, SkillManifest] = {}
        if not root_path.exists():
            raise SkillResolutionError(f"skills root 不存在: {root_path}")

        for skill_dir in sorted(root_path.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not _NAME_RE.match(skill_dir.name):
                raise SkillResolutionError(f"Skill 目录名非法: {skill_dir.name}")
            manifest = _scan_skill_dir(skill_dir, required_names)
            manifests[manifest.name] = manifest

        # 必需包完整性校验
        for required in sorted(required_names):
            if required not in manifests:
                raise SkillResolutionError(f"必需 Skill 缺失: {required}")

        return SkillSnapshot(
            skills=manifests,
            version=datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
            built_at=datetime.now(UTC).isoformat(),
        )

    @property
    def snapshot(self) -> SkillSnapshot | None:
        return self._snapshot

    @property
    def is_ready(self) -> bool:
        """必需 Skill 已全部解析且快照可用。"""
        if self._snapshot is None:
            return False
        required = frozenset(
            name for names in _REQUIRED_FOR_PURPOSE.values() for name in names
        )
        return required.issubset(self._snapshot.skills)

    @property
    def snapshot_hash(self) -> str:
        """当前快照 hash；未加载时返回空。"""
        return self._snapshot.snapshot_hash if self._snapshot else ""

    # ── purpose 加载 ──────────────────────────────────────

    def get_required(self, purpose: Purpose) -> list[SkillManifest]:
        """确定性加载指定 purpose 的必需 Skill。

        Raises:
            SkillResolutionError: 必需 Skill 缺失或快照不可用
        """
        if self._snapshot is None:
            raise SkillResolutionError("SkillRegistry 未加载（快照为空）")
        names = _REQUIRED_FOR_PURPOSE.get(purpose, ())
        result: list[SkillManifest] = []
        for name in names:
            manifest = self._snapshot.skills.get(name)
            if manifest is None:
                raise SkillResolutionError(f"必需 Skill 缺失: {name} (purpose={purpose})")
            result.append(manifest)
        return result

    # ── chat 匹配 ─────────────────────────────────────────

    def match_chat(self, query: str, authorized_candidates: list[str] | None = None) -> list[str]:
        """对话场景匹配：规则召回优先，语义选择仅返回 Skill name。

        Args:
            query: 用户问题
            authorized_candidates: 授权候选 Skill 名称列表（API 层传入）

        Returns:
            匹配的 Skill name 列表（不返回内容/权限）
        """
        candidates = authorized_candidates or list(PURPOSE_SKILLS.get("chat", []))
        if not candidates:
            return []
        if self._snapshot is None:
            return []

        query_lower = query.lower()
        matched: list[str] = []
        for name in candidates:
            if name not in self._snapshot.skills:
                continue
            keywords = _CHAT_TRIGGER_KEYWORDS.get(name, ())
            if any(kw.lower() in query_lower for kw in keywords):
                matched.append(name)
        # 语义选择兜底：仅在规则召回为空时，用描述关键词简单打分
        if not matched:
            best_score = 0
            best_name = ""
            for name in candidates:
                manifest = self._snapshot.skills.get(name)
                if manifest is None:
                    continue
                score = sum(1 for w in manifest.description.split() if w in query)
                if score > best_score:
                    best_score = score
                    best_name = name
            if best_name:
                matched = [best_name]
        return matched

    # ── 内容加载 ──────────────────────────────────────────

    def load_instructions(self, name: str) -> str:
        """返回 SKILL.md 全文（frontmatter + 主体），供注入指令。"""
        manifest = self._manifest_or_raise(name)
        skill_dir = self._skills_root / manifest.skill_dir
        fp = _safe_resolve(skill_dir, "SKILL.md")
        try:
            return fp.read_text(encoding="utf-8")
        except Exception as exc:
            raise SkillError(f"Skill '{name}' 指令读取失败: {exc}") from exc

    def load_reference(self, name: str, relative_path: str) -> str:
        """安全读取 references/ 或 assets/ 下的引用文件。"""
        manifest = self._manifest_or_raise(name)
        skill_dir = self._skills_root / manifest.skill_dir
        if not (relative_path.startswith("references/") or relative_path.startswith("assets/")):
            raise SkillSecurityError(f"引用路径必须在 references/ 或 assets/ 下: {relative_path}")
        fp = _safe_resolve(skill_dir, relative_path)
        if not fp.is_file():
            raise SkillError(f"Skill '{name}' 引用文件不存在: {relative_path}")
        try:
            return fp.read_text(encoding="utf-8")
        except Exception as exc:
            raise SkillError(
                f"Skill '{name}' 引用文件读取失败: {relative_path}: {exc}"
            ) from exc

    def _manifest_or_raise(self, name: str) -> SkillManifest:
        if self._snapshot is None:
            raise SkillResolutionError("SkillRegistry 未加载（快照为空）")
        manifest = self._snapshot.skills.get(name)
        if manifest is None:
            raise SkillError(f"Skill 不存在: {name}")
        return manifest


# 模块级默认实例（应用启动时由 main.py 加载）
_default_registry: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """获取全局 SkillRegistry（懒初始化，幂等）。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = SkillRegistry()
        _default_registry.load()
    return _default_registry
