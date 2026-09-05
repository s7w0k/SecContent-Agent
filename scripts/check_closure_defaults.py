"""P4 收口门禁：生产默认不得回退 legacy（CI Hard Gate）。

检查 Settings 的【代码声明默认值】（经 pydantic model_fields 读取，
不受 .env / 环境变量影响）：
  - AGENT_EXECUTION_MODE 默认 skill_planned（新架构为默认主链）
  - KNOWLEDGE_BACKEND   默认 wiki（Two Hard Gates：GOAL B）
若任一默认被改回 legacy，则 exit 1 阻断发布。

用法：python scripts/check_closure_defaults.py
"""

from __future__ import annotations

import sys

REQUIRED_DEFAULTS = {
    "AGENT_EXECUTION_MODE": "skill_planned",
    "KNOWLEDGE_BACKEND": "wiki",
}


def main() -> int:
    sys.path.insert(0, "services/backend")
    from config import Settings

    fields = Settings.model_fields
    failures: list[str] = []
    for name, expected in REQUIRED_DEFAULTS.items():
        field = fields.get(name)
        actual = field.default if field is not None else "<missing>"
        if actual != expected:
            failures.append(f"{name} 默认={actual!r}，要求={expected!r}")
    if failures:
        sys.stdout.write("FAIL: 生产默认配置回退检查未通过\n")
        for item in failures:
            sys.stdout.write(f"  - {item}\n")
        return 1
    sys.stdout.write("OK: 生产默认配置符合收口要求（skill_planned / wiki）\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
