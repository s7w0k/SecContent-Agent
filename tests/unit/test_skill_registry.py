"""
SkillRegistry 单元测试（阶段二 Step 2）

覆盖：
  - scan / snapshot / hash / version
  - PURPOSE_SKILLS 确定性映射与 get_required
  - load_instructions / load_reference（含路径安全）
  - 必需包失败、非法名称、坏 frontmatter 拒绝
  - match_chat 规则召回

运行:
    pytest tests/unit/test_skill_registry.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "services", "backend"))

from agent.skill_registry import (
    PURPOSE_SKILLS,
    SkillError,
    SkillRegistry,
    SkillResolutionError,
    SkillSecurityError,
)

# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _write_skill(root, name: str, description: str, extra_refs: dict | None = None):
    """构造一个最小合规 Skill 包。extra_refs: {相对路径: 内容}"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    refs_md = ""
    for rel, content in (extra_refs or {}).items():
        fp = skill_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        refs_md += f"参考：{rel}\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n"
        f"# {name}\n\n主体内容。\n\n{refs_md}",
        encoding="utf-8",
    )
    return skill_dir


def _make_registry(tmp_path) -> SkillRegistry:
    """构造三个规范包 + registry。"""
    _write_skill(
        tmp_path,
        "scoring-knowledge",
        "用于评分时选择知识资料，不用于对话。",
    )
    _write_skill(
        tmp_path,
        "draft-writing",
        "用于稿件生成，不用于评分。",
    )
    _write_skill(
        tmp_path,
        "compliance-review",
        "用于合规红线检查，不用于写作。",
    )
    registry = SkillRegistry(skills_root=str(tmp_path))
    registry.load()
    return registry


# ═══════════════════════════════════════════════════════════════
# PURPOSE_SKILLS 映射
# ═══════════════════════════════════════════════════════════════


def test_purpose_skills_mapping():
    assert PURPOSE_SKILLS == {
        "score": ["scoring-knowledge"],
        "draft": ["draft-writing", "compliance-review"],
        "chat": [],
    }


# ═══════════════════════════════════════════════════════════════
# scan / snapshot / get_required
# ═══════════════════════════════════════════════════════════════


def test_scan_and_snapshot(tmp_path):
    registry = _make_registry(tmp_path)
    assert registry.is_ready
    snap = registry.snapshot
    assert snap is not None
    assert set(snap.skills) == {
        "scoring-knowledge",
        "draft-writing",
        "compliance-review",
    }
    assert snap.version
    assert snap.snapshot_hash.startswith("sha256:")
    assert len(registry.snapshot_hash) > 8


def test_get_required_deterministic(tmp_path):
    registry = _make_registry(tmp_path)
    score = registry.get_required("score")
    assert [m.name for m in score] == ["scoring-knowledge"]
    draft = registry.get_required("draft")
    assert [m.name for m in draft] == ["draft-writing", "compliance-review"]
    # chat 无必需
    assert registry.get_required("chat") == []


def test_snapshot_hash_stable_and_sensitive(tmp_path):
    registry = _make_registry(tmp_path)
    h1 = registry.snapshot_hash
    # 幂等：再次 load 不改变
    registry.load()
    assert registry.snapshot_hash == h1
    # 修改一个文件 → hash 变化
    (tmp_path / "scoring-knowledge" / "SKILL.md").write_text(
        (tmp_path / "scoring-knowledge" / "SKILL.md").read_text(encoding="utf-8") + "\nchanged",
        encoding="utf-8",
    )
    registry.reload()
    assert registry.snapshot_hash != h1


def test_manifest_content_hash_and_references(tmp_path):
    _write_skill(
        tmp_path,
        "draft-writing",
        "用于稿件生成，不用于评分。",
        extra_refs={"references/a.md": "# A\n参考：../SKILL.md"},
    )
    _write_skill(tmp_path, "scoring-knowledge", "用于评分，不用于对话。")
    _write_skill(tmp_path, "compliance-review", "用于合规检查，不用于写作。")
    registry = SkillRegistry(skills_root=str(tmp_path))
    registry.load()
    manifest = registry.snapshot.skills["draft-writing"]
    assert manifest.content_hash.startswith("sha256:")
    assert "references/a.md" in manifest.reference_hashes
    assert manifest.reference_hashes["references/a.md"].startswith("sha256:")


# ═══════════════════════════════════════════════════════════════
# load_instructions / load_reference（内容 + 安全）
# ═══════════════════════════════════════════════════════════════


def test_load_instructions(tmp_path):
    registry = _make_registry(tmp_path)
    text = registry.load_instructions("scoring-knowledge")
    assert text.startswith("---")
    assert "name: scoring-knowledge" in text


def test_load_reference(tmp_path):
    _write_skill(
        tmp_path,
        "draft-writing",
        "用于稿件生成，不用于评分。",
        extra_refs={"assets/templates/pr.md": "# 模板\n正文"},
    )
    _write_skill(tmp_path, "scoring-knowledge", "用于评分，不用于对话。")
    _write_skill(tmp_path, "compliance-review", "用于合规检查，不用于写作。")
    registry = SkillRegistry(skills_root=str(tmp_path))
    registry.load()
    content = registry.load_reference("draft-writing", "assets/templates/pr.md")
    assert "正文" in content


def test_load_reference_rejects_outside(tmp_path):
    registry = _make_registry(tmp_path)
    with pytest.raises(SkillSecurityError):
        registry.load_reference("scoring-knowledge", "../outside.md")
    with pytest.raises(SkillSecurityError):
        registry.load_reference("scoring-knowledge", "/etc/passwd")
    with pytest.raises(SkillSecurityError):
        registry.load_reference("scoring-knowledge", "SKILL.md")  # 不在 references/assets 下


def test_load_instructions_unknown_skill(tmp_path):
    registry = _make_registry(tmp_path)
    with pytest.raises(SkillError):
        registry.load_instructions("not-exist")


# ═══════════════════════════════════════════════════════════════
# 失败场景：必需包缺失 / 非法名称 / 坏 frontmatter
# ═══════════════════════════════════════════════════════════════


def test_required_skill_missing_raises(tmp_path):
    _write_skill(tmp_path, "draft-writing", "用于稿件生成，不用于评分。")
    registry = SkillRegistry(skills_root=str(tmp_path))
    with pytest.raises(SkillResolutionError):
        registry.load()


def test_missing_skill_md_raises(tmp_path):
    skill_dir = tmp_path / "scoring-knowledge"
    skill_dir.mkdir()
    (skill_dir / "other.md").write_text("x", encoding="utf-8")
    registry = SkillRegistry(skills_root=str(tmp_path))
    with pytest.raises(SkillResolutionError):
        registry.load()


def test_invalid_name_rejected(tmp_path):
    _write_skill(tmp_path, "Bad Name", "desc 不适用")
    # 缺必需包也会报错，但 name 校验先触发
    with pytest.raises(SkillResolutionError):
        SkillRegistry(skills_root=str(tmp_path)).load()


def test_bad_frontmatter_rejected(tmp_path):
    skill_dir = tmp_path / "scoring-knowledge"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(SkillResolutionError):
        SkillRegistry(skills_root=str(tmp_path)).load()


def test_empty_description_rejected(tmp_path):
    skill_dir = tmp_path / "scoring-knowledge"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: scoring-knowledge\ndescription: \n---\nbody", encoding="utf-8"
    )
    with pytest.raises(SkillResolutionError):
        SkillRegistry(skills_root=str(tmp_path)).load()


# ═══════════════════════════════════════════════════════════════
# match_chat
# ═══════════════════════════════════════════════════════════════


def test_match_chat_rule_recall(tmp_path):
    registry = _make_registry(tmp_path)
    candidates = ["scoring-knowledge", "draft-writing", "compliance-review"]
    assert registry.match_chat("帮我写一篇PR初稿", candidates) == ["draft-writing"]
    # 「稿件」命中 draft-writing 触发词、「合规红线」命中 compliance-review（规则召回可多命中）
    matched = registry.match_chat("这个稿件有没有合规红线问题", candidates)
    assert "compliance-review" in matched
    assert "draft-writing" in matched
    assert "scoring-knowledge" in registry.match_chat("打分相关度多少", candidates)


def test_match_chat_empty_candidates(tmp_path):
    registry = _make_registry(tmp_path)
    assert registry.match_chat("随便问", []) == []
    assert registry.match_chat("随便问", None) == []


def test_match_chat_semantic_fallback(tmp_path):
    registry = _make_registry(tmp_path)
    # 无规则触发词时走语义兜底（description 词匹配）
    matched = registry.match_chat("知识资料选择 评分", ["scoring-knowledge", "draft-writing"])
    assert matched == ["scoring-knowledge"]
