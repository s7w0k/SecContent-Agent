"""CI Hard Gate 2：禁止隐式 / 生产默认 Legacy。

扫描 production/default 路径，发现 KNOWLEDGE_BACKEND 被设为 legacy，或以缺失
字段隐式回退 legacy，即视为 CI 失败。

Allowlist 仅允许：
  - 显式 legacy 测试（tests/**，带 legacy 语义的专用脚本）
  - docs/runbook/wiki-backend-rollback.md（紧急回滚说明）

用法:
    python scripts/check_wiki_default.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# production/default 扫描路径
SCAN_PATTERNS = [
    "services/backend/config.py",
    ".env.example",
    ".env*",
    "docker-compose*.yml",
    "deploy/**/*.yml",
    "deploy/**/*.env",
    "deploy/**/*.txt",
    "helm/**",
    ".github/workflows/**",
    "scripts/*.py",
    "scripts/*.sh",
    "scripts/*.ps1",
]

# Allowlist：仅允许显式 legacy 的测试与文档
ALLOW_LEGACY_SUBSTR = [
    "tests/",
    "docs/runbook/wiki-backend-rollback.md",
    "scripts/collect_legacy_baseline.py",  # 显式 legacy 采集专用工具
    "scripts/check_wiki_default.py",  # 本脚本仅注释提及 legacy（用于校验）
]

# 命中的两种模式：
#  1) KNOWLEDGE_BACKEND=legacy
#  2) KNOWLEDGE_BACKEND: ... = "legacy"
_ASSIGN_RE = re.compile(
    r"KNOWLEDGE_BACKEND\s*(?:[=:]\s*|\s*=\s*[\"'`])?(legacy)", re.IGNORECASE
)


def _is_allowed(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return any(rel.startswith(a) or rel.endswith(a) for a in ALLOW_LEGACY_SUBSTR)


def _scan_path(path: Path) -> list[str]:
    if _is_allowed(path):
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _ASSIGN_RE.search(line):
            hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    return hits


def main() -> int:
    violations: list[str] = []

    # 1) config.py 代码默认值必须为 wiki
    config_path = ROOT / "services" / "backend" / "config.py"
    if config_path.exists():
        cfg_text = config_path.read_text(encoding="utf-8", errors="ignore")
        if not re.search(
            r"KNOWLEDGE_BACKEND.*?default\s*=\s*\"wiki\"",
            cfg_text,
            re.DOTALL,
        ):
            violations.append(
                "services/backend/config.py: 默认 KNOWLEDGE_BACKEND 必须显式为 'wiki'"
            )
        for lineno, line in enumerate(cfg_text.splitlines(), start=1):
            if re.search(
                r"KNOWLEDGE_BACKEND.*['\"]legacy['\"]\s*(#.*default)?", line
            ) and ("default" in line.lower() or "=" in line):
                violations.append(
                    f"services/backend/config.py:{lineno}: 默认值不得为 legacy: {line.strip()}"
                )

    # 2) 扫描 production/default 路径
    for pattern in SCAN_PATTERNS:
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file():
                violations.extend(_scan_path(path))

    # 3) runtime_factory 不得有隐式 legacy default（缺失字段必须抛错而非回退）
    factory = ROOT / "services" / "backend" / "agent" / "wiki" / "runtime_factory.py"
    if factory.exists():
        fc = factory.read_text(encoding="utf-8", errors="ignore")
        if "KnowledgeRuntimeError" not in fc:
            violations.append(
                "runtime_factory.py: 缺失 KNOWLEDGE_BACKEND 字段时必须抛错，未使用 KnowledgeRuntimeError"
            )

    if violations:
        print("❌ CI Hard Gate 2 failed — 检测到隐式 / 生产默认 legacy：")
        for v in violations:
            print(f"  - {v}")
        print("若确为 emergency rollback，请显式配置并在 docs/runbook/wiki-backend-rollback.md 说明。")
        return 1

    print("✅ CI Hard Gate 2 passed — no production/default legacy found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
