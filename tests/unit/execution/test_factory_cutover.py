"""共享装配 Cutover（计划 §16-18 / §24）+ 配置默认（§27 / §118）。

验收：
  - build_execution_runtime 装配出完整 execution 层（正常路径）
  - skill_registry 注册生产五技能，且 snapshot 稳定
  - skill_shadow 模式装配出 ShadowExecutor/Business-only-write 不变量
  - 仅 legacy（无 business 栈）装配时 bundle.engine=legacy
  - config 默认 AGENT_EXECUTION_MODE 为合法值（当前 legacy，安全默认）
"""

from __future__ import annotations

from agent.business_tools.contracts import build_business_tool_registry
from agent.business_tools.execution import BusinessToolExecutor
from agent.execution.contracts import ExecutionRequest, ExecutionResult, WorkflowExecutor
from agent.execution.factory import build_execution_runtime

SKILL_NAMES = {
    "article-triage",
    "product-scoring",
    "draft-writing",
    "draft-revision",
    "full-draft-workflow",
}


class _Settings:
    AGENT_EXECUTION_MODE = "legacy"
    AGENT_SHADOW_SAMPLE_PERCENT = 100
    AGENT_SHADOW_TIMEOUT_SECONDS = 60
    AGENT_SKILL_CANARY_PERCENT = 0
    AGENT_CANARY_HASH_SEED = "seccontent-agent-v1"
    KNOWLEDGE_BACKEND = "wiki"


class _LegacyStub(WorkflowExecutor):
    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(engine="legacy", status="SUCCEEDED")

    async def resume(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(engine="legacy", status="SUCCEEDED")


def _stack(settings=_Settings) -> tuple:
    registry = build_business_tool_registry()
    executor = BusinessToolExecutor(registry, adapters={})
    return registry, executor


class TestFactory:
    def test_assembles_full_stack(self) -> None:
        registry, executor = _stack()
        bundle = build_execution_runtime(
            settings=_Settings(), business_registry=registry, business_executor=executor
        )
        assert bundle.execution_router is not None
        assert bundle.skill_registry is not None
        assert set(bundle.skill_registry.names()) == SKILL_NAMES
        assert bundle.orchestration_runtime is not None
        assert bundle.skill_runtime is not None
        assert bundle.skill_snapshot_hash.startswith("sha256:")
        assert bundle.mode == "legacy"

    def test_shadow_mode_assembles_shadow_executor(self) -> None:
        registry, executor = _stack()

        class _ShadowSettings(_Settings):
            AGENT_EXECUTION_MODE = "skill_shadow"

        bundle = build_execution_runtime(
            settings=_ShadowSettings(),
            business_registry=registry,
            business_executor=executor,
            legacy_executor=_LegacyStub(),
        )
        assert bundle.shadow_executor is not None
        assert bundle.shadow_comparator is not None
        assert bundle.legacy_executor is not None

    def test_legacy_only_when_no_business_stack(self) -> None:
        bundle = build_execution_runtime(settings=_Settings(), legacy_executor=_LegacyStub())
        assert bundle.legacy_executor is not None
        assert bundle.skill_registry is None
        assert bundle.metadata["engine"] == "legacy"

    def test_snapshot_stability(self) -> None:
        registry, executor = _stack()
        b1 = build_execution_runtime(
            settings=_Settings(), business_registry=registry, business_executor=executor
        )
        b2 = build_execution_runtime(
            settings=_Settings(), business_registry=registry, business_executor=executor
        )
        assert b1.skill_snapshot_hash == b2.skill_snapshot_hash


class TestConfig:
    def test_default_mode_is_valid(self) -> None:
        from config import Settings

        s = Settings(DEEPSEEK_API_KEY="test", _env_file=None)
        assert s.AGENT_EXECUTION_MODE in {"legacy", "skill_shadow", "skill_canary", "skill_planned"}
