"""P4 收口门禁测试：生产默认配置必须保持 skill_planned / wiki（防回退 legacy）。"""

from __future__ import annotations

from config import Settings

EXPECTED = {
    "AGENT_EXECUTION_MODE": "skill_planned",
    "KNOWLEDGE_BACKEND": "wiki",
}


def test_production_defaults_stay_on_new_architecture() -> None:
    """读取代码声明默认值（model_fields，不受 .env/环境变量影响）。"""
    fields = Settings.model_fields
    for name, expected in EXPECTED.items():
        field = fields.get(name)
        assert field is not None, f"{name} 字段缺失"
        assert field.default == expected, f"{name} 默认被改回: {field.default!r}"
